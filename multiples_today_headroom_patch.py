from __future__ import annotations

"""Temporary paid-quote headroom for the 2026-10-20 evening football slate.

This runs after MULTIPLES_CREDIT_EFFICIENCY_V1. It does not alter selection,
pricing, same-book, card construction or betting authority. The extra allowance
exists only on 2026-10-20 UTC; subsequent target cycles enforce the normal caps.
Per-event pacing/backoff from the efficiency patch remains in force.
"""

from datetime import datetime, timezone

import credit_policy_unlimited_patch as base_policy
import hit_rate_multiples_lab
import live_yankee_feed
import multiples_credit_efficiency_patch as efficiency

VERSION = "MULTIPLES_TODAY_HEADROOM_2026_10_20_V1"
BOOST_DATE_UTC = "2026-10-20"
NORMAL_LIVE_YANKEE_CAP = 1_200
NORMAL_HIT_RATE_CAP = 400
BOOST_LIVE_YANKEE_CAP = 1_650
BOOST_HIT_RATE_CAP = 650
AUTO_REVERT_AT = "2026-10-21T00:00:00Z"

_INSTALLED = False
_ORIGINAL_LIVE_TARGETS = None
_ORIGINAL_HR_TARGETS = None
_ORIGINAL_SOFT_ALLOWANCE = None


def _is_boost_day(now: datetime) -> bool:
    return now.astimezone(timezone.utc).date().isoformat() == BOOST_DATE_UTC


def _effective_caps(now: datetime) -> tuple[int, int]:
    if _is_boost_day(now):
        return BOOST_LIVE_YANKEE_CAP, BOOST_HIT_RATE_CAP
    return NORMAL_LIVE_YANKEE_CAP, NORMAL_HIT_RATE_CAP


def _apply_provider_caps(live_cap: int, hr_cap: int) -> None:
    # The underlying collectors check these before our target wrappers run. Keep
    # them at the maximum temporary ceiling; the dynamic guards below enforce the
    # lower normal caps again automatically after the boost date.
    efficiency.LIVE_YANKEE_DAILY_CREDITS = BOOST_LIVE_YANKEE_CAP
    efficiency.HIT_RATE_DAILY_CREDITS = BOOST_HIT_RATE_CAP
    live_yankee_feed.DAILY_REFRESH_CREDIT_BUDGET = BOOST_LIVE_YANKEE_CAP
    hit_rate_multiples_lab.DAILY_REFRESH_CREDIT_BUDGET = BOOST_HIT_RATE_CAP
    base_policy.LIVE_YANKEE_DAILY_CREDITS = BOOST_LIVE_YANKEE_CAP
    base_policy.HIT_RATE_DAILY_CREDITS = BOOST_HIT_RATE_CAP


def install(db) -> dict:
    global _INSTALLED, _ORIGINAL_LIVE_TARGETS, _ORIGINAL_HR_TARGETS, _ORIGINAL_SOFT_ALLOWANCE
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    _ORIGINAL_LIVE_TARGETS = efficiency._live_targets
    _ORIGINAL_HR_TARGETS = efficiency._hr_targets
    _ORIGINAL_SOFT_ALLOWANCE = efficiency._soft_allowance

    if not all(callable(fn) for fn in (_ORIGINAL_LIVE_TARGETS, _ORIGINAL_HR_TARGETS, _ORIGINAL_SOFT_ALLOWANCE)):
        return {"installed": False, "version": VERSION, "error": "efficiency patch hooks unavailable"}

    # Allow the base collectors to reach today's temporary ceiling. Dynamic
    # wrappers below remain the final hard-stop authority and revert by UTC date.
    _apply_provider_caps(BOOST_LIVE_YANKEE_CAP, BOOST_HIT_RATE_CAP)

    def soft_allowance(cap: int, now: datetime) -> int:
        if _is_boost_day(now):
            # Today only, make the bounded extra headroom actually usable. The
            # event-level 5/15/20/45/120-minute pacing remains unchanged.
            if cap == BOOST_LIVE_YANKEE_CAP:
                return BOOST_LIVE_YANKEE_CAP
            if cap == BOOST_HIT_RATE_CAP:
                return BOOST_HIT_RATE_CAP
            return cap

        normal_cap = cap
        if cap == BOOST_LIVE_YANKEE_CAP:
            normal_cap = NORMAL_LIVE_YANKEE_CAP
        elif cap == BOOST_HIT_RATE_CAP:
            normal_cap = NORMAL_HIT_RATE_CAP
        return _ORIGINAL_SOFT_ALLOWANCE(normal_cap, now)

    def live_targets(db_obj, now: datetime, targets):
        live_cap, _ = _effective_caps(now)
        spent = efficiency._spent_today(db_obj, "LIVE_YANKEE_QUOTES", now)
        if spent >= live_cap:
            return []
        return _ORIGINAL_LIVE_TARGETS(db_obj, now, targets)

    def hr_targets(db_obj, now: datetime, targets):
        _, hr_cap = _effective_caps(now)
        spent = efficiency._spent_today(db_obj, "HIT_RATE_MULTIPLES_QUOTES", now)
        if spent >= hr_cap:
            return []
        return _ORIGINAL_HR_TARGETS(db_obj, now, targets)

    efficiency._soft_allowance = soft_allowance
    efficiency._live_targets = live_targets
    efficiency._hr_targets = hr_targets

    now = datetime.now(timezone.utc)
    live_cap, hr_cap = _effective_caps(now)
    live_spent = efficiency._spent_today(db, "LIVE_YANKEE_QUOTES", now)
    hr_spent = efficiency._spent_today(db, "HIT_RATE_MULTIPLES_QUOTES", now)

    result = {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "boost_active": _is_boost_day(now),
        "effective_caps": {"live_yankee": live_cap, "hit_rate": hr_cap},
        "normal_caps": {"live_yankee": NORMAL_LIVE_YANKEE_CAP, "hit_rate": NORMAL_HIT_RATE_CAP},
        "spent_today": {"live_yankee": live_spent, "hit_rate": hr_spent},
        "remaining_headroom": {
            "live_yankee": max(0, live_cap - live_spent),
            "hit_rate": max(0, hr_cap - hr_spent),
        },
        "automatic_revert_at": AUTO_REVERT_AT,
        "soft_pacing_bypass_today_only": True,
        "event_refresh_cadence_changed": False,
        "selection_rules_changed": False,
        "same_book_rules_changed": False,
        "betting_authority_changed": False,
    }
    _INSTALLED = True
    return result
