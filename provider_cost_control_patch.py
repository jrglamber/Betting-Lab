from __future__ import annotations

"""Provider-credit cost controls for non-production research lanes.

This patch deliberately leaves the two multiples quote authorities untouched:
Live Yankee keeps its 1,500-credit daily allowance and Hit-Rate Yankee/Heinz
keeps its 500-credit daily allowance, including their existing near-card and
near-kickoff priority logic.

Savings are taken from lower-priority research:
- legacy manual-system quote refreshes: hard ceiling 200 credits/day;
- MSP2 additional/alternate line markets: hard ceiling 175 credits/day;
- multisport / multisport-lines convergence: half the prior polling frequency;
- football predictive approved-venue convergence: half the prior polling
  frequency (MS3 armed one-shot priority remains untouched);
- retrospective predictive-historical validation: hard ceiling 75 credits/day.

Selection, model, card construction, settlement, staking, alerting and execution
authority are unchanged.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Tuple

import collector
import credit_policy_unlimited_patch as multiples_policy
import hit_rate_multiples_lab
import live_yankee_feed
import manual_systems_shadow
import multisport_lines_shadow
import multisport_shadow
import predictive_historical

VERSION = "PROVIDER_COST_CONTROL_2026_10_27_V1"
MANUAL_SYSTEM_DAILY_CREDIT_CEILING = 200
ADDITIONAL_LINES_DAILY_CREDIT_CEILING = 175
HISTORICAL_DAILY_CREDIT_CEILING = 75

_INSTALLED = False
_ORIGINAL_MANUAL_REFRESH = None
_ORIGINAL_ADDITIONAL_POLL = None
_ORIGINAL_HISTORICAL_BUDGET = None


def _today_cost(db, run_types: Iterable[str], now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    run_types = tuple(str(x) for x in run_types if str(x))
    if not run_types:
        return 0
    placeholders = ",".join("?" for _ in run_types)
    try:
        row = db.fetchone(
            f"""SELECT COALESCE(SUM(actual_cost),0) AS n
                FROM collector_runs
                WHERE run_type IN ({placeholders})
                  AND actual_cost>0
                  AND substr(started_at,1,10)=?""",
            tuple(run_types) + (now.astimezone(timezone.utc).date().isoformat(),),
        ) or {}
        return int(row.get("n") or 0)
    except Exception:
        # Fail open if observability itself is unavailable; provider quota/reserve
        # guards remain authoritative.
        return 0


def _slower_multisport_convergence(next_mins):
    """Preserve urgency shape while halving normal convergence frequency."""
    if next_mins is None:
        return 360
    if next_mins <= 30:
        return 10
    if next_mins <= 90:
        return 20
    if next_mins <= 360:
        return 40
    if next_mins <= 1440:
        return 120
    return 360


def _slower_predictive_convergence(commence_time: str, now: datetime | None = None) -> int:
    """Half the high-resolution football price-path polling frequency."""
    mins = collector.minutes_to_start(commence_time, now)
    if mins <= 0:
        return 10**9
    if mins <= 15:
        return 10
    if mins <= 30:
        return 20
    if mins <= 60:
        return 30
    if mins <= 180:
        return 60
    if mins <= 360:
        return 120
    if mins <= 720:
        return 240
    if mins <= 1440:
        return 360
    return 720


def install(db) -> Dict[str, Any]:
    global _INSTALLED
    global _ORIGINAL_MANUAL_REFRESH, _ORIGINAL_ADDITIONAL_POLL, _ORIGINAL_HISTORICAL_BUDGET

    if _INSTALLED:
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
            "multiples_untouched": True,
        }

    # 1) Legacy/manual system price research. Railway env overrides can be higher
    # than the code default, so enforce the ceiling at the function boundary.
    _ORIGINAL_MANUAL_REFRESH = manual_systems_shadow.refresh_manual_system_quotes

    def controlled_manual_refresh(*args, **kwargs):
        configured = int(kwargs.get("daily_credit_budget", MANUAL_SYSTEM_DAILY_CREDIT_CEILING))
        kwargs["daily_credit_budget"] = min(
            max(0, configured),
            MANUAL_SYSTEM_DAILY_CREDIT_CEILING,
        )
        return _ORIGINAL_MANUAL_REFRESH(*args, **kwargs)

    manual_systems_shadow.refresh_manual_system_quotes = controlled_manual_refresh

    # 2) Alternate/additional MSP2 lines. This is a hard research ceiling around
    # the target run-rate. The original event selection, markets and quote logic
    # remain intact whenever headroom exists.
    _ORIGINAL_ADDITIONAL_POLL = multisport_lines_shadow.MultiSportLinesEngine._poll_one_additional_event

    def controlled_additional_poll(engine, now):
        spent = _today_cost(engine.db, ("MULTISPORT_LINES_ADDITIONAL",), now)
        estimated = max(1, len(getattr(engine, "additional_markets", ()) or ())) * 2
        if spent + estimated > ADDITIONAL_LINES_DAILY_CREDIT_CEILING:
            return {
                "polled": 0,
                "reason": "cost_control_additional_daily_budget",
                "spent": spent,
                "estimated_next_cost": estimated,
                "daily_budget": ADDITIONAL_LINES_DAILY_CREDIT_CEILING,
            }
        return _ORIGINAL_ADDITIONAL_POLL(engine, now)

    multisport_lines_shadow.MultiSportLinesEngine._poll_one_additional_event = controlled_additional_poll

    # 3) MSP1/MSP2 convergence. MSP2 imported the function object directly, so
    # patch both module globals. Breadth discovery and result settlement are not
    # changed.
    multisport_shadow._convergence_interval_minutes = _slower_multisport_convergence
    multisport_lines_shadow._convergence_interval_minutes = _slower_multisport_convergence

    # 4) Football predictive approved-venue convergence. Collector's MS3 armed
    # path sets interval=0 before consulting this function, so the one-shot armed
    # refresh keeps full priority.
    collector.predictive_convergence_interval_minutes = _slower_predictive_convergence

    # 5) Frozen retrospective validation. Respect any lower configured budget,
    # but prevent a Railway env override from pushing this research lane above 75.
    _ORIGINAL_HISTORICAL_BUDGET = predictive_historical.PredictiveHistoricalValidator._budget_allows

    def controlled_historical_budget(validator, estimated: int) -> Tuple[bool, str]:
        estimated = max(1, int(estimated))
        spent = int(validator._today_cost())
        if spent + estimated > HISTORICAL_DAILY_CREDIT_CEILING:
            return False, "cost_control_historical_daily_budget"
        return _ORIGINAL_HISTORICAL_BUDGET(validator, estimated)

    predictive_historical.PredictiveHistoricalValidator._budget_allows = controlled_historical_budget

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "ceilings": {
            "manual_system_quotes": MANUAL_SYSTEM_DAILY_CREDIT_CEILING,
            "multisport_lines_additional": ADDITIONAL_LINES_DAILY_CREDIT_CEILING,
            "predictive_historical": HISTORICAL_DAILY_CREDIT_CEILING,
        },
        "convergence": {
            "multisport_frequency_multiplier": 0.5,
            "multisport_lines_frequency_multiplier": 0.5,
            "predictive_odds_frequency_multiplier": 0.5,
            "ms3_armed_one_shot_priority_changed": False,
        },
        "multiples": {
            "live_yankee_policy_cap": int(multiples_policy.LIVE_YANKEE_DAILY_CREDITS),
            "hit_rate_policy_cap": int(multiples_policy.HIT_RATE_DAILY_CREDITS),
            "live_yankee_runtime_cap": int(live_yankee_feed.DAILY_REFRESH_CREDIT_BUDGET),
            "hit_rate_runtime_cap": int(hit_rate_multiples_lab.DAILY_REFRESH_CREDIT_BUDGET),
            "changed": False,
        },
        "today_before_install": {
            "manual_system_quotes": _today_cost(db, ("MANUAL_SYSTEM_QUOTES",)),
            "multisport_lines_additional": _today_cost(db, ("MULTISPORT_LINES_ADDITIONAL",)),
            "predictive_historical": _today_cost(
                db,
                ("PREDICTIVE_HISTORICAL_EVENTS", "PREDICTIVE_HISTORICAL_ODDS"),
            ),
        },
        "selection_rules_changed": False,
        "card_rules_changed": False,
        "settlement_changed": False,
        "staking_changed": False,
        "alerting_changed": False,
        "betting_authority_changed": False,
    }
