from __future__ import annotations

"""Reduce wasted paid quote polling without changing frozen selection rules.

The existing quote collectors remain authoritative. This patch only changes when
queued Live Yankee and HR events are refreshed and how much daily paid quota the
two high-frequency quote lanes may consume. Five-minute reaction is reserved for
genuinely near-card fixtures close to kickoff; distant research is paced so a
morning slate cannot consume most of the day's allowance before the main UK/US
windows arrive.

No candidate, model, minimum-price, same-book, card-construction, staking,
settlement, Telegram or betting-authority rule is changed.
"""

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import credit_policy_unlimited_patch as base_policy
import hit_rate_multiples_lab
import live_yankee_feed

VERSION = "MULTIPLES_CREDIT_EFFICIENCY_V2"
LIVE_YANKEE_DAILY_CREDITS = 1_200
HIT_RATE_DAILY_CREDITS = 400

URGENT_KICKOFF_MINUTES = 90.0
FAR_FROM_KICKOFF_MINUTES = 240.0
SOFT_DAY_FLOOR_FRACTION = 0.15

# Normal Yankee polling: 60-120m when >4h away, 30-45m from 90m-4h,
# and 15m inside 90m. Only a genuinely near-card stream gets 5m treatment.
LIVE_USABLE_MID_MINUTES = 30.0
LIVE_USABLE_FAR_MINUTES = 60.0
LIVE_MISS_MID_MINUTES = 45.0
LIVE_MISS_FAR_MINUTES = 120.0
LIVE_URGENT_MINUTES = 15.0
LIVE_NEAR_CARD_URGENT_MINUTES = 5.0

# HR follows the same principle. A stream one leg short can refresh at 5m inside
# 90m, but normal urgent events are held to 15m.
HR_NEAR_CARD_MID_MINUTES = 15.0
HR_NORMAL_MID_MINUTES = 45.0
HR_NORMAL_FAR_MINUTES = 120.0
HR_URGENT_MINUTES = 15.0
HR_NEAR_CARD_URGENT_MINUTES = 5.0
HR_NEAR_CARD_SOFT_BYPASS_MINUTES = 180.0

_INSTALLED = False


def _dt(value: Any):
    return live_yankee_feed._parse_iso(value)


def _spent_today(db, run_type: str, now: datetime) -> int:
    try:
        row = db.fetchone(
            """SELECT COALESCE(SUM(actual_cost),0) AS n
               FROM collector_runs
               WHERE run_type=? AND started_at>=?""",
            (run_type, now.date().isoformat() + "T00:00:00+00:00"),
        ) or {}
        return int(row.get("n") or 0)
    except Exception:
        return 0


def _soft_allowance(cap: int, now: datetime) -> int:
    """Time-of-day allowance for non-urgent work; urgent events may exceed it."""
    utc = now.astimezone(timezone.utc)
    elapsed = utc.hour * 3600 + utc.minute * 60 + utc.second
    day_fraction = min(1.0, max(0.0, elapsed / 86400.0))
    fraction = SOFT_DAY_FLOOR_FRACTION + (1.0 - SOFT_DAY_FLOOR_FRACTION) * day_fraction
    return max(1, int(cap * fraction))


def _last_success(db, run_type: str, event_id: str):
    return base_policy._last_success(db, run_type, event_id)


def _due_after(last, now: datetime, minutes: float) -> bool:
    if last is None:
        return True
    return (now - last).total_seconds() / 60.0 + 1e-9 >= minutes


def _live_has_usable(db, event_id: str, now: datetime) -> bool:
    try:
        rows = db.fetchall(
            "SELECT * FROM live_yankee_candidates WHERE event_id=? AND status='QUEUED' ORDER BY id",
            (event_id,),
        )
        return any(bool(live_yankee_feed._valid_quotes(db, row, now)) for row in rows)
    except Exception:
        # Fail open on quote-state diagnostics; hard caps still protect spend.
        return True


def _live_near_card(db, event_id: str, now: datetime) -> bool:
    """Use existing stream-readiness diagnostics; never infer a new selection rule."""
    try:
        eligible_events, near_events, state = base_policy._hr_stream_readiness(db, now)
        if state.get("failed_open"):
            return False
        return event_id in eligible_events and event_id in near_events
    except Exception:
        return False


def _live_targets(db, now: datetime, targets: Sequence[Mapping[str, Any]]):
    spent = _spent_today(db, "LIVE_YANKEE_QUOTES", now)
    soft = _soft_allowance(LIVE_YANKEE_DAILY_CREDITS, now)
    kept = []

    for raw in targets:
        target = dict(raw)
        event_id = str(target.get("event_id") or "")
        kickoff = _dt(target.get("commence_time"))
        if not event_id or kickoff is None:
            continue

        minutes_to_kickoff = (kickoff - now).total_seconds() / 60.0
        urgent = minutes_to_kickoff <= URGENT_KICKOFF_MINUTES
        near = urgent and _live_near_card(db, event_id, now)
        if not urgent and spent >= soft:
            continue

        if urgent:
            wait = LIVE_NEAR_CARD_URGENT_MINUTES if near else LIVE_URGENT_MINUTES
        else:
            usable = _live_has_usable(db, event_id, now)
            far = minutes_to_kickoff > FAR_FROM_KICKOFF_MINUTES
            if usable:
                wait = LIVE_USABLE_FAR_MINUTES if far else LIVE_USABLE_MID_MINUTES
            else:
                wait = LIVE_MISS_FAR_MINUTES if far else LIVE_MISS_MID_MINUTES

        last = _last_success(db, "LIVE_YANKEE_QUOTES", event_id)
        if _due_after(last, now, wait):
            kept.append(raw)

    return kept


