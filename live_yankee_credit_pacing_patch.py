from __future__ import annotations

"""Pace Live Yankee quote credits as a low-cost sentinel lane.

Live Yankee is no longer a primary paid-quote research lane. The first 100
credits/day retain broad but paced coverage; the final 50-credit reserve is only
available when fresh stored prices show a genuine same-book near-card state
(three of the four Yankee legs already covered). Hit-Rate multiples remains the
primary multiples research spend.

This patch changes quote-budget/pacing only. Candidate selection, odds rules,
same-book construction, staking, settlement, Telegram alerts and betting
authority are unchanged.
"""

from collections import defaultdict
from datetime import datetime, timezone
import sys
from typing import Any, Dict, Mapping, Sequence

import live_yankee_feed as ly

VERSION = "LIVE_YANKEE_SENTINEL_PACING_V2"
HARD_DAILY_CAP = 150
BASE_SENTINEL_CREDITS = 100
NEAR_CARD_LEGS = 3
DISCOVERY_EVENTS_PER_NEAR_CARD = 2

# Cumulative UTC gates. The final 50 credits are additionally protected by the
# near-card target gate below, so these are ceilings rather than spend targets.
PACING_STAGES = (
    (6, 50),
    (10, 75),
    (14, 100),
    (18, 125),
    (24, 150),
)

_INSTALLED = False
_ORIGINAL_REFRESH = None
_ORIGINAL_TARGETS = None


def paced_limit(now: datetime) -> int:
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    hour = now.astimezone(timezone.utc).hour
    for before_hour, limit in PACING_STAGES:
        if hour < before_hour:
            return int(limit)
    return HARD_DAILY_CAP


def _set_runtime_cap(cap: int) -> Dict[str, Any]:
    """Set every still-live Live Yankee cap surface to the sentinel ceiling."""
    cap = int(cap)
    changed: Dict[str, Any] = {"live_yankee_feed": cap}
    ly.DAILY_REFRESH_CREDIT_BUDGET = cap

    direct_specs = (
        ("credit_policy_unlimited_patch", "LIVE_YANKEE_DAILY_CREDITS"),
        ("provider_cost_control_patch", "LIVE_YANKEE_DAILY_CEILING"),
        ("multiples_budget_balance_patch", "LIVE_YANKEE_DAILY_CREDITS"),
        ("multiples_credit_efficiency_patch", "LIVE_YANKEE_DAILY_CREDITS"),
    )
    for module_name, attr in direct_specs:
        try:
            module = __import__(module_name)
            if hasattr(module, attr):
                setattr(module, attr, cap)
                changed[f"{module_name}.{attr}"] = cap
        except Exception:
            pass

    # The historical headroom patch uses separate normal/boost names.
    try:
        legacy = __import__("multiples_today_headroom_patch")
        for attr in ("NORMAL_LIVE_YANKEE_CAP", "BOOST_LIVE_YANKEE_CAP"):
            if hasattr(legacy, attr):
                setattr(legacy, attr, cap)
                changed[f"multiples_today_headroom_patch.{attr}"] = cap
    except Exception:
        pass

    # Keep the final production module's diagnostic constant aligned. This
    # module is imported while production_dashboard_app is still initialising,
    # so updating its module dict here makes the later startup summary truthful.
    prod = sys.modules.get("production_dashboard_app")
    if prod is not None:
        try:
            setattr(prod, "LIVE_YANKEE_DAILY_CREDITS", cap)
            changed["production_dashboard_app.LIVE_YANKEE_DAILY_CREDITS"] = cap
        except Exception:
            pass

    return changed


def _set_lane_caps() -> Dict[str, int]:
    # Preserve the existing 75/12.5/12.5-ish lane mix inside the smaller cap.
    caps = {"STANDARD": 112, "XS1": 19, "XS2": 19}
    try:
        import multiples_credit_efficiency_patch as efficiency
        efficiency.LIVE_LANE_DAILY_CAPS = dict(caps)
    except Exception:
        pass
    for attr in ("LANE_DAILY_CREDIT_CAPS", "LIVE_LANE_DAILY_CAPS"):
        try:
            current = getattr(ly, attr, None)
            if isinstance(current, dict):
                current.clear()
                current.update(caps)
        except Exception:
            pass
    return caps


def _target_event_id(target: Mapping[str, Any]) -> str:
    return str(target.get("event_id") or "")


