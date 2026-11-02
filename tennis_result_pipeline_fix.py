from __future__ import annotations

"""Harden tennis/TPRED result collection and settlement.

Fixes two gaps in the existing research pipeline:
1) the rich tennis collector advanced past the current UTC date after an early-
   morning fetch, so matches completed later that day were never revisited;
2) TPRED-only fixtures were not guaranteed to receive a tennis_results row,
   because the Odds-API score collector is driven by execution bets.

The bridge is deliberately conservative: it only maps a cached rich-provider
result when tour + exact normalized player pair + near-date produce exactly one
candidate, and the stored winner is one of those two players. Ambiguous matches,
walkovers and cancellations are left unresolved rather than guessed.

Research/settlement only. No selections, staking, alerts or betting authority.
"""

from datetime import date, datetime, timedelta, timezone
import json
from typing import Any, Dict, Mapping, Optional

import adaptive_winner_research as adaptive
import tennis_predictive3 as tpred3
import tennis_rich_source as rich
from db import utc_now_iso
from tennis_shadow import settle_tennis_event

VERSION = "TENNIS_RESULT_PIPELINE_FIX_V1"
CURRENT_REFRESH_HOURS = 4.0
STARTUP_REPAIR_DAYS = 21
CURRENT_REFRESH_LOOKBACK_DAYS = 1
_INSTALLED = False


