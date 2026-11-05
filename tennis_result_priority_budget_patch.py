from __future__ import annotations

"""Protect tennis RESULT settlement from optional rich research API usage.

Result refreshes are invoked by tennis_result_pipeline_fix separately from
rich.maintenance. Reserve app-level daily capacity for those result refreshes,
and cap optional historical match-statistics calls to a small daily allowance.
Neither changes tennis selections, scoring, quota accounting or market polling.
"""

from datetime import datetime, timezone
from typing import Any, Dict

import tennis_rich_source as rich

VERSION = "TENNIS_RESULT_PRIORITY_BUDGET_V1"
RESULT_REQUEST_RESERVE = 500
OPTIONAL_STATS_DAILY_CAP = 24
_INSTALLED = False


def _today(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _stats_used_today(db, now=None):
    prefix = _today(now).date().isoformat() + "%"
    try:
        row = db.fetchone(
            """SELECT COUNT(*) AS n FROM tennis_rich_api_ledger
               WHERE requested_at LIKE ? AND purpose LIKE 'match_stats_%'""",
            (prefix,),
        ) or {}
        return max(0, int(row.get("n") or 0))
    except Exception:
        # If the stats accounting is unavailable, fail closed for OPTIONAL
        # statistics, while leaving result collection completely untouched.
        return OPTIONAL_STATS_DAILY_CAP


def _research_allowance(used: int, daily_cap: int) -> int:
    return max(0, int(daily_cap) - max(0, int(used)) - RESULT_REQUEST_RESERVE)


def _optional_stats_allowance(used: int, daily_cap: int, stats_used: int) -> int:
    return max(
        0,
        min(OPTIONAL_STATS_DAILY_CAP - max(0, int(stats_used)),
            _research_allowance(used, daily_cap)),
    )


def install() -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    original_maintenance = rich.maintenance
    original_match_stats = rich.sync_missing_match_stats

    def maintenance_with_result_reserve(db, now=None, max_requests=rich.PER_CYCLE_CAP):
        now = _today(now)
        used = rich._used_today(db, now)
        allowance = _research_allowance(used, rich.DAILY_REQUEST_CAP)
        permitted = min(max(0, int(max_requests)), allowance)
        if permitted <= 0:
            return {
                "ok": True, "version": VERSION, "skipped": True,
                "reason": "rich_research_paused_reserve_for_results",
                "requests": 0, "stats": {"fetched": 0, "nonempty": 0},
                "daily_used": used, "daily_cap": rich.DAILY_REQUEST_CAP,
                "reserved_for_results": RESULT_REQUEST_RESERVE,
                "betting_authority": False,
            }
        return original_maintenance(db, now=now, max_requests=permitted)

    def budgeted_match_stats(db, client, limit=100):
        used = rich._used_today(db)
        stats_used = _stats_used_today(db)
        allowed = _optional_stats_allowance(used, rich.DAILY_REQUEST_CAP, stats_used)
        permitted = min(max(0, int(limit)), allowed)
        if permitted <= 0:
            return {
                "fetched": 0, "nonempty": 0,
                "skipped": "optional_match_stats_daily_cap_or_result_reserve",
                "stats_used_today": stats_used,
                "daily_cap": OPTIONAL_STATS_DAILY_CAP,
            }
        return original_match_stats(db, client, limit=permitted)

    rich.maintenance = maintenance_with_result_reserve
    rich.sync_missing_match_stats = budgeted_match_stats
    _INSTALLED = True
    return {
        "installed": True, "already_installed": False, "version": VERSION,
        "result_reserve": RESULT_REQUEST_RESERVE,
        "optional_match_stats_daily_cap": OPTIONAL_STATS_DAILY_CAP,
        "provider_calls_added": 0, "result_collection_changed": False,
        "betting_authority_changed": False, "research_only": True,
    }
