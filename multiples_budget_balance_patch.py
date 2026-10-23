from __future__ import annotations

"""Balance the two paid multiples quote lanes after expired temporary boosts.

By 23 Oct the observed spend rates are similar, but legacy one-day headroom code
still leaves Live Yankee advertising a much larger provider ceiling than HR.
This final runtime patch gives Live Yankee and Hit-Rate multiples the same daily
hard cap and therefore the same time-of-day soft allowance. Existing per-event
backoff, urgency, same-book, price, selection and card-construction rules remain
authoritative.

No betting authority is added and no selection rule changes.
"""

from datetime import datetime, timezone
from typing import Any, Dict

import credit_policy_unlimited_patch as base_policy
import hit_rate_daily_boost_patch as hr_daily_boost
import hit_rate_multiples_lab
import live_yankee_feed
import multiples_credit_efficiency_patch as efficiency
import multiples_today_headroom_patch as legacy_headroom

VERSION = "MULTIPLES_BUDGET_BALANCE_2026_10_23_V1"
BALANCED_DAILY_CREDITS = 500
_INSTALLED = False


def _spent_today(db, run_type: str) -> int:
    now = datetime.now(timezone.utc)
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


def install(db) -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
            "daily_cap_each": BALANCED_DAILY_CREDITS,
        }

    cap = BALANCED_DAILY_CREDITS

    # Collector-visible hard caps.
    live_yankee_feed.DAILY_REFRESH_CREDIT_BUDGET = cap
    hit_rate_multiples_lab.DAILY_REFRESH_CREDIT_BUDGET = cap

    # Base/efficiency policy globals used dynamically by the existing wrappers.
    base_policy.LIVE_YANKEE_DAILY_CREDITS = cap
    base_policy.HIT_RATE_DAILY_CREDITS = cap
    efficiency.LIVE_YANKEE_DAILY_CREDITS = cap
    efficiency.HIT_RATE_DAILY_CREDITS = cap

    # Neutralise the expired 19/20-Oct temporary ceilings. Their installed
    # wrappers resolve these globals at runtime, so updating them here makes the
    # already-wrapped collectors enforce the balanced cap without wrapper stacks.
    hr_daily_boost.NORMAL_DAILY_CAP = cap
    hr_daily_boost.BOOST_DAILY_CAP = cap
    legacy_headroom.NORMAL_LIVE_YANKEE_CAP = cap
    legacy_headroom.NORMAL_HIT_RATE_CAP = cap
    legacy_headroom.BOOST_LIVE_YANKEE_CAP = cap
    legacy_headroom.BOOST_HIT_RATE_CAP = cap

    now = datetime.now(timezone.utc)
    soft_each = efficiency._soft_allowance(cap, now)
    live_spent = _spent_today(db, "LIVE_YANKEE_QUOTES")
    hr_spent = _spent_today(db, "HIT_RATE_MULTIPLES_QUOTES")

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "daily_cap_each": cap,
        "soft_allowance_each_now": soft_each,
        "spent_today": {
            "live_yankee": live_spent,
            "hit_rate": hr_spent,
        },
        "remaining_headroom": {
            "live_yankee": max(0, cap - live_spent),
            "hit_rate": max(0, cap - hr_spent),
        },
        "legacy_oct19_hr_boost_neutralized": True,
        "legacy_oct20_headroom_neutralized": True,
        "same_book_rules_changed": False,
        "selection_rules_changed": False,
        "price_rules_changed": False,
        "event_backoff_changed": False,
        "betting_authority_changed": False,
    }
