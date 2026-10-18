from __future__ import annotations

"""Targeted provider-credit policy for quote-refresh research lanes.

The provider's real remaining-credit guard remains authoritative, but the two
high-frequency manual-price labs also get explicit daily budgets so a busy slate
cannot consume the monthly allowance disproportionately.

Repeated successful polls which still leave an event without a currently usable
manual-book quote are backed off while kickoff is distant. Close to kickoff the
normal five-minute cadence is retained. Candidate generation, modelling,
settlement, card rules, staking and execution authority are unchanged.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

import hit_rate_multiples_lab
import live_yankee_feed

VERSION = "CREDIT_POLICY_TARGETED_BUDGET_V2"
HIT_RATE_DAILY_CREDITS = 200
LIVE_YANKEE_DAILY_CREDITS = 1_500
BASE_REFRESH_MINUTES = 5.0
MID_BACKOFF_MINUTES = 20.0
FAR_BACKOFF_MINUTES = 30.0
CLOSE_TO_KICKOFF_MINUTES = 90.0
FAR_FROM_KICKOFF_MINUTES = 240.0
RECENT_POLL_WINDOW_MINUTES = 30.0
REPEATED_MISS_POLLS = 2
_INSTALLED = False
_ORIGINAL_LY_TARGETS = None
_ORIGINAL_HRM_TARGETS = None


def _dt(value: Any):
    return live_yankee_feed._parse_iso(value)


def _recent_successes(db, run_type: str, event_id: str, now: datetime):
    since = (now - timedelta(minutes=RECENT_POLL_WINDOW_MINUTES)).isoformat()
    try:
        return db.fetchall(
            """
            SELECT started_at FROM collector_runs
            WHERE run_type=? AND event_id=? AND ok=1 AND started_at>=?
            ORDER BY started_at DESC,id DESC LIMIT 3
            """,
            (run_type, event_id, since),
        )
    except Exception:
        return []


def _live_yankee_has_usable(db, event_id: str, now: datetime) -> bool:
    try:
        rows = db.fetchall(
            "SELECT * FROM live_yankee_candidates WHERE event_id=? AND status='QUEUED' ORDER BY id",
            (event_id,),
        )
        return any(bool(live_yankee_feed._valid_quotes(db, row, now)) for row in rows)
    except Exception:
        # Fail open: an observability/backoff failure must never suppress research.
        return True


def _hit_rate_has_usable(db, event_id: str, now: datetime) -> bool:
    try:
        rows = db.fetchall(
            "SELECT * FROM hit_rate_candidates WHERE event_id=? AND status='QUEUED' ORDER BY id",
            (event_id,),
        )
        return any(bool(hit_rate_multiples_lab._quotes(db, row, now, False)) for row in rows)
    except Exception:
        return True


def _backoff_targets(
    db,
    now: datetime,
    targets: Sequence[Mapping[str, Any]],
    run_type: str,
    has_usable: Callable[[Any, str, datetime], bool],
):
    kept = []
    for raw in targets:
        target = dict(raw)
        event_id = str(target.get("event_id") or "")
        kickoff = _dt(target.get("commence_time"))
        if not event_id or kickoff is None:
            kept.append(raw)
            continue

        minutes_to_kickoff = (kickoff - now).total_seconds() / 60.0
        if minutes_to_kickoff <= CLOSE_TO_KICKOFF_MINUTES:
            kept.append(raw)
            continue

        # If a currently usable quote exists, preserve the normal five-minute
        # cadence so a nearly placeable card is not starved of price updates.
        if has_usable(db, event_id, now):
            kept.append(raw)
            continue

        recent = _recent_successes(db, run_type, event_id, now)
        if len(recent) < REPEATED_MISS_POLLS:
            kept.append(raw)
            continue

        required = (
            FAR_BACKOFF_MINUTES
            if minutes_to_kickoff > FAR_FROM_KICKOFF_MINUTES
            else MID_BACKOFF_MINUTES
        )
        last = _dt((recent[0] or {}).get("started_at")) if recent else None
        if last is None:
            kept.append(raw)
            continue
        elapsed = (now - last).total_seconds() / 60.0
        if elapsed + 1e-9 >= required:
            kept.append(raw)
    return kept


def install() -> dict:
    global _INSTALLED, _ORIGINAL_LY_TARGETS, _ORIGINAL_HRM_TARGETS

    before = {
        "hit_rate_multiples": int(hit_rate_multiples_lab.DAILY_REFRESH_CREDIT_BUDGET),
        "live_yankee": int(live_yankee_feed.DAILY_REFRESH_CREDIT_BUDGET),
    }

    hit_rate_multiples_lab.DAILY_REFRESH_CREDIT_BUDGET = HIT_RATE_DAILY_CREDITS
    live_yankee_feed.DAILY_REFRESH_CREDIT_BUDGET = LIVE_YANKEE_DAILY_CREDITS

    if not _INSTALLED:
        _ORIGINAL_LY_TARGETS = live_yankee_feed._targets
        _ORIGINAL_HRM_TARGETS = hit_rate_multiples_lab._targets

        def live_yankee_targets_with_backoff(db, now=None):
            now = now or datetime.now(timezone.utc)
            targets = _ORIGINAL_LY_TARGETS(db, now)
            return _backoff_targets(
                db, now, targets, "LIVE_YANKEE_QUOTES", _live_yankee_has_usable
            )

        def hit_rate_targets_with_backoff(db, now=None):
            now = now or datetime.now(timezone.utc)
            targets = _ORIGINAL_HRM_TARGETS(db, now)
            return _backoff_targets(
                db, now, targets, "HIT_RATE_MULTIPLES_QUOTES", _hit_rate_has_usable
            )

        live_yankee_feed._targets = live_yankee_targets_with_backoff
        hit_rate_multiples_lab._targets = hit_rate_targets_with_backoff

    result = {
        "installed": True,
        "already_installed": _INSTALLED,
        "version": VERSION,
        "policy": "targeted_daily_caps_plus_adaptive_failed_price_backoff",
        "before": before,
        "after": {
            "hit_rate_multiples": int(hit_rate_multiples_lab.DAILY_REFRESH_CREDIT_BUDGET),
            "live_yankee": int(live_yankee_feed.DAILY_REFRESH_CREDIT_BUDGET),
        },
        "backoff": {
            "base_minutes": BASE_REFRESH_MINUTES,
            "after_repeated_no_usable_quote": REPEATED_MISS_POLLS,
            "mid_minutes": MID_BACKOFF_MINUTES,
            "far_minutes": FAR_BACKOFF_MINUTES,
            "close_to_kickoff_minutes": CLOSE_TO_KICKOFF_MINUTES,
            "far_from_kickoff_minutes": FAR_FROM_KICKOFF_MINUTES,
            "recent_poll_window_minutes": RECENT_POLL_WINDOW_MINUTES,
        },
        "selection_rules_changed": False,
        "betting_authority_changed": False,
    }
    _INSTALLED = True
    return result
