from __future__ import annotations

"""Pace Live Yankee quote credits across the UTC provider day.

The Live Yankee queue has repeatedly exhausted its 1,500-credit allowance early
in the day. A larger unpaced ceiling would simply move the exhaustion point, so
this patch combines a modestly larger 2,000-credit hard cap with cumulative
intraday gates. It wraps the already-installed quote refresh implementation and
does not change selections, card construction, settlement, staking or alerts.
"""

from datetime import datetime, timezone
from typing import Any, Dict

import live_yankee_feed as ly

VERSION = "LIVE_YANKEE_CREDIT_PACING_V1"
HARD_DAILY_CAP = 2000

# Cumulative UTC spend gates. The provider accounting day is UTC. These retain
# meaningful capacity for afternoon/evening opportunities instead of allowing
# the morning queue to consume the whole daily allowance.
PACING_STAGES = (
    (6, 500),
    (10, 1200),
    (14, 1500),
    (18, 1750),
    (24, 2000),
)

_INSTALLED = False
_ORIGINAL_REFRESH = None


def paced_limit(now: datetime) -> int:
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    hour = now.astimezone(timezone.utc).hour
    for before_hour, limit in PACING_STAGES:
        if hour < before_hour:
            return int(limit)
    return HARD_DAILY_CAP


def _set_runtime_cap(cap: int) -> Dict[str, Any]:
    """Set every known Live Yankee cap surface used by the wrapped patch stack."""
    changed: Dict[str, Any] = {"live_yankee_feed": int(cap)}
    ly.DAILY_REFRESH_CREDIT_BUDGET = int(cap)

    module_specs = (
        ("credit_policy_unlimited_patch", "LIVE_YANKEE_DAILY_CREDITS"),
        ("provider_cost_control_patch", "LIVE_YANKEE_DAILY_CEILING"),
        ("multiples_today_headroom_patch", "LIVE_DAILY_CREDITS"),
        ("multiples_budget_balance_patch", "LIVE_DAILY_CREDITS"),
        ("multiples_credit_efficiency_patch", "LIVE_DAILY_CREDITS"),
    )
    for module_name, attr in module_specs:
        try:
            module = __import__(module_name)
            if hasattr(module, attr):
                setattr(module, attr, int(cap))
                changed[f"{module_name}.{attr}"] = int(cap)
        except Exception:
            pass
    return changed


def _set_lane_caps() -> Dict[str, int]:
    # Preserve the existing lane mix while allowing the enlarged hard ceiling.
    caps = {"STANDARD": 1500, "XS1": 250, "XS2": 250}
    try:
        import multiples_credit_efficiency_patch as efficiency
        efficiency.LIVE_LANE_DAILY_CAPS = dict(caps)
    except Exception:
        pass
    # Some live-lane patches expose the lane caps on the feed module itself.
    for attr in ("LANE_DAILY_CREDIT_CAPS", "LIVE_LANE_DAILY_CAPS"):
        try:
            current = getattr(ly, attr, None)
            if isinstance(current, dict):
                current.clear(); current.update(caps)
        except Exception:
            pass
    return caps


def _refresh_with_pacing(db, collector, settings, now: datetime):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    used = int(ly._credits_today(db, now))
    limit = paced_limit(now)
    if used >= limit:
        return {
            "polled": 0,
            "inserted": 0,
            "spent": used,
            "reason": "paced_budget",
            "used_today": used,
            "paced_limit": limit,
            "daily_cap": HARD_DAILY_CAP,
            "next_stage": next(
                (f"{hour:02d}:00 UTC -> {stage_limit}" for hour, stage_limit in PACING_STAGES if hour > now.hour),
                "next UTC day",
            ),
        }

    # The wrapped implementation may use any of several module-level cap
    # constants. Temporarily lower them to the current pacing gate so an inner
    # refresh cannot use the full 2,000-credit hard cap ahead of schedule.
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
    return result


def install() -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_REFRESH
    if _INSTALLED:
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
            "daily_cap": HARD_DAILY_CAP,
        }

    # Support either the original feed name or a compatibility alias installed
    # by a later patch.
    refresh_name = "_refresh_quotes" if hasattr(ly, "_refresh_quotes") else "_refresh"
    _ORIGINAL_REFRESH = getattr(ly, refresh_name)
    setattr(ly, refresh_name, _refresh_with_pacing)

    caps = _set_runtime_cap(HARD_DAILY_CAP)
    lane_caps = _set_lane_caps()
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "wrapped_refresh": refresh_name,
        "daily_cap": HARD_DAILY_CAP,
        "pacing_stages_utc": [
            {"before_hour": hour, "cumulative_cap": limit}
            for hour, limit in PACING_STAGES
        ],
        "lane_caps": lane_caps,
        "cap_surfaces": caps,
        "selection_rules_changed": False,
        "card_rules_changed": False,
        "staking_changed": False,
        "alerting_changed": False,
    }