def _near_card_target_plan(db, now: datetime, targets: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return a tightly bounded event plan for the protected 50-credit reserve.

    A bookmaker must already have fresh valid prices for at least three distinct
    queued Yankee events. We then prioritise at most two uncovered due events as
    fourth-leg discovery attempts, followed by due members of the fresh cluster
    that may need refreshing. If no same-book three-leg cluster exists, the
    reserve stays unused.
    """
    target_ids = {_target_event_id(t) for t in targets if _target_event_id(t)}
    if not target_ids:
        return {"allowed": [], "near_books": [], "best_overlap": 0}

    try:
        rows = [dict(x) for x in db.fetchall(
            "SELECT * FROM live_yankee_candidates WHERE status='QUEUED' ORDER BY first_seen_at,id"
        )]
    except Exception:
        # Fail closed for the protected reserve. The first 100 credits have
        # already provided broad coverage; diagnostics should not unlock more.
        return {"allowed": [], "near_books": [], "best_overlap": 0, "failed_closed": True}

    book_events: Dict[str, set[str]] = defaultdict(set)
    for candidate in rows:
        event_id = str(candidate.get("event_id") or "")
        if event_id not in target_ids or not ly._actionable(candidate.get("commence_time"), now):
            continue
        try:
            quotes = ly._valid_quotes(db, candidate, now)
        except Exception:
            quotes = {}
        for book in quotes:
            if str(book):
                book_events[str(book)].add(event_id)

    best_overlap = max((len(events) for events in book_events.values()), default=0)
    near = {book: events for book, events in book_events.items() if len(events) >= NEAR_CARD_LEGS}
    if not near:
        return {"allowed": [], "near_books": [], "best_overlap": best_overlap}

    def kickoff_key(target: Mapping[str, Any]):
        kickoff = ly._parse_iso(target.get("commence_time"))
        return (kickoff or datetime.max.replace(tzinfo=timezone.utc), str(target.get("first_seen_at") or ""), _target_event_id(target))

    ordered = sorted((dict(t) for t in targets), key=kickoff_key)
    discovery: list[str] = []
    refresh: list[str] = []

    # Prefer finding the missing fourth leg. Keep discovery globally bounded so
    # the reserve cannot degrade into another broad quote sweep.
    for _, covered in sorted(near.items(), key=lambda item: (-len(item[1]), item[0])):
        for target in ordered:
            event_id = _target_event_id(target)
            if event_id and event_id not in covered and event_id not in discovery:
                discovery.append(event_id)
                if len(discovery) >= DISCOVERY_EVENTS_PER_NEAR_CARD:
                    break
        if len(discovery) >= DISCOVERY_EVENTS_PER_NEAR_CARD:
            break

    # If discovery is not due (or has already been tried), preserve fresh cluster
    # members only as a secondary refresh path to stop an almost-complete card
    # from going stale while waiting for its fourth leg.
    covered_union = set().union(*near.values()) if near else set()
    for target in ordered:
        event_id = _target_event_id(target)
        if event_id in covered_union and event_id not in discovery and event_id not in refresh:
            refresh.append(event_id)

    allowed = discovery + refresh
    return {
        "allowed": allowed,
        "discovery": discovery,
        "refresh": refresh,
        "near_books": sorted(near),
        "best_overlap": best_overlap,
    }


def _refresh_with_pacing(db, collector, settings, now: datetime):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    used = int(ly._credits_today(db, now))
    limit = min(HARD_DAILY_CAP, paced_limit(now))
    if used >= limit:
        return {
            "polled": 0,
            "inserted": 0,
            "spent": used,
            "reason": "paced_budget",
            "used_today": used,
            "paced_limit": limit,
            "daily_cap": HARD_DAILY_CAP,
            "sentinel_base": BASE_SENTINEL_CREDITS,
            "next_stage": next(
                (f"{hour:02d}:00 UTC -> {stage_limit}" for hour, stage_limit in PACING_STAGES if hour > now.hour),
                "next UTC day",
            ),
        }

    _set_runtime_cap(limit)
    try:
        result = _ORIGINAL_REFRESH(db, collector, settings, now)
    finally:
        _set_runtime_cap(HARD_DAILY_CAP)

    if isinstance(result, dict):
        result = dict(result)
        result.setdefault("used_today", int(ly._credits_today(db, now)))
        result["paced_limit"] = limit
        result["daily_cap"] = HARD_DAILY_CAP
        result["sentinel_base"] = BASE_SENTINEL_CREDITS
    return result


def install() -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_REFRESH, _ORIGINAL_TARGETS, HARD_DAILY_CAP, PACING_STAGES
    if _INSTALLED:
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
            "daily_cap": HARD_DAILY_CAP,
            "sentinel_base": BASE_SENTINEL_CREDITS,
        }

    # production_dashboard_app historically overwrote these module globals to
    # 500 before calling install(). Reassert the new authority here so this patch
    # remains the final source of truth even across that legacy bootstrap order.
    HARD_DAILY_CAP = 150
    PACING_STAGES = ((6, 50), (10, 75), (14, 100), (18, 125), (24, 150))

    refresh_name = "_refresh_quotes" if hasattr(ly, "_refresh_quotes") else "_refresh"
    _ORIGINAL_REFRESH = getattr(ly, refresh_name)
    setattr(ly, refresh_name, _refresh_with_pacing)

    _ORIGINAL_TARGETS = ly._targets

    def sentinel_targets(db, now=None):
        stamp = now or datetime.now(timezone.utc)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        else:
            stamp = stamp.astimezone(timezone.utc)

        targets = list(_ORIGINAL_TARGETS(db, stamp))
        used = int(ly._credits_today(db, stamp))
        if used < BASE_SENTINEL_CREDITS:
            return targets
        if used >= HARD_DAILY_CAP:
            return []

        plan = _near_card_target_plan(db, stamp, targets)
        rank = {event_id: idx for idx, event_id in enumerate(plan.get("allowed") or [])}
        if not rank:
            return []
        kept = [t for t in targets if _target_event_id(t) in rank]
        return sorted(kept, key=lambda t: rank.get(_target_event_id(t), 10**9))

    ly._targets = sentinel_targets

    caps = _set_runtime_cap(HARD_DAILY_CAP)
    lane_caps = _set_lane_caps()
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "policy": "150_daily_sentinel_first100_broad_final50_near_card_only",
        "wrapped_refresh": refresh_name,
        "daily_cap": HARD_DAILY_CAP,
        "sentinel_base": BASE_SENTINEL_CREDITS,
        "protected_reserve": HARD_DAILY_CAP - BASE_SENTINEL_CREDITS,
        "near_card_required_legs": NEAR_CARD_LEGS,
        "pacing_stages_utc": [
            {"before_hour": hour, "cumulative_cap": limit}
            for hour, limit in PACING_STAGES
        ],
        "lane_caps": lane_caps,
        "cap_surfaces": caps,
        "selection_rules_changed": False,
        "card_rules_changed": False,
        "same_book_rules_changed": False,
        "staking_changed": False,
        "settlement_changed": False,
        "alerting_changed": False,
        "betting_authority_changed": False,
    }
