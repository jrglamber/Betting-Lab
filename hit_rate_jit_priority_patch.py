from __future__ import annotations

"""Prioritise HR quote refreshes and protect the late-day quote reserve.

Monday–Thursday (UTC): normal HR quote allowance 2,500 credits/day with a
protected final 500-credit reserve, up to a hard ceiling of 3,000.
Friday–Sunday (UTC): 5,000 credits/day for HR quote polling, available to
ordinary due targets without removing existing pacing/backoff safeguards.
The weekend limit is an upper bound, not a daily spend target.

The reserve uses quotes already stored in ``live_yankee_quotes``. That table is
shared with Live Yankee, so overlapping quotes collected by either lane continue
to be reused without an additional provider call. A 60-minute recent-quote
window is used only to remember near-card state after the normal 15-minute quote
freshness window expires; actual card construction still uses the unchanged
strict freshness and price rules in ``hit_rate_multiples_lab``.

Source selections, odds bands, minimum prices, same-book requirements, system
construction, staking, settlement, alerts and betting authority are unchanged.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping

import credit_policy_unlimited_patch as credit_policy
import hit_rate_daily_boost_patch as hr_daily_boost
import hit_rate_multiples_lab as hr
import live_yankee_feed as ly
import multiples_budget_balance_patch as budget_balance
import multiples_credit_efficiency_patch as efficiency
import multiples_today_headroom_patch as legacy_headroom
import provider_cost_control_patch as provider_cost

VERSION = "HR_JIT_QUOTE_PRIORITY_2026_11_07_V4_WEEKEND_5000"
NEAR_CARD_PRIORITY_MINUTES = 180.0
URGENT_KICKOFF_MINUTES = 90.0
MID_KICKOFF_MINUTES = 240.0

BASE_DAILY_CREDITS = 2_500
BURST_DAILY_CREDITS = 3_000
WEEKEND_DAILY_CREDITS = 5_000
WEEKEND_DAYS_UTC = frozenset((4, 5, 6))  # Friday, Saturday, Sunday
PROTECTED_RESERVE_CREDITS = BURST_DAILY_CREDITS - BASE_DAILY_CREDITS
RESERVE_RECENT_QUOTE_MINUTES = 60.0
RESERVE_DISCOVERY_EVENTS_PER_STREAM = 2
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


def _burst_allowed(db, now: datetime, reserve_target_count: int) -> tuple[bool, float | None]:
    """Fail closed unless there is protected work and provider headroom."""
    if reserve_target_count <= 0:
        return False, _provider_projection_30d(db, now)
    projection = _provider_projection_30d(db, now)
    if projection is None:
        return False, None
    return projection + PROTECTED_RESERVE_CREDITS <= PROVIDER_MONTHLY_ALLOWANCE, projection


def _weekend_utc(now=None) -> bool:
    """The Odds API spending ledger resets at 00:00 UTC."""
    return _utc(now).weekday() in WEEKEND_DAYS_UTC


def _effective_base_cap(now=None) -> int:
    return WEEKEND_DAILY_CREDITS if _weekend_utc(now) else BASE_DAILY_CREDITS


def _burst_hard_cap(now=None) -> int:
    """Collector hard cap: 5,000 Fri–Sun UTC; 3,000 Mon–Thu UTC."""
    return WEEKEND_DAILY_CREDITS if _weekend_utc(now) else BURST_DAILY_CREDITS


def _sync_runtime_caps(now=None) -> int:
    """Synchronise historic wrappers before due targets and collector checks.

    Old target wrappers have their own caps and may otherwise suppress weekend
    events before the final HR priority layer ever sees them.
    """
    hard = _burst_hard_cap(now)
    hr.DAILY_REFRESH_CREDIT_BUDGET = hard
    credit_policy.HIT_RATE_DAILY_CREDITS = hard
    efficiency.HIT_RATE_DAILY_CREDITS = hard
    hr_daily_boost.NORMAL_DAILY_CAP = hard
    hr_daily_boost.BOOST_DAILY_CAP = hard
    hr_daily_boost._effective_cap = _burst_hard_cap
    legacy_headroom.NORMAL_HIT_RATE_CAP = hard
    legacy_headroom.BOOST_HIT_RATE_CAP = hard
    budget_balance.HIT_RATE_DAILY_CREDITS = hard
    provider_cost.HR_DAILY_CREDIT_CEILING = hard
    return hard


def _valid_quotes_for_window(db, candidate: Mapping[str, Any], now: datetime, window_minutes: float):
    """Return strict valid quotes, allowing a wider window only for reserve memory.

    This deliberately mirrors the strict parts of hr._quotes. It does not relax
    price bands, minimum odds, bookmaker eligibility, point matching or the
    candidate's first-seen boundary.
    """
    try:
        rows = db.fetchall(
            """SELECT * FROM live_yankee_quotes
               WHERE event_id=? AND market_key=? AND selection=?
               ORDER BY captured_at DESC,id DESC LIMIT 500""",
            (candidate["event_id"], candidate["market_key"], candidate["selection"]),
        )
    except Exception:
        return {}

    cutoff = now - timedelta(minutes=float(window_minutes))
    first_seen = _dt(candidate.get("first_seen_at"))
    out: Dict[str, Dict[str, Any]] = {}
    for raw in rows:
        q = dict(raw)
        book = str(q.get("bookmaker_key") or "")
        if book in out or book not in hr.DEFAULT_PLACEABLE_BOOKS:
            continue
        if not ly._same_point(q.get("point"), candidate.get("point")):
            continue
        ts = _dt(q.get("captured_at"))
        if ts is None or ts < cutoff or ts > now or (first_seen and ts < first_seen):
            continue
        try:
            price = float(q.get("price") or 0)
            band_min = float(candidate.get("band_min") or 0)
            band_max = float(candidate.get("band_max") or 0)
            min_odds = float(candidate.get("min_odds") or 0)
        except Exception:
            continue
        if not (band_min <= price < band_max):
            continue
        if price + 1e-12 < min_odds:
            continue
        q["quote_age_minutes"] = max(0.0, (now - ts).total_seconds() / 60.0)
        out[book] = q
    return out


def _queued_streams(db, now: datetime):
    """Ordered, one-candidate-per-event view of current HR streams."""
    try:
        rows = [dict(x) for x in db.fetchall(
            """SELECT * FROM hit_rate_candidates
               WHERE status='QUEUED' ORDER BY first_seen_at,id"""
        )]
    except Exception:
        return {}

    streams: Dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    seen: Dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for candidate in rows:
        if not hr._actionable(candidate.get("commence_time"), now):
            continue
        key = (
            str(candidate.get("policy_version") or ""),
            str(candidate.get("experiment_key") or ""),
            str(candidate.get("system_type") or ""),
        )
        event_id = str(candidate.get("event_id") or "")
        if not event_id or event_id in seen[key]:
            continue
        seen[key].add(event_id)
        streams[key].append(candidate)
    return streams


def _protected_reserve_plan(db, now: datetime) -> Dict[str, Dict[str, Any]]:
    """Identify event polls capable of reviving/completing a recently near card.

    A stream qualifies only if one bookmaker covered at least ``legs - 1``
    distinct candidate events within the last hour. Existing fresh coverage is
    preferred. Stale members of that recent same-book cluster are refresh
    targets; if the cluster itself has only ``legs - 1`` events, at most two
    additional candidate events are admitted as discovery attempts for the final
    leg. Ordinary backlog never enters the protected reserve.
    """
    plan: Dict[str, Dict[str, Any]] = {}
    streams = _queued_streams(db, now)

    for (policy, experiment, system), candidates in streams.items():
        spec = hr.SYSTEMS.get(system) or {}
        try:
            need = int(spec.get("legs") or 0)
        except Exception:
            need = 0
        if need < 2 or len(candidates) < need:
            continue

        recent_by_event: Dict[str, Dict[str, Dict[str, Any]]] = {}
        fresh_by_event: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for candidate in candidates:
            event_id = str(candidate.get("event_id") or "")
            recent_by_event[event_id] = _valid_quotes_for_window(
                db, candidate, now, RESERVE_RECENT_QUOTE_MINUTES
            )
            fresh_by_event[event_id] = _valid_quotes_for_window(
                db, candidate, now, float(hr.QUOTE_FRESHNESS_MINUTES)
            )

        books = set()
        for quote_map in recent_by_event.values():
            books.update(quote_map.keys())

        for book in books:
            recent_events = [
                str(c.get("event_id") or "")
                for c in candidates
                if book in recent_by_event.get(str(c.get("event_id") or ""), {})
            ]
            if len(recent_events) < need - 1:
                continue

            fresh_events = {
                str(c.get("event_id") or "")
                for c in candidates
                if book in fresh_by_event.get(str(c.get("event_id") or ""), {})
            }
            if len(fresh_events) >= need:
                # This bookmaker is already card-complete; formation, not polling,
                # should consume it.
                continue

            recent_ranked = recent_events[:need]
            reserve_events = [eid for eid in recent_ranked if eid not in fresh_events]

            # If the recent same-book cluster itself is one leg short, allow only
            # a tiny number of discovery attempts for the missing leg.
            if len(recent_events) == need - 1:
                discovery = [
                    str(c.get("event_id") or "")
                    for c in candidates
                    if str(c.get("event_id") or "") not in set(recent_events)
                    and str(c.get("event_id") or "") not in fresh_events
                ][:RESERVE_DISCOVERY_EVENTS_PER_STREAM]
                reserve_events.extend(discovery)

            if not reserve_events:
                continue

            fresh_count = len(fresh_events)
            recent_count = len(recent_events)
            for event_id in reserve_events:
                entry = plan.setdefault(event_id, {
                    "event_id": event_id,
                    "card_yield": 0,
                    "best_fresh_count": 0,
                    "best_recent_count": 0,
                    "best_need": need,
                    "streams": [],
                })
                entry["card_yield"] = int(entry.get("card_yield") or 0) + 1
                if fresh_count > int(entry.get("best_fresh_count") or 0):
                    entry["best_fresh_count"] = fresh_count
                if recent_count > int(entry.get("best_recent_count") or 0):
                    entry["best_recent_count"] = recent_count
                entry["best_need"] = min(int(entry.get("best_need") or need), need)
                entry["streams"].append({
                    "policy": policy,
                    "experiment": experiment,
                    "system": system,
                    "bookmaker": book,
                    "fresh": fresh_count,
                    "recent": recent_count,
                    "need": need,
                })

    return plan


def _reserve_priority_key(db, target: Mapping[str, Any], now: datetime, meta: Mapping[str, Any]):
    minutes = _minutes_to_kickoff(target, now)
    need = int(meta.get("best_need") or 99)
    fresh = int(meta.get("best_fresh_count") or 0)
    recent = int(meta.get("best_recent_count") or 0)
    deficit = max(0, need - fresh)
    recent_deficit = max(0, need - recent)
    yield_count = int(meta.get("card_yield") or 0)
    age = _last_success_age_minutes(db, str(target.get("event_id") or ""), now)
    return (
        recent_deficit,
        deficit,
        -yield_count,
        0 if minutes <= URGENT_KICKOFF_MINUTES else 1,
        minutes,
        -age,
        str(target.get("first_seen_at") or ""),
        str(target.get("event_id") or ""),
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

    # Match the legacy wrapper caps to today's UTC spending window.
    # This refreshes again on every target pass when the day changes.
    _sync_runtime_caps(_utc())

    def prioritised_targets(db, now=None):
        stamp = _utc(now)
        _sync_runtime_caps(stamp)
        targets = list(_ORIGINAL_TARGETS(db, stamp))

        try:
            spent = int(hr._credits_today(db, stamp))
        except Exception:
            spent = _effective_base_cap(stamp)

        if spent >= _burst_hard_cap(stamp):
            return []

        if spent >= _effective_base_cap(stamp):
            reserve_plan = _protected_reserve_plan(db, stamp)
            targets = [
                target for target in targets
                if str(target.get("event_id") or "") in reserve_plan
            ]
            allowed, _ = _burst_allowed(db, stamp, len(targets))
            if not allowed:
                return []
            return sorted(
                targets,
                key=lambda target: _reserve_priority_key(
                    db,
                    target,
                    stamp,
                    reserve_plan.get(str(target.get("event_id") or ""), {}),
                ),
            )

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
        "weekend_utc_days": ["Friday", "Saturday", "Sunday"],
        "weekend_daily_credit_cap": WEEKEND_DAILY_CREDITS,
        "effective_today_base_cap": _effective_base_cap(),
        "effective_today_hard_cap": _burst_hard_cap(),
        "protected_reserve_credits": PROTECTED_RESERVE_CREDITS,
        "weekend_protected_reserve_credits": 0,
        "reserve_recent_quote_minutes": RESERVE_RECENT_QUOTE_MINUTES,
        "reserve_discovery_events_per_stream": RESERVE_DISCOVERY_EVENTS_PER_STREAM,
        "provider_monthly_allowance": PROVIDER_MONTHLY_ALLOWANCE,
        "provider_projection_lookback_days": PROVIDER_LOOKBACK_DAYS,
        "shared_quote_table": "live_yankee_quotes",
        "selection_rules_changed": False,
        "price_rules_changed": False,
        "same_book_rules_changed": False,
        "card_rules_changed": False,
        "quote_budget_policy_changed": True,
        "betting_authority_changed": False,
    }


__all__ = ["VERSION", "install"]
