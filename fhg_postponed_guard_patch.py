from __future__ import annotations

"""Prevent stale/postponed FHG fixtures leaking into research and Challenge Export.

Research/integrity only. No provider calls, selection thresholds, staking, alerts,
or betting authority are changed.

A future FHG candidate is treated as non-runnable when any of the following is
true:
- its central event has an explicit terminal status;
- the central event kickoff has moved materially from the frozen candidate;
- a later successful DISCOVERY run for the same sport no longer saw the event.

The last rule uses the existing events.last_seen_at + collector_runs audit trail,
so a failed discovery call cannot incorrectly remove a fixture.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional, Tuple

import challenge_export_patch as challenge
import first_half_goal_hardening_patch as hardening

VERSION = "FHG_POSTPONED_GUARD_V1"
DISCOVERY_GRACE_SECONDS = 300
RESCHEDULE_DRIFT_MINUTES = 180
TERMINAL = {"POSTPONED", "CANCELLED", "CANCELED", "VOID", "ABANDONED"}
_INSTALLED = False
_ORIGINAL_RECONCILE = None
_ORIGINAL_FUTURE_CANDIDATES = None
_ORIGINAL_MODEL_SUPPORT = None


def _dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if out.tzinfo is None:
            out = out.replace(tzinfo=timezone.utc)
        return out.astimezone(timezone.utc)
    except Exception:
        return None


def _latest_successful_discovery(db, sport_key: str) -> Optional[datetime]:
    if not sport_key:
        return None
    try:
        row = db.fetchone(
            """SELECT * FROM collector_runs
               WHERE run_type='DISCOVERY' AND ok=1 AND sport_key=?
               ORDER BY id DESC LIMIT 1""",
            (sport_key,),
        )
    except Exception:
        return None
    if not row:
        return None
    # Keep this schema-tolerant: different historical migrations used slightly
    # different timestamp names.
    return _dt(
        row.get("finished_at")
        or row.get("ended_at")
        or row.get("completed_at")
        or row.get("started_at")
        or row.get("created_at")
    )


def _central_event(db, event_id: str) -> Optional[dict[str, Any]]:
    if not event_id:
        return None
    try:
        row = db.fetchone(
            """SELECT event_id,sport_key,commence_time,status,home_team,away_team,
                      first_seen_at,last_seen_at
               FROM events WHERE event_id=?""",
            (event_id,),
        )
    except Exception:
        return None
    return dict(row) if row else None


def _invalid_reason(db, candidate: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
    """Return (lifecycle_status, detail), or None when candidate remains live."""
    event_id = str(candidate.get("event_id") or "")
    sport_key = str(candidate.get("sport_key") or "")
    candidate_ko = _dt(candidate.get("commence_time"))
    created_at = _dt(candidate.get("created_at"))
    event = _central_event(db, event_id)
    latest_discovery = _latest_successful_discovery(db, sport_key)

    if event is not None:
        status = str(event.get("status") or "").upper()
        if status in TERMINAL:
            return "VOID", f"central_event_status={status}"

        event_ko = _dt(event.get("commence_time"))
        if candidate_ko is not None and event_ko is not None:
            drift_minutes = abs((event_ko - candidate_ko).total_seconds()) / 60.0
            if drift_minutes >= RESCHEDULE_DRIFT_MINUTES:
                return (
                    "SUPERSEDED",
                    f"central_kickoff_moved candidate={candidate_ko.isoformat()} event={event_ko.isoformat()} drift_minutes={drift_minutes:.1f}",
                )

        last_seen = _dt(event.get("last_seen_at"))
        if latest_discovery is not None and last_seen is not None:
            grace = timedelta(seconds=DISCOVERY_GRACE_SECONDS)
            # Require a discovery after candidate creation as an extra guard
            # against historical rows created from an older schedule snapshot.
            created_ok = created_at is None or latest_discovery > created_at + grace
            if created_ok and latest_discovery > last_seen + grace:
                return (
                    "VOID",
                    "provider_missing_after_successful_discovery "
                    f"last_seen_at={last_seen.isoformat()} latest_discovery={latest_discovery.isoformat()}",
                )
        return None

    # Missing central row is only actionable if a successful later discovery
    # proves the schedule has refreshed since this candidate was created.
    if latest_discovery is not None and created_at is not None:
        if latest_discovery > created_at + timedelta(seconds=DISCOVERY_GRACE_SECONDS):
            return (
                "VOID",
                f"central_event_missing_after_successful_discovery latest_discovery={latest_discovery.isoformat()}",
            )
    return None


def _record_lifecycle(db, candidate: Mapping[str, Any], status: str, detail: str, now: datetime) -> bool:
    try:
        changed = db.execute(
            "UPDATE first_half_goal_candidates SET status=? WHERE id=? AND status='OPEN'",
            (status, int(candidate["id"])),
        )
        if int(changed or 0) <= 0:
            return False
        db.execute(
            """INSERT INTO first_half_goal_lifecycle(
                   candidate_id,lifecycle_status,replacement_event_id,detected_at,detail
               ) VALUES(?,?,?,?,?)
               ON CONFLICT(candidate_id) DO UPDATE SET
                   lifecycle_status=excluded.lifecycle_status,
                   replacement_event_id=excluded.replacement_event_id,
                   detected_at=excluded.detected_at,
                   detail=excluded.detail""",
            (int(candidate["id"]), status, None, now.isoformat(), detail),
        )
        return True
    except Exception:
        return False


def reconcile_lifecycle_guarded(db, now: Optional[datetime] = None) -> Dict[str, int]:
    now = hardening._utc(now)
    base: Dict[str, int] = {}
    try:
        base = dict(_ORIGINAL_RECONCILE(db, now)) if _ORIGINAL_RECONCILE else {}
    except Exception:
        base = {}

    hardening._ensure_schema(db)
    try:
        rows = db.fetchall(
            """SELECT * FROM first_half_goal_candidates
               WHERE status='OPEN' AND commence_time>?
               ORDER BY commence_time,id""",
            (now.isoformat(),),
        )
    except Exception:
        rows = []

    early_voided = early_superseded = 0
    for raw in rows:
        candidate = dict(raw)
        invalid = _invalid_reason(db, candidate)
        if invalid is None:
            continue
        status, detail = invalid
        if _record_lifecycle(db, candidate, status, detail, now):
            early_voided += int(status == "VOID")
            early_superseded += int(status == "SUPERSEDED")

    base["early_voided"] = early_voided
    base["early_superseded"] = early_superseded
    return base


def _fhg_export_candidate_live(db, row: Mapping[str, Any]) -> bool:
    source_id = row.get("source_id")
    candidate = None
    try:
        if source_id not in (None, ""):
            candidate = db.fetchone(
                "SELECT * FROM first_half_goal_candidates WHERE id=?",
                (int(source_id),),
            )
        if not candidate:
            candidate = db.fetchone(
                "SELECT * FROM first_half_goal_candidates WHERE event_id=? ORDER BY id DESC LIMIT 1",
                (str(row.get("event_id") or ""),),
            )
    except Exception:
        candidate = None
    if not candidate:
        return True
    if str(candidate.get("status") or "").upper() != "OPEN":
        return False
    return _invalid_reason(db, dict(candidate)) is None


def future_candidates_guarded(db, now: datetime, end: datetime):
    rows = list(_ORIGINAL_FUTURE_CANDIDATES(db, now, end))
    out = []
    filtered = 0
    for row in rows:
        if str(row.get("source") or "") == "FHG1" and not _fhg_export_candidate_live(db, row):
            filtered += 1
            continue
        out.append(row)
    if filtered:
        print(f"FHG_POSTPONED_GUARD_EXPORT_FILTER filtered={filtered}", flush=True)
    return out


def model_support_guarded(db, now: datetime, end: datetime):
    rows = list(_ORIGINAL_MODEL_SUPPORT(db, now, end))
    out = []
    filtered = 0
    for row in rows:
        if str(row.get("model") or "") != "FHG1":
            out.append(row)
            continue
        synthetic = {
            "source_id": row.get("id"),
            "event_id": row.get("event_id"),
        }
        if _fhg_export_candidate_live(db, synthetic):
            out.append(row)
        else:
            filtered += 1
    if filtered:
        print(f"FHG_POSTPONED_GUARD_MODEL_FILTER filtered={filtered}", flush=True)
    return out


def install() -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_RECONCILE, _ORIGINAL_FUTURE_CANDIDATES, _ORIGINAL_MODEL_SUPPORT
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    _ORIGINAL_RECONCILE = hardening.reconcile_lifecycle
    _ORIGINAL_FUTURE_CANDIDATES = challenge._future_candidates
    _ORIGINAL_MODEL_SUPPORT = challenge._model_support

    hardening.reconcile_lifecycle = reconcile_lifecycle_guarded
    challenge._future_candidates = future_candidates_guarded
    challenge._model_support = model_support_guarded

    # Clean up already-stale OPEN rows immediately on process start. The export
    # filter remains an independent second defence between maintenance cycles.
    try:
        startup = reconcile_lifecycle_guarded(hardening.fhg_runtime.db if hasattr(hardening.fhg_runtime, "db") else None)
    except Exception:
        startup = {"startup_cleanup": "deferred_to_next_maintenance"}

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "startup": startup,
        "provider_calls_added": 0,
        "selection_threshold_changed": False,
        "betting_authority_changed": False,
        "challenge_export_guard": True,
        "future_lifecycle_guard": True,
    }


__all__ = ["VERSION", "install", "reconcile_lifecycle_guarded"]