def _dt(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        out = datetime.fromisoformat(text)
        if out.tzinfo is None:
            out = out.replace(tzinfo=timezone.utc)
        return out.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(value)[:10], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def _ensure_state(db) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS tennis_result_pipeline_state(
            state_key TEXT PRIMARY KEY,
            state_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def _state_get(db, key: str) -> Optional[str]:
    row = db.fetchone(
        "SELECT state_value FROM tennis_result_pipeline_state WHERE state_key=?",
        (key,),
    )
    return str(row.get("state_value")) if row and row.get("state_value") is not None else None


def _state_set(db, key: str, value: str) -> None:
    now = utc_now_iso()
    db.execute(
        """
        INSERT INTO tennis_result_pipeline_state(state_key,state_value,updated_at)
        VALUES(?,?,?)
        ON CONFLICT(state_key) DO UPDATE SET
            state_value=excluded.state_value,updated_at=excluded.updated_at
        """,
        (key, value, now),
    )


def _refresh_recent_rich_results(
    db,
    *,
    now: Optional[datetime] = None,
    force: bool = False,
    lookback_days: int = CURRENT_REFRESH_LOOKBACK_DAYS,
    max_requests: int = 12,
) -> Dict[str, Any]:
    """Refresh recent ATP/WTA completed results without re-running model logic."""
    now = now or datetime.now(timezone.utc)
    state = db.fetchone("SELECT * FROM tennis_rich_sync_state WHERE singleton_id=1") or {}
    last = _dt(state.get("last_current_results_at"))
    if not force and last is not None:
        hours = (now - last).total_seconds() / 3600.0
        if hours < CURRENT_REFRESH_HOURS:
            return {"ok": True, "skipped": True, "reason": "current_refresh_not_due", "hours_since": hours, "requests": 0}

    client = rich.RichClient(db, max_requests=max_requests)
    if not client.key:
        return {"ok": False, "skipped": True, "reason": "TENNIS_RAPIDAPI_KEY_missing", "requests": 0}
    if client.max_requests <= 0:
        return {"ok": True, "skipped": True, "reason": "rich_daily_cap_reached", "requests": 0}

    start = now.date() - timedelta(days=max(0, int(lookback_days)))
    end = now.date()
    tours: Dict[str, Any] = {}
    for tour in ("atp", "wta"):
        if not client.available:
            break
        tours[tour] = rich.sync_results_range(db, client, tour, start, end)

    # Mark the cadence only after a real provider attempt. If the key/cap is
    # unavailable we leave it due so recovery happens automatically later.
    db.execute(
        "UPDATE tennis_rich_sync_state SET last_current_results_at=? WHERE singleton_id=1",
        (now.isoformat(),),
    )
    return {
        "ok": client.errors == 0,
        "skipped": False,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "tours": tours,
        "requests": client.requests,
        "errors": client.errors,
    }


def _candidate_rich_result(db, event: Mapping[str, Any]) -> Dict[str, Any]:
    tour = str(event.get("tour") or "").strip().lower()
    if tour not in {"atp", "wta"}:
        return {"matched": False, "reason": "unsupported_tour"}
    commence = _dt(event.get("commence_time"))
    if commence is None:
        return {"matched": False, "reason": "bad_commence_time"}
    n1 = rich._norm_name(event.get("player_one"))
    n2 = rich._norm_name(event.get("player_two"))
    if not n1 or not n2 or n1 == n2:
        return {"matched": False, "reason": "bad_player_identity"}

    start = (commence.date() - timedelta(days=1)).isoformat()
    end = (commence.date() + timedelta(days=1)).isoformat()
    rows = db.fetchall(
        """
        SELECT * FROM tennis_rich_matches
        WHERE tour=? AND substr(match_date,1,10)>=? AND substr(match_date,1,10)<=?
        ORDER BY match_date,provider_match_id
        """,
        (tour, start, end),
    )
    candidates = []
    for row in rows:
        a = rich._norm_name(row.get("player1_name"))
        b = rich._norm_name(row.get("player2_name"))
        if {a, b} != {n1, n2}:
            continue
        result_type = str(row.get("result_type") or "").strip().lower()
        if any(token in result_type for token in ("walkover", "w/o", "cancel", "postpon")):
            continue
        p1id = row.get("player1_id")
        p2id = row.get("player2_id")
        winner_id = row.get("winner_id")
        if winner_id is None or winner_id not in {p1id, p2id}:
            continue
        candidates.append(row)

    if len(candidates) != 1:
        return {
            "matched": False,
            "reason": "no_exact_candidate" if not candidates else "ambiguous_exact_candidates",
            "candidate_count": len(candidates),
        }

    row = candidates[0]
    winner = row.get("player1_name") if row.get("winner_id") == row.get("player1_id") else row.get("player2_name")
    if rich._norm_name(winner) not in {n1, n2}:
        return {"matched": False, "reason": "winner_identity_mismatch", "candidate_count": 1}
    return {"matched": True, "row": row, "winner": str(winner), "candidate_count": 1}


def bridge_cached_results(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Create tennis_results only from unique exact rich-cache matches."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)
    events = db.fetchall(
        """
        SELECT DISTINCT e.event_id,e.tour,e.commence_time,e.player_one,e.player_two
        FROM tennis_events e
        LEFT JOIN tennis_results r ON r.event_id=e.event_id
        WHERE r.event_id IS NULL
          AND e.commence_time<=?
          AND (
                EXISTS(
                    SELECT 1 FROM tennis_execution_bets b
                    WHERE b.event_id=e.event_id AND b.status='OPEN'
                )
                OR EXISTS(
                    SELECT 1 FROM tennis_predictive3_predictions p
                    WHERE p.event_id=e.event_id AND p.status='OPEN'
                )
          )
        ORDER BY e.commence_time,e.event_id
        """,
        (cutoff.isoformat(),),
    )

    inserted = 0
    execution_settled = 0
    unmatched: Dict[str, int] = {}
    examples = []
    for event in events:
        match = _candidate_rich_result(db, event)
        if not match.get("matched"):
            reason = str(match.get("reason") or "unmatched")
            unmatched[reason] = unmatched.get(reason, 0) + 1
            if len(examples) < 8:
                examples.append({
                    "event_id": event.get("event_id"),
                    "fixture": f"{event.get('player_one')} v {event.get('player_two')}",
                    "commence_time": event.get("commence_time"),
                    "reason": reason,
                    "candidate_count": match.get("candidate_count"),
                })
            continue

        row = match["row"]
        winner = str(match["winner"])
        fetched = utc_now_iso()
        raw = {
            "bridge_version": VERSION,
            "rich_provider_match_id": row.get("provider_match_id"),
            "tour": row.get("tour"),
            "match_date": row.get("match_date"),
            "player1_name": row.get("player1_name"),
            "player2_name": row.get("player2_name"),
            "winner_id": row.get("winner_id"),
            "result": row.get("result"),
            "result_type": row.get("result_type"),
        }
        db.execute(
            """
            INSERT INTO tennis_results(
                event_id,fetched_at,completed_at,winner,player_one_score,
                player_two_score,source,settlement_quality,
                settlement_provenance,raw_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            (
                event.get("event_id"), fetched, row.get("match_date"), winner, None, None,
                "rapidapi_tennis_rich_cache", "RICH_EXACT_PAIR_DATE_UNIQUE",
                "exact normalized player pair + same tour + +/-1 UTC date; unique candidate; winner id verified; ambiguous/walkover/cancelled rows rejected",
                json.dumps(raw, separators=(",", ":"), default=str),
            ),
        )
        stored = db.fetchone("SELECT winner FROM tennis_results WHERE event_id=?", (event.get("event_id"),))
        if not stored:
            continue
        db.execute("UPDATE tennis_events SET status='COMPLETED' WHERE event_id=?", (event.get("event_id"),))
        execution_settled += settle_tennis_event(db, str(event.get("event_id")), str(stored.get("winner")))
        inserted += 1

    tpred3_settled = int(tpred3.settle_forward_predictions(db) or 0)
    try:
        tpred4_sync = adaptive._tpred4_current_predictions(db)
    except Exception as exc:
        tpred4_sync = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        tennis_w1_sync = adaptive.run_tennis_w1(db)
    except Exception as exc:
        tennis_w1_sync = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    out = {
        "ok": True,
        "version": VERSION,
        "events_considered": len(events),
        "results_inserted": inserted,
        "execution_bets_settled": execution_settled,
        "tpred3_predictions_settled": tpred3_settled,
        "tpred4_sync": tpred4_sync,
        "tennis_w1_sync": tennis_w1_sync,
        "unmatched": unmatched,
        "unmatched_examples": examples,
        "provider_calls": 0,
    }
    try:
        db.record_collector_run(
            "TENNIS_RESULT_BRIDGE",
            True,
            requested_markets="cached_results",
            estimated_cost=0,
            actual_cost=0,
            detail=json.dumps(out, sort_keys=True, default=str),
        )
    except Exception:
        pass
    return out


def _startup_repair(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    _ensure_state(db)
    done = _state_get(db, "startup_recent_result_repair_v1")
    if done:
        return {"ok": True, "skipped": True, "reason": "already_completed", "completed_at": done}
    refresh = _refresh_recent_rich_results(
        db,
        now=now,
        force=True,
        lookback_days=STARTUP_REPAIR_DAYS,
        max_requests=16,
    )
    # Only mark the one-off repair complete after an actual provider attempt.
    if not refresh.get("skipped"):
        _state_set(db, "startup_recent_result_repair_v1", utc_now_iso())
    bridge = bridge_cached_results(db, now)
    return {"ok": bool(refresh.get("ok", True)) and bool(bridge.get("ok", True)), "refresh": refresh, "bridge": bridge}


def install(db, tennis_engine=None) -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}
    _ensure_state(db)

    startup = _startup_repair(db)

    original_rich_maintenance = rich.maintenance
    if not getattr(rich, "_tennis_result_pipeline_fix_installed", False):
        def wrapped_rich_maintenance(*args, **kwargs):
            result = original_rich_maintenance(*args, **kwargs)
            now = kwargs.get("now") or datetime.now(timezone.utc)
            refresh = _refresh_recent_rich_results(db, now=now)
            bridge = bridge_cached_results(db, now)
            if isinstance(result, dict):
                result = dict(result)
                result["current_results_refresh"] = refresh
                result["result_bridge"] = bridge
            return result

        rich.maintenance = wrapped_rich_maintenance
        rich._tennis_result_pipeline_fix_installed = True

    engine_wrapped = False
    if tennis_engine is not None and not getattr(tennis_engine, "_tennis_result_pipeline_fix_installed", False):
        original_cycle = tennis_engine.one_cycle

        def wrapped_cycle(*args, **kwargs):
            result = original_cycle(*args, **kwargs)
            bridge = bridge_cached_results(db, kwargs.get("now"))
            if isinstance(result, dict):
                result = dict(result)
                result["tennis_result_bridge"] = bridge
            return result

        tennis_engine.one_cycle = wrapped_cycle
        tennis_engine._tennis_result_pipeline_fix_installed = True
        engine_wrapped = True

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "startup": startup,
        "current_refresh_hours": CURRENT_REFRESH_HOURS,
        "startup_repair_days": STARTUP_REPAIR_DAYS,
        "strict_match_policy": "tour + exact normalized player pair + unique +/-1 UTC date candidate + verified winner id",
        "ambiguous_results_guessed": False,
        "provider_calls_added_per_due_refresh": "normally 2 (ATP + WTA), pagination permitting",
        "engine_wrapped": engine_wrapped,
        "betting_authority_changed": False,
        "staking_changed": False,
        "alerts_changed": False,
    }