def _hr_targets(db, now: datetime, targets: Sequence[Mapping[str, Any]]):
    eligible_events, near_events, state = base_policy._hr_stream_readiness(db, now)
    if state.get("failed_open"):
        # Preserve prior fail-open behaviour if readiness diagnostics fail.
        return list(targets)

    spent = _spent_today(db, "HIT_RATE_MULTIPLES_QUOTES", now)
    soft = _soft_allowance(HIT_RATE_DAILY_CREDITS, now)
    kept = []

    for raw in targets:
        target = dict(raw)
        event_id = str(target.get("event_id") or "")
        kickoff = _dt(target.get("commence_time"))
        if not event_id or kickoff is None or event_id not in eligible_events:
            continue

        minutes_to_kickoff = (kickoff - now).total_seconds() / 60.0
        near = event_id in near_events
        urgent = minutes_to_kickoff <= URGENT_KICKOFF_MINUTES
        priority_window = urgent or (near and minutes_to_kickoff <= HR_NEAR_CARD_SOFT_BYPASS_MINUTES)

        # Keep the existing failure-aware retry protection.
        failure_wait = base_policy._failure_retry_wait(
            db,
            "HIT_RATE_MULTIPLES_QUOTES",
            event_id,
            now,
            priority=priority_window,
        )
        if failure_wait is not None:
            last_attempt = base_policy._last_attempt_at(db, "HIT_RATE_MULTIPLES_QUOTES", event_id)
            if last_attempt is not None and not _due_after(last_attempt, now, failure_wait):
                continue

        if not priority_window and spent >= soft:
            continue

        if urgent:
            wait = HR_NEAR_CARD_URGENT_MINUTES if near else HR_URGENT_MINUTES
        elif near:
            wait = HR_NEAR_CARD_MID_MINUTES
        else:
            wait = (
                HR_NORMAL_FAR_MINUTES
                if minutes_to_kickoff > FAR_FROM_KICKOFF_MINUTES
                else HR_NORMAL_MID_MINUTES
            )

        last = _last_success(db, "HIT_RATE_MULTIPLES_QUOTES", event_id)
        if _due_after(last, now, wait):
            kept.append(raw)

    return kept


def _today_breakdown(db, now: datetime):
    try:
        return [dict(r) for r in db.fetchall(
            """SELECT run_type,COALESCE(sport_key,'') AS sport_key,
                      COALESCE(SUM(actual_cost),0) AS credits,COUNT(*) AS runs
               FROM collector_runs
               WHERE started_at>=? AND actual_cost>0
               GROUP BY run_type,COALESCE(sport_key,'')
               ORDER BY credits DESC,run_type,sport_key""",
            (now.date().isoformat() + "T00:00:00+00:00",),
        )]
    except Exception:
        return []


def install(db) -> dict:
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    # The V5 policy has already captured the original unpatched collectors during
    # dashboard bootstrap. Reuse those originals so wrappers do not stack.
    original_live = base_policy._ORIGINAL_LY_TARGETS
    original_hr = base_policy._ORIGINAL_HRM_TARGETS
    if not callable(original_live) or not callable(original_hr):
        return {
            "installed": False,
            "version": VERSION,
            "error": "base credit policy originals unavailable",
        }

    live_yankee_feed.DAILY_REFRESH_CREDIT_BUDGET = LIVE_YANKEE_DAILY_CREDITS
    hit_rate_multiples_lab.DAILY_REFRESH_CREDIT_BUDGET = HIT_RATE_DAILY_CREDITS
    base_policy.LIVE_YANKEE_DAILY_CREDITS = LIVE_YANKEE_DAILY_CREDITS
    base_policy.HIT_RATE_DAILY_CREDITS = HIT_RATE_DAILY_CREDITS

    def live_targets(db_obj, now=None):
        stamp = now or datetime.now(timezone.utc)
        return _live_targets(db_obj, stamp, original_live(db_obj, stamp))

    def hr_targets(db_obj, now=None):
        stamp = now or datetime.now(timezone.utc)
        return _hr_targets(db_obj, stamp, original_hr(db_obj, stamp))

    live_yankee_feed._targets = live_targets
    hit_rate_multiples_lab._targets = hr_targets

    now = datetime.now(timezone.utc)
    today = _today_breakdown(db, now)
    result = {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "hard_caps": {
            "live_yankee": LIVE_YANKEE_DAILY_CREDITS,
            "hit_rate": HIT_RATE_DAILY_CREDITS,
        },
        "soft_allowance_now": {
            "live_yankee": _soft_allowance(LIVE_YANKEE_DAILY_CREDITS, now),
            "hit_rate": _soft_allowance(HIT_RATE_DAILY_CREDITS, now),
        },
        "today_paid_breakdown": today[:20],
        "live_refresh_minutes": {
            "near_card_urgent": LIVE_NEAR_CARD_URGENT_MINUTES,
            "urgent": LIVE_URGENT_MINUTES,
            "usable_mid": LIVE_USABLE_MID_MINUTES,
            "usable_far": LIVE_USABLE_FAR_MINUTES,
            "miss_mid": LIVE_MISS_MID_MINUTES,
            "miss_far": LIVE_MISS_FAR_MINUTES,
        },
        "hr_refresh_minutes": {
            "near_card_urgent": HR_NEAR_CARD_URGENT_MINUTES,
            "urgent": HR_URGENT_MINUTES,
            "near_card_mid": HR_NEAR_CARD_MID_MINUTES,
            "normal_mid": HR_NORMAL_MID_MINUTES,
            "normal_far": HR_NORMAL_FAR_MINUTES,
        },
        "selection_rules_changed": False,
        "same_book_rules_changed": False,
        "betting_authority_changed": False,
    }
    _INSTALLED = True
    return result
