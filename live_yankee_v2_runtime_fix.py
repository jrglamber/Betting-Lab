from __future__ import annotations

"""Self-healing runtime guard for Live Yankee price-tolerance V2.

The V2 experiment is intentionally research-only and adds no provider calls.
It depends on the canonical ``live_yankee_candidates`` queue.  The 25-Oct
production export showed that V2's schema/meta row existed while its audit,
eligibility, card and leg tables stayed empty despite current source-pocket
bets.  This guard removes that silent dependency by:

1. installing the existing deterministic Live Yankee runtime;
2. seeding the current candidate queue directly from the already-stored source
   rows using the feed's current source policy (zero Odds API calls);
3. normalising the legacy ``NFL`` lane label when it refers to the same current
   multisport source row;
4. running V2 immediately after that seed;
5. repeating the zero-provider-call guard after each normal worker cycle; and
6. recording an explicit guard health row so a zero-evidence stall is visible.

No selection threshold, price tolerance, same-book rule, staking rule or betting
authority is changed.
"""

from datetime import datetime, timezone
import json
from typing import Any, Dict, Optional

import live_yankee_feed as feed
import live_yankee_price_tolerance_v2_patch as tolerance_v2
import live_yankee_runtime_patch as runtime

VERSION = "LIVE_YANKEE_PRICE_TOLERANCE_V2_RUNTIME_GUARD_V1"
_INSTALLED = False
_ORIGINAL_WORKER_ONE_CYCLE = None


def _utc(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _current_source_rows(db, now: datetime) -> list[dict[str, Any]]:
    """Read the feed's current actionable source rows without provider calls."""
    try:
        rows = feed._source_rows(
            db,
            "1970-01-01T00:00:00+00:00",
            now,
        )
    except Exception:
        rows = []
    return [dict(row) for row in rows]


def _normalise_legacy_lanes(db, rows: list[dict[str, Any]]) -> int:
    """Repair metadata-only lane renames without changing frozen candidate prices."""
    changed = 0
    for row in rows:
        lane = str(row.get("source_lane") or "")
        table = str(row.get("source_table") or "")
        source_id = int(row.get("source_id") or 0)
        if not lane or not table or not source_id:
            continue

        key = feed._candidate_key(
            row.get("event_id"),
            row.get("market_key"),
            row.get("selection"),
            row.get("point"),
        )
        existing = db.fetchone(
            """SELECT id,source_lane,source_table,source_id
               FROM live_yankee_candidates WHERE candidate_key=?""",
            (key,),
        ) or {}
        if not existing:
            continue

        # Only rename metadata when this is demonstrably the same underlying
        # source row. min_odds/source_odds remain frozen and are never rewritten.
        if (
            str(existing.get("source_table") or "") == table
            and int(existing.get("source_id") or 0) == source_id
            and str(existing.get("source_lane") or "") != lane
        ):
            changed += int(
                db.execute(
                    "UPDATE live_yankee_candidates SET source_lane=? WHERE id=?",
                    (lane, int(existing["id"])),
                )
                or 0
            )
    return changed


def _seed_candidates(db, now: datetime) -> Dict[str, int]:
    """Ensure V2 can see the same candidate universe as the Live Yankee feed."""
    feed._ensure_schema(db)
    rows = _current_source_rows(db, now)

    # _ingest is idempotent (candidate_key UNIQUE / ON CONFLICT DO NOTHING) and
    # makes no provider call. Source-focus is already installed by runtime.install
    # before this guard executes, so these are the current six-lane policies.
    seeded = feed._ingest(
        db,
        "1970-01-01T00:00:00+00:00",
        now,
    )
    renamed = _normalise_legacy_lanes(db, rows)
    return {
        "source_rows": len(rows),
        "inserted": int((seeded or {}).get("inserted") or 0),
        "legacy_lane_rows_normalised": renamed,
    }


def run_guarded_maintenance(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = _utc(now)
    seeded = _seed_candidates(db, now)
    research = tolerance_v2.run_research_maintenance(db, now)
    score = tolerance_v2.scoreboard(db)

    source_rows = int(seeded.get("source_rows") or 0)
    observed = int(score.get("observed_candidates") or 0)
    healthy = not (source_rows > 0 and observed == 0)

    payload = {
        "ok": healthy,
        "version": VERSION,
        "seed": seeded,
        "research": research,
        "observed_candidates": observed,
        "fresh_quoted_candidates": int(score.get("fresh_quoted_candidates") or 0),
        "provider_calls_added": 0,
        "selection_rules_changed": False,
        "price_rules_changed": False,
        "same_book_rules_changed": False,
        "betting_authority_changed": False,
    }
    try:
        db.record_collector_run(
            "LIVE_YANKEE_PRICE_TOLERANCE_V2_GUARD",
            healthy,
            detail=json.dumps(payload, sort_keys=True, default=str),
        )
    except Exception:
        pass

    if not healthy:
        print(
            "LIVE_YANKEE_PRICE_TOLERANCE_V2_GUARD_STALLED "
            + json.dumps(payload, sort_keys=True, default=str),
            flush=True,
        )
    return payload


def install(db=None) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_WORKER_ONE_CYCLE
    db = db or feed.web_module.db

    base = runtime.install(db)
    if _INSTALLED:
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
            "runtime": base,
            "guard": run_guarded_maintenance(db),
        }

    # Run once during import/startup so exports cannot remain silently empty
    # until a later worker pass.
    startup = run_guarded_maintenance(db)

    worker = feed.web_module.worker
    if not getattr(worker, "_live_yankee_v2_guard_installed", False):
        _ORIGINAL_WORKER_ONE_CYCLE = worker.one_cycle

        def one_cycle_with_v2_guard():
            result = _ORIGINAL_WORKER_ONE_CYCLE()
            try:
                run_guarded_maintenance(worker.db)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                try:
                    worker.db.record_collector_run(
                        "LIVE_YANKEE_PRICE_TOLERANCE_V2_GUARD",
                        False,
                        detail=detail,
                    )
                except Exception:
                    pass
                print(
                    f"LIVE_YANKEE_PRICE_TOLERANCE_V2_GUARD_ERROR {detail}",
                    flush=True,
                )
            return result

        worker.one_cycle = one_cycle_with_v2_guard
        worker._live_yankee_v2_guard_installed = True

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "runtime": base,
        "startup_guard": startup,
        "worker_guard_installed": True,
        "provider_calls_added": 0,
        "selection_rules_changed": False,
        "price_rules_changed": False,
        "same_book_rules_changed": False,
        "betting_authority_changed": False,
    }


__all__ = ["VERSION", "install", "run_guarded_maintenance"]
