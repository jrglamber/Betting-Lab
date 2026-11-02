from __future__ import annotations

"""Prioritise HR quote refreshes and permit a guarded busy-day burst.

The normal HR quote allowance remains 2,500 credits/day. Once that base is fully
used, the lane may consume up to 500 additional credits only when at least one
full refresh cycle of already-due targets remains and the previous seven
completed UTC days still project within the 100k monthly provider allowance.

Beyond the base allowance, existing pacing continues to reserve calls for urgent
or near-card work. Source selections, odds bands, minimum prices, same-book
requirements, system construction, staking, settlement, alerts and betting
authority are unchanged.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping

import credit_policy_unlimited_patch as credit_policy
import hit_rate_daily_boost_patch as hr_daily_boost
import hit_rate_multiples_lab as hr
import live_yankee_feed as ly
import multiples_budget_balance_patch as budget_balance
import multiples_today_headroom_patch as legacy_headroom
import provider_cost_control_patch as provider_cost

VERSION = "HR_JIT_QUOTE_PRIORITY_2026_11_02_V2_BUSY_BURST"
NEAR_CARD_PRIORITY_MINUTES = 180.0
URGENT_KICKOFF_MINUTES = 90.0
MID_KICKOFF_MINUTES = 240.0

BASE_DAILY_CREDITS = 2_500
BURST_DAILY_CREDITS = 3_000
BURST_MIN_DUE_EVENTS = 8
PROVIDER_MONTHLY_ALLOWANCE = 100_000
PROVIDER_LOOKBACK_DAYS = 7
PROVIDER_PROJECTION_DAYS = 30

_INSTALLED = False
_ORIGINAL_TARGETS = None


def _dt(value: Any):
    return ly._parse_iso(value)


def _utc(now=None) -> datetime:
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _minutes_to_kickoff(target: Mapping[str, Any], now: datetime) -> float:
    kickoff = _dt(target.get("commence_time"))
    if kickoff is None:
        return 10**9
    return max(0.0, (kickoff - now).total_seconds() / 60.0)


def _last_success_age_minutes(db, event_id: str, now: datetime) -> float:
    try:
        last = credit_policy._last_success(db, "HIT_RATE_MULTIPLES_QUOTES", event_id)
    except Exception:
        last = None
    if last is None:
        return 10**9
    return max(0.0, (now - last).total_seconds() / 60.0)


def _priority_key(db, target: Mapping[str, Any], now: datetime, near_events: set[str]):
    event_id = str(target.get("event_id") or "")
    minutes = _minutes_to_kickoff(target, now)
    near = event_id in near_events

    if near and minutes <= URGENT_KICKOFF_MINUTES:
        tier = 0
    elif minutes <= URGENT_KICKOFF_MINUTES:
        tier = 1
    elif near and minutes <= NEAR_CARD_PRIORITY_MINUTES:
        tier = 2
    elif minutes <= MID_KICKOFF_MINUTES:
        tier = 3
    else:
        tier = 4

    age = _last_success_age_minutes(db, event_id, now)
    return (
        tier,
        minutes,
        -age,
        str(target.get("first_seen_at") or ""),
        event_id,
    )


def _provider_projection_30d(db, now: datetime) -> float | None:
    """Project 30-day spend from the previous seven completed UTC days."""
    end_day = now.date()
    start_day = end_day - timedelta(days=PROVIDER_LOOKBACK_DAYS)
    try:
        rows = db.fetchall(
            """SELECT substr(started_at,1,10) AS day,
                      COALESCE(SUM(actual_cost),0) AS credits
               FROM collector_runs
               WHERE actual_cost>0
                 AND substr(started_at,1,10)>=?
                 AND substr(started_at,1,10)<?
               GROUP BY substr(started_at,1,10)""",
            (start_day.isoformat(), end_day.isoformat()),
        )
        total = sum(int((row or {}).get("credits") or 0) for row in rows)
        return (total / float(PROVIDER_LOOKBACK_DAYS)) * float(PROVIDER_PROJECTION_DAYS)
    except Exception:
        return None


def _burst_allowed(db, now: datetime, due_count: int) -> tuple[bool, float | None]:
    """Fail closed for the optional burst if spend diagnostics are unavailable."""
    if due_count < BURST_MIN_DUE_EVENTS:
        return False, _provider_projection_30d(db, now)
    projection = _provider_projection_30d(db, now)
    if projection is None:
        return False, None
    extra = BURST_DAILY_CREDITS - BASE_DAILY_CREDITS
    return projection + extra <= PROVIDER_MONTHLY_ALLOWANCE, projection


def _burst_hard_cap(_now=None) -> int:
    """Ceiling only; target gating below decides whether the extra 500 is usable."""
    return BURST_DAILY_CREDITS


def install() -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_TARGETS
    if _INSTALLED:
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
        }

    _ORIGINAL_TARGETS = hr._targets

    # The retired Oct wrappers still sit in the call chain. Open their hard
    # ceilings to 3,000 so they cannot suppress the due list before this patch
    # applies the live conditional gate. The normal efficiency soft pacing stays
    # at 2,500, so burst credits naturally remain focused on urgent/near-card work.
    hr.DAILY_REFRESH_CREDIT_BUDGET = BURST_DAILY_CREDITS
    hr_daily_boost.NORMAL_DAILY_CAP = BASE_DAILY_CREDITS
    hr_daily_boost.BOOST_DAILY_CAP = BURST_DAILY_CREDITS
    hr_daily_boost._effective_cap = _burst_hard_cap
    legacy_headroom.NORMAL_HIT_RATE_CAP = BURST_DAILY_CREDITS
    legacy_headroom.BOOST_HIT_RATE_CAP = BURST_DAILY_CREDITS
    budget_balance.HIT_RATE_DAILY_CREDITS = BURST_DAILY_CREDITS
    provider_cost.HR_DAILY_CREDIT_CEILING = BURST_DAILY_CREDITS

    def prioritised_targets(db, now=None):
        stamp = _utc(now)
        targets = list(_ORIGINAL_TARGETS(db, stamp))

        # Once the normal 2,500 credits are spent, expose further targets only
        # when a meaningful due backlog remains and provider-budget headroom is
        # still healthy. Otherwise the effective behaviour remains a 2,500 cap.
        try:
            spent = int(hr._credits_today(db, stamp))
        except Exception:
            spent = BASE_DAILY_CREDITS

        if spent >= BASE_DAILY_CREDITS:
            allowed, _ = _burst_allowed(db, stamp, len(targets))
            if not allowed:
                return []

        try:
            _, near_events, state = credit_policy._hr_stream_readiness(db, stamp)
            if state.get("failed_open"):
                near_events = set()
        except Exception:
            near_events = set()

        return sorted(
            targets,
            key=lambda target: _priority_key(db, target, stamp, near_events),
        )

    hr._targets = prioritised_targets
    _INSTALLED = True

    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "urgent_kickoff_minutes": URGENT_KICKOFF_MINUTES,
        "near_card_priority_minutes": NEAR_CARD_PRIORITY_MINUTES,
        "mid_kickoff_minutes": MID_KICKOFF_MINUTES,
        "max_refresh_events_per_cycle": int(hr.MAX_REFRESH_EVENTS_PER_CYCLE),
        "base_daily_credit_cap": BASE_DAILY_CREDITS,
        "busy_day_credit_ceiling": BURST_DAILY_CREDITS,
        "busy_day_min_due_events": BURST_MIN_DUE_EVENTS,
        "provider_monthly_allowance": PROVIDER_MONTHLY_ALLOWANCE,
        "provider_projection_lookback_days": PROVIDER_LOOKBACK_DAYS,
        "selection_rules_changed": False,
        "price_rules_changed": False,
        "same_book_rules_changed": False,
        "card_rules_changed": False,
        "quote_budget_policy_changed": True,
        "betting_authority_changed": False,
    }


__all__ = ["VERSION", "install"]
