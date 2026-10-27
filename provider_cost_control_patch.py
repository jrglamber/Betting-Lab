from __future__ import annotations

"""Provider-credit cost controls and HR multiples budget rebalance.

The provider's real remaining-credit guard remains authoritative. This patch
moves 500 credits/day of research allowance toward the Hit-Rate Yankee/Heinz
lane while leaving Live Yankee unchanged at 1,500/day.

Budget transfer:
- Hit-Rate Yankee/Heinz: 500 -> 1,000 credits/day (+500)
- legacy manual-system dedicated quotes: 200 -> 0 (-200)
- tennis research: 500 -> 350 (-150)
- MSP2 additional/alternate line markets: 175 -> 75 (-100)
- retrospective PRED1/PRED2 historical validation: 75 -> 25 (-50)

The legacy manual-system engine remains enabled and can still form/settle shadow
cards from shared stored odds; only its bespoke paid quote refresh is disabled.
Existing multisport / multisport-lines and football predictive convergence
throttles from V1 are retained. Selection, model, card construction, settlement,
staking, alerting and execution authority are unchanged.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Tuple

import collector
import credit_policy_unlimited_patch as multiples_policy
import hit_rate_daily_boost_patch as hr_daily_boost
import hit_rate_multiples_lab
import live_yankee_feed
import manual_systems_shadow
import multiples_credit_efficiency_patch as efficiency
import multiples_today_headroom_patch as legacy_headroom
import multisport_lines_shadow
import multisport_shadow
import predictive_historical

VERSION = "PROVIDER_COST_CONTROL_2026_10_27_V2_HR1000"
HR_DAILY_CREDIT_CEILING = 1_000
MANUAL_SYSTEM_DAILY_CREDIT_CEILING = 0
TENNIS_DAILY_CREDIT_CEILING = 350
ADDITIONAL_LINES_DAILY_CREDIT_CEILING = 75
HISTORICAL_DAILY_CREDIT_CEILING = 25

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


def _hr_cap(_now=None) -> int:
    """Authoritative HR allowance through legacy wrappers."""
    return HR_DAILY_CREDIT_CEILING


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
            "hr_daily_cap": HR_DAILY_CREDIT_CEILING,
            "live_yankee_unchanged": True,
        }

    # 0) Raise only the Hit-Rate Yankee/Heinz allowance. Several retired wrappers
    # still surround HR refreshes, so changing one constant is insufficient: each
    # legacy authority is synchronized to 1,000. Live Yankee is deliberately not
    # touched here and remains under the existing 1,500/day policy.
    multiples_policy.HIT_RATE_DAILY_CREDITS = HR_DAILY_CREDIT_CEILING
    hit_rate_multiples_lab.DAILY_REFRESH_CREDIT_BUDGET = HR_DAILY_CREDIT_CEILING
    efficiency.HIT_RATE_DAILY_CREDITS = HR_DAILY_CREDIT_CEILING
    hr_daily_boost.NORMAL_DAILY_CAP = HR_DAILY_CREDIT_CEILING
    hr_daily_boost.BOOST_DAILY_CAP = HR_DAILY_CREDIT_CEILING
    hr_daily_boost._effective_cap = _hr_cap
    legacy_headroom.NORMAL_HIT_RATE_CAP = HR_DAILY_CREDIT_CEILING
    legacy_headroom.BOOST_HIT_RATE_CAP = HR_DAILY_CREDIT_CEILING

    # 1) Legacy/manual system price research. Keep the engine, card formation,
    # settlement and exports alive, but stop bespoke provider quote calls. It can
    # still consume shared odds snapshots collected for the core football lanes.
    _ORIGINAL_MANUAL_REFRESH = manual_systems_shadow.refresh_manual_system_quotes

    def controlled_manual_refresh(*args, **kwargs):
        configured = int(kwargs.get("daily_credit_budget", MANUAL_SYSTEM_DAILY_CREDIT_CEILING))
        kwargs["daily_credit_budget"] = min(
            max(0, configured),
            MANUAL_SYSTEM_DAILY_CREDIT_CEILING,
        )
        return _ORIGINAL_MANUAL_REFRESH(*args, **kwargs)

    manual_systems_shadow.refresh_manual_system_quotes = controlled_manual_refresh

    # 2) Tennis stays fully enabled, but its broad provider allowance is reduced.
    # The evidence still justifies keeping the lane (especially the 10-15% edge
    # pocket), not funding it at the same level as the stronger multiples work.
    tennis_before = tennis_after = None
    tennis_error = None
    try:
        import web as web_module

        tennis_quota = getattr(getattr(web_module, "tennis_engine", None), "quota", None)
        if tennis_quota is not None:
            tennis_before = int(getattr(tennis_quota, "daily_budget", 0))
            tennis_quota.daily_budget = min(
                max(0, tennis_before),
                TENNIS_DAILY_CREDIT_CEILING,
            )
            tennis_after = int(tennis_quota.daily_budget)
    except Exception as exc:
        tennis_error = f"{type(exc).__name__}: {exc}"

    # 3) Alternate/additional MSP2 lines. Keep the research lane and its existing
    # event/market logic, but enforce a much smaller hard ceiling.
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

    # 4) MSP1/MSP2 convergence. Retain the V1 frequency reduction. Breadth
    # discovery and result settlement are not changed.
    multisport_shadow._convergence_interval_minutes = _slower_multisport_convergence
    multisport_lines_shadow._convergence_interval_minutes = _slower_multisport_convergence

    # 5) Football predictive approved-venue convergence. Retain the V1 frequency
    # reduction. Collector's MS3 armed path sets interval=0 before consulting this
    # function, so the one-shot armed refresh keeps full priority.
    collector.predictive_convergence_interval_minutes = _slower_predictive_convergence

    # 6) Frozen retrospective validation. PRED1/PRED2 remain available for
    # corroboration, but their poor forward evidence does not justify high paid
    # historical spend.
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
        "budget_transfer_daily": {
            "hit_rate_multiples": 500,
            "funded_by": {
                "manual_system_quotes": 200,
                "tennis": 150,
                "multisport_lines_additional": 100,
                "predictive_historical": 50,
            },
            "net_change": 0,
        },
        "ceilings": {
            "hit_rate_multiples": HR_DAILY_CREDIT_CEILING,
            "live_yankee": int(live_yankee_feed.DAILY_REFRESH_CREDIT_BUDGET),
            "manual_system_quotes": MANUAL_SYSTEM_DAILY_CREDIT_CEILING,
            "tennis": TENNIS_DAILY_CREDIT_CEILING,
            "multisport_lines_additional": ADDITIONAL_LINES_DAILY_CREDIT_CEILING,
            "predictive_historical": HISTORICAL_DAILY_CREDIT_CEILING,
        },
        "tennis_runtime": {
            "before": tennis_before,
            "after": tennis_after,
            "error": tennis_error,
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
            "live_yankee_changed": False,
            "hit_rate_changed": True,
        },
        "today_before_install": {
            "manual_system_quotes": _today_cost(db, ("MANUAL_SYSTEM_QUOTES",)),
            "multisport_lines_additional": _today_cost(db, ("MULTISPORT_LINES_ADDITIONAL",)),
            "predictive_historical": _today_cost(
                db,
                ("PREDICTIVE_HISTORICAL_EVENTS", "PREDICTIVE_HISTORICAL_ODDS"),
            ),
        },
        "manual_system_engine_enabled": True,
        "manual_system_dedicated_provider_quotes_enabled": False,
        "selection_rules_changed": False,
        "card_rules_changed": False,
        "settlement_changed": False,
        "staking_changed": False,
        "alerting_changed": False,
        "betting_authority_changed": False,
    }
