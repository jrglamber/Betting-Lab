from __future__ import annotations

"""Prioritise Hit-Rate quote refreshes where they can form a card soonest.

The existing HR target selector already controls eligibility, paid-credit pacing,
failed-call backoff and refresh cadence. The remaining issue is ordering: the
base collector slices the first MAX_REFRESH_EVENTS_PER_CYCLE targets, while the
wrapped target list historically retained first-seen ordering. On a busy slate,
older distant fixtures could therefore occupy the cycle before an imminent or
near-complete card.

This patch changes ordering only. It does not change source selections, odds
bands, minimum prices, same-book requirements, system construction, staking,
settlement, alerts, daily provider-credit caps or betting authority.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Mapping

import credit_policy_unlimited_patch as credit_policy
import hit_rate_multiples_lab as hr
import live_yankee_feed as ly

VERSION = "HR_JIT_QUOTE_PRIORITY_2026_11_01_V1"
NEAR_CARD_PRIORITY_MINUTES = 180.0
URGENT_KICKOFF_MINUTES = 90.0
MID_KICKOFF_MINUTES = 240.0

_INSTALLED = False
_ORIGINAL_TARGETS = None


def _dt(value: Any):
    return ly._parse_iso(value)


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

    # Tier 0: near-complete card and inside the final 90 minutes.
    # Tier 1: any other event inside the final 90 minutes.
    # Tier 2: near-complete card inside three hours.
    # Tier 3: ordinary mid-window event (<=4h).
    # Tier 4: distant research snapshot.
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

    # Within the same tier favour sooner kickoffs, then the stalest/missing
    # successful quote. first_seen/event_id provide deterministic tie-breakers.
    age = _last_success_age_minutes(db, event_id, now)
    return (
        tier,
        minutes,
        -age,
        str(target.get("first_seen_at") or ""),
        event_id,
    )


def install() -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_TARGETS
    if _INSTALLED:
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
        }

    _ORIGINAL_TARGETS = hr._targets

    def prioritised_targets(db, now=None):
        stamp = now or datetime.now(timezone.utc)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        else:
            stamp = stamp.astimezone(timezone.utc)

        targets = list(_ORIGINAL_TARGETS(db, stamp))
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
        "daily_credit_cap": int(hr.DAILY_REFRESH_CREDIT_BUDGET),
        "selection_rules_changed": False,
        "price_rules_changed": False,
        "same_book_rules_changed": False,
        "card_rules_changed": False,
        "provider_cap_changed": False,
        "betting_authority_changed": False,
    }


__all__ = ["VERSION", "install"]
