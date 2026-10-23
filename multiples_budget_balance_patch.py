from __future__ import annotations

"""Set current paid multiples quote budgets after expired temporary boosts.

Live Yankee remains at 500 credits/day. Hit-Rate multiples receives 700/day so
its larger same-book HR1/HR2 Yankee/Heinz queue has more room to refresh stale or
missing prices. Existing per-event pacing/backoff, urgency, same-book, price,
selection and card-construction rules remain authoritative.

This patch also neutralises the historical Oct-20 reporting shim that otherwise
resets HR's cap to 400 on every refresh. No betting authority is added.
"""

from datetime import datetime, timezone
from typing import Any, Dict

import credit_policy_unlimited_patch as base_policy
import hit_rate_daily_boost_patch as hr_daily_boost
import hit_rate_multiples_lab
import live_yankee_feed
import multiples_credit_efficiency_patch as efficiency
import multiples_today_headroom_patch as legacy_headroom

VERSION = "MULTIPLES_BUDGET_BALANCE_2026_10_23_V2_HR700"
LIVE_YANKEE_DAILY_CREDITS = 500
HIT_RATE_DAILY_CREDITS = 700
_INSTALLED = False


def _spent_today(db, run_type: str) -> int:
    now = datetime.now(timezone.utc)
    try:
        row = db.fetchone(
            """SELECT COALESCE(SUM(actual_cost),0) AS n
               FROM collector_runs
               WHERE run_type=? AND started_at>=?""",
            (run_type, now.date().isoformat() + "T00:00:00+00:00",),
        ) or {}
        return int(row.get("n") or 0)
    except Exception:
        return 0


def _hr_cap(_now=None) -> int:
    """Authoritative current HR cap, including through legacy refresh wrappers."""
    return HIT_RATE_DAILY_CREDITS


def install(db) -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
            "live_yankee_daily_cap": LIVE_YANKEE_DAILY_CREDITS,
            "hit_rate_daily_cap": HIT_RATE_DAILY_CREDITS,
        }

    live_cap = LIVE_YANKEE_DAILY_CREDITS
    hr_cap = HIT_RATE_DAILY_CREDITS

    # Collector-visible hard caps.
    live_yankee_feed.DAILY_REFRESH_CREDIT_BUDGET = live_cap
    hit_rate_multiples_lab.DAILY_REFRESH_CREDIT_BUDGET = hr_cap

    # Base/efficiency policy globals used dynamically by the existing wrappers.
    base_policy.LIVE_YANKEE_DAILY_CREDITS = live_cap
    base_policy.HIT_RATE_DAILY_CREDITS = hr_cap
    efficiency.LIVE_YANKEE_DAILY_CREDITS = live_cap
    efficiency.HIT_RATE_DAILY_CREDITS = hr_cap

    # The retired 19-Oct wrapper still surrounds HR._refresh. Oct-20 reporting
    # replaced its _effective_cap function with a fixed post-20-Oct 400 result,
    # which means merely changing constants is insufficient: every refresh would
    # put the collector back to 400. Replace the function itself with the current
    # authority so 700 survives every polling cycle.
    hr_daily_boost.NORMAL_DAILY_CAP = hr_cap
    hr_daily_boost.BOOST_DAILY_CAP = hr_cap
    hr_daily_boost._effective_cap = _hr_cap

    # Neutralise the expired 20-Oct temporary ceilings while preserving its
    # wrapper shape. The live and HR paths now resolve to today's intended caps.
    legacy_headroom.NORMAL_LIVE_YANKEE_CAP = live_cap
    legacy_headroom.NORMAL_HIT_RATE_CAP = hr_cap
    legacy_headroom.BOOST_LIVE_YANKEE_CAP = live_cap
    legacy_headroom.BOOST_HIT_RATE_CAP = hr_cap

    now = datetime.now(timezone.utc)
    live_soft = efficiency._soft_allowance(live_cap, now)
    hr_soft = efficiency._soft_allowance(hr_cap, now)
    live_spent = _spent_today(db, "LIVE_YANKEE_QUOTES")
    hr_spent = _spent_today(db, "HIT_RATE_MULTIPLES_QUOTES")

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "daily_caps": {
            "live_yankee": live_cap,
            "hit_rate": hr_cap,
        },
        "soft_allowance_now": {
            "live_yankee": live_soft,
            "hit_rate": hr_soft,
        },
        "spent_today": {
            "live_yankee": live_spent,
            "hit_rate": hr_spent,
        },
        "remaining_headroom": {
            "live_yankee": max(0, live_cap - live_spent),
            "hit_rate": max(0, hr_cap - hr_spent),
        },
        "legacy_oct19_hr_boost_neutralized": True,
        "legacy_oct20_hr_400_reset_neutralized": True,
        "legacy_oct20_headroom_neutralized": True,
        "same_book_rules_changed": False,
        "selection_rules_changed": False,
        "price_rules_changed": False,
        "event_backoff_changed": False,
        "betting_authority_changed": False,
    }
