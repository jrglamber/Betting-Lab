from __future__ import annotations

"""Protect tennis RESULT settlement from optional rich research API usage.

Result refreshes are invoked by tennis_result_pipeline_fix separately from
rich.maintenance. Reserve app-level daily capacity for those result refreshes,
and cap optional historical match-statistics calls to a small daily allowance.
Neither changes tennis selections, scoring, quota accounting or market polling.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import tennis_rich_source as rich

VERSION = "TENNIS_RESULT_PRIORITY_BUDGET_V3_TPRED_FRESH"
FREE_TIER_MAX_REQUESTS = 50
FREE_RESULT_REQUEST_RESERVE = 40
FREE_RANKING_DAILY_BUDGET = 6
FREE_FIXTURE_DAILY_BUDGET = 4
PAID_RESULT_REQUEST_RESERVE = 500
PAID_OPTIONAL_STATS_DAILY_CAP = 24

def _free_tier(daily_cap: int) -> bool:
    return 0 < int(daily_cap) <= FREE_TIER_MAX_REQUESTS


def _result_reserve(daily_cap: int) -> int:
    # Protect forty free requests for completed results, leaving ten for
    # weekly current rankings and daily upcoming fixture identities.
    return min(int(daily_cap), FREE_RESULT_REQUEST_RESERVE) if _free_tier(daily_cap) else min(
        PAID_RESULT_REQUEST_RESERVE, max(0, int(daily_cap))
    )


def _stats_daily_cap(daily_cap: int) -> int:
    return 0 if _free_tier(daily_cap) else PAID_OPTIONAL_STATS_DAILY_CAP
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
        return _stats_daily_cap(rich.DAILY_REQUEST_CAP)


def _research_allowance(used: int, daily_cap: int) -> int:
    return max(0, int(daily_cap) - max(0, int(used)) - _result_reserve(daily_cap))


def _optional_stats_allowance(used: int, daily_cap: int, stats_used: int) -> int:
    return max(
        0,
        min(_stats_daily_cap(daily_cap) - max(0, int(stats_used)),
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
        if _free_tier(rich.DAILY_REQUEST_CAP):
            # Refresh rankings weekly without restarting the historical
            # match-statistics backlog; preserve result and fixture budgets.
            rich.install_schema(db)
            state = db.fetchone(
                "SELECT last_rankings_at FROM tennis_rich_sync_state WHERE singleton_id=1"
            ) or {}
            week_monday = now.date() - timedelta(days=now.weekday())
            if str(state.get("last_rankings_at") or "")[:10] >= week_monday.isoformat():
                return {
                    "ok": True, "version": VERSION, "requests": 0,
                    "reason": "free_rankings_current_week",
                    "mode": "FREE_RESULTS_TPRED_CONTEXT",
                    "reserved_for_results": _result_reserve(rich.DAILY_REQUEST_CAP),
                    "betting_authority": False,
                }
            budget = max(
                0,
                int(rich.DAILY_REQUEST_CAP) - int(used)
                - _result_reserve(rich.DAILY_REQUEST_CAP)
                - FREE_FIXTURE_DAILY_BUDGET,
            )
            permitted = min(max(0, int(max_requests)), FREE_RANKING_DAILY_BUDGET, budget)
            if permitted < 4:
                return {
                    "ok": True, "version": VERSION, "requests": 0,
                    "reason": "free_rankings_budget_unavailable",
                    "mode": "FREE_RESULTS_TPRED_CONTEXT",
                    "reserved_for_results": _result_reserve(rich.DAILY_REQUEST_CAP),
                    "betting_authority": False,
                }
            client = rich.RichClient(db, max_requests=permitted)
            if not client.key:
                return {
                    "ok": False, "version": VERSION, "requests": 0,
                    "reason": "TENNIS_RAPIDAPI_KEY_missing",
                    "betting_authority": False,
                }
            rankings = {}
            for tour in ("atp", "wta"):
                if not client.available:
                    break
                rankings[tour] = rich.sync_current_rankings(db, client, tour, now)
            if client.errors == 0 and len(rankings) == 2:
                db.execute(
                    "UPDATE tennis_rich_sync_state SET last_rankings_at=? WHERE singleton_id=1",
                    (now.isoformat(),),
                )
            return {
                "ok": client.errors == 0, "version": VERSION,
                "requests": client.requests, "errors": client.errors,
                "rankings": rankings, "mode": "FREE_RESULTS_TPRED_CONTEXT",
                "reserved_for_results": _result_reserve(rich.DAILY_REQUEST_CAP),
                "fixture_budget": FREE_FIXTURE_DAILY_BUDGET,
                "optional_stats_budget": 0,
                "betting_authority": False,
            }
        allowance = _research_allowance(used, rich.DAILY_REQUEST_CAP)
        permitted = min(max(0, int(max_requests)), allowance)
        if permitted <= 0:
            return {
                "ok": True, "version": VERSION, "skipped": True,
                "reason": ("free_50_results_only" if _free_tier(rich.DAILY_REQUEST_CAP)
                           else "rich_research_paused_reserve_for_results"),
                "requests": 0, "stats": {"fetched": 0, "nonempty": 0},
                "daily_used": used, "daily_cap": rich.DAILY_REQUEST_CAP,
                "reserved_for_results": _result_reserve(rich.DAILY_REQUEST_CAP),
                "mode": ("FREE_RESULTS_TPRED_CONTEXT" if _free_tier(rich.DAILY_REQUEST_CAP)
                         else "LARGE_PLAN_BUDGETED_RESEARCH"),
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
                "daily_cap": _stats_daily_cap(rich.DAILY_REQUEST_CAP),
            }
        return original_match_stats(db, client, limit=permitted)

    rich.maintenance = maintenance_with_result_reserve
    rich.sync_missing_match_stats = budgeted_match_stats
    _INSTALLED = True
    return {
        "installed": True, "already_installed": False, "version": VERSION,
        "result_reserve": _result_reserve(rich.DAILY_REQUEST_CAP),
        "optional_match_stats_daily_cap": _stats_daily_cap(rich.DAILY_REQUEST_CAP),
        "mode": ("FREE_RESULTS_TPRED_CONTEXT" if _free_tier(rich.DAILY_REQUEST_CAP)
                 else "LARGE_PLAN_BUDGETED_RESEARCH"),
        "rich_provider_daily_cap": int(rich.DAILY_REQUEST_CAP),
        "free_rankings_daily_budget": FREE_RANKING_DAILY_BUDGET,
        "free_fixtures_daily_budget": FREE_FIXTURE_DAILY_BUDGET,
        "provider_calls_added": 0, "result_collection_changed": False,
        "betting_authority_changed": False, "research_only": True,
    }
