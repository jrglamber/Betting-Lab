"""Final Railway production bootstrap for Betting Lab.

This module imports the established adaptive dashboard stack, then installs the
production-only forward research/runtime controls explicitly under Railway's
real entrypoint. It intentionally does not rely on Python's optional
sitecustomize startup hook.
"""

import adaptive_dashboard_app as base
from football_coverage_expansion_patch import run_cross_comp_maintenance
from uefa_fhg_coverage_export_patch import install as install_uefa_fhg_coverage_export
from challenge_selection_framework_v5 import install as install_challenge_selection_framework_v5
import credit_policy_unlimited_patch as credit_policy
import hit_rate_daily_boost_patch as hr_daily_boost
import hit_rate_multiples_lab as hit_rate_lab
import live_yankee_feed as live_yankee_feed
import live_yankee_credit_pacing_patch as live_yankee_pacing
import multiples_budget_balance_patch as budget_balance
import multiples_credit_efficiency_patch as multiples_efficiency
import multiples_today_headroom_patch as legacy_headroom
import provider_cost_control_patch as provider_cost
from hit_rate_trixie_patch import install as install_hit_rate_small_systems
from hit_rate_jit_priority_patch import install as install_hit_rate_jit_priority
from hit_rate_price_tolerance_ladder_patch import install as install_hit_rate_price_tolerance_ladder
from hit_rate_settlement_fallback_patch import install as install_hit_rate_settlement_fallback
from hit_rate_theoretical_result_collection_patch_v3 import (
    install as install_hit_rate_theoretical_result_collection,
    run_once as run_hit_rate_theoretical_result_collection_once,
)
from multiples_funnel_diagnostic_patch import install as install_multiples_funnel_diagnostic
from multiples_home_small_systems_patch import install as install_multiples_home_small_systems
from live_yankee_credit_pacing_patch import install as install_live_yankee_credit_pacing
from tennis_result_pipeline_fix import install as install_tennis_result_pipeline_fix
from tennis_result_retry_guard import install as install_tennis_result_retry_guard
from tennis_result_priority_budget_patch import install as install_tennis_result_priority_budget
from tennis_forward_readiness_patch import install as install_tennis_forward_readiness
from tennis_forward_home_sync_fix import install as install_tennis_forward_home_sync_fix
from tennis_predictive_readiness_embed_patch import install as install_tennis_predictive_readiness_embed
from verified_jacquet_machac_rescue import run_once as repair_verified_jacquet_machac
from verified_van_assche_tien_rescue import run_once as repair_verified_van_assche_tien

BUILD_VERSION = "0.19.141"

# Keep all visible version surfaces aligned with the real production entrypoint.
base.BUILD_VERSION = BUILD_VERSION
base.adaptive_core.DASHBOARD_VERSION = BUILD_VERSION
base.dashboard_responsiveness.BUILD_VERSION = BUILD_VERSION
base.curated.dashboard_layout_v2.VERSION = BUILD_VERSION
base.curated.base_app.web_module.VERSION = BUILD_VERSION
base.curated.base_app.app.version = BUILD_VERSION

# New evidence-led £200→£2,000 manual challenge policy.  The adaptive
# bootstrap has already installed the October legacy and CLV-first wrappers;
# this export-only V5 layer supersedes their staking annotations, preserving
# the legacy policy as separate historical files. No execution authority.
try:
    challenge_selection_framework_v5 = install_challenge_selection_framework_v5(BUILD_VERSION)
except Exception as exc:
    challenge_selection_framework_v5 = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"CHALLENGE_SELECTION_FRAMEWORK_V5_INSTALL {challenge_selection_framework_v5}", flush=True)

# The adaptive bootstrap installs FHG1 and cross-competition research;
# the £200→£2,000 challenge wrapper is already installed in production.
# Supplement the final export with zero-cost UEFA visibility and an explicitly
# uncalibrated, unpriced first-half-goal research proxy (no betting authority).
try:
    uefa_fhg_coverage_export = install_uefa_fhg_coverage_export(BUILD_VERSION)
except Exception as exc:
    uefa_fhg_coverage_export = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"UEFA_FHG_COVERAGE_EXPORT_INSTALL {uefa_fhg_coverage_export}", flush=True)


# Ensure research-only cross-competition forecasts are reconciled at startup,
# even if the existing periodic worker has not completed a cycle yet.
# This uses stored local training/results only: zero paid provider calls.
try:
    uefa_xcomp_startup = run_cross_comp_maintenance(base.curated.base_app.db)
except Exception as exc:
    uefa_xcomp_startup = {"error": f"{type(exc).__name__}: {exc}", "installed": False}
print(f"UEFA_XCOMP_STARTUP_RECONCILIATION {uefa_xcomp_startup}", flush=True)

# Final paid Odds-API quote budgets. Synchronise all still-live wrappers because
# several historical patches resolve their caps dynamically on each refresh.
HIT_RATE_DAILY_CREDITS = 2_500
LIVE_YANKEE_DAILY_CREDITS = 500
LIVE_YANKEE_QUOTE_REFRESH_MINUTES = 10.0
LIVE_YANKEE_MAX_EVENTS_PER_CYCLE = 4


def _final_hr_cap(_now=None) -> int:
    return HIT_RATE_DAILY_CREDITS


credit_policy.HIT_RATE_DAILY_CREDITS = HIT_RATE_DAILY_CREDITS
credit_policy.LIVE_YANKEE_DAILY_CREDITS = LIVE_YANKEE_DAILY_CREDITS
hit_rate_lab.DAILY_REFRESH_CREDIT_BUDGET = HIT_RATE_DAILY_CREDITS
live_yankee_feed.DAILY_REFRESH_CREDIT_BUDGET = LIVE_YANKEE_DAILY_CREDITS
multiples_efficiency.HIT_RATE_DAILY_CREDITS = HIT_RATE_DAILY_CREDITS
multiples_efficiency.LIVE_YANKEE_DAILY_CREDITS = LIVE_YANKEE_DAILY_CREDITS
provider_cost.HR_DAILY_CREDIT_CEILING = HIT_RATE_DAILY_CREDITS
hr_daily_boost.NORMAL_DAILY_CAP = HIT_RATE_DAILY_CREDITS
hr_daily_boost.BOOST_DAILY_CAP = HIT_RATE_DAILY_CREDITS
hr_daily_boost._effective_cap = _final_hr_cap
legacy_headroom.NORMAL_HIT_RATE_CAP = HIT_RATE_DAILY_CREDITS
legacy_headroom.BOOST_HIT_RATE_CAP = HIT_RATE_DAILY_CREDITS
legacy_headroom.NORMAL_LIVE_YANKEE_CAP = LIVE_YANKEE_DAILY_CREDITS
legacy_headroom.BOOST_LIVE_YANKEE_CAP = LIVE_YANKEE_DAILY_CREDITS
budget_balance.HIT_RATE_DAILY_CREDITS = HIT_RATE_DAILY_CREDITS
budget_balance.LIVE_YANKEE_DAILY_CREDITS = LIVE_YANKEE_DAILY_CREDITS

credit_policy.REPEATED_MISS_POLLS = 1
credit_policy.MID_BACKOFF_MINUTES = 60.0
credit_policy.FAR_BACKOFF_MINUTES = 120.0
credit_policy.RECENT_POLL_WINDOW_MINUTES = 180.0
credit_policy.CLOSE_TO_KICKOFF_MINUTES = 60.0
live_yankee_feed.QUOTE_REFRESH_MINUTES = LIVE_YANKEE_QUOTE_REFRESH_MINUTES
live_yankee_feed.MAX_REFRESH_EVENTS_PER_CYCLE = LIVE_YANKEE_MAX_EVENTS_PER_CYCLE

live_yankee_pacing.HARD_DAILY_CAP = LIVE_YANKEE_DAILY_CREDITS
live_yankee_pacing.PACING_STAGES = (
    (6, 100),
    (10, 200),
    (14, 300),
    (18, 400),
    (24, 500),
)

try:
    hit_rate_small_systems = install_hit_rate_small_systems()
except Exception as exc:
    hit_rate_small_systems = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"HIT_RATE_HR2_SMALL_SYSTEMS_INSTALL {hit_rate_small_systems}", flush=True)

# Rank already-due HR quote targets by card readiness and time-to-kickoff before
# the collector applies its event limit. Eligibility and card rules are unchanged.
try:
    hit_rate_jit_priority = install_hit_rate_jit_priority()
except Exception as exc:
    hit_rate_jit_priority = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"HIT_RATE_JIT_PRIORITY_INSTALL {hit_rate_jit_priority}", flush=True)

# Explain sudden zero-quote days without changing HR2 rules or provider spend.
# Read-only one-time snapshot of the frozen queue, kickoff window, and target gates.
try:
    import json as _hr_quote_json
    from hr_quote_eligibility_audit import snapshot as _hr_quote_eligibility_snapshot
    _eligibility = _hr_quote_eligibility_snapshot(base.curated.base_app.db)
    print("HR_QUOTE_ELIGIBILITY_AUDIT "
          + _hr_quote_json.dumps(_eligibility, sort_keys=True), flush=True)
except Exception as exc:
    print(f"HR_QUOTE_ELIGIBILITY_AUDIT_ERROR {type(exc).__name__}: {exc}", flush=True)

try:
    hit_rate_price_tolerance_ladder = install_hit_rate_price_tolerance_ladder(base.curated.base_app.db)
except Exception as exc:
    hit_rate_price_tolerance_ladder = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"HR_PRICE_TOLERANCE_LADDER_INSTALL {hit_rate_price_tolerance_ladder}", flush=True)

try:
    hit_rate_settlement_fallback = install_hit_rate_settlement_fallback(base.curated.base_app.db)
except Exception as exc:
    hit_rate_settlement_fallback = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"HR_SETTLEMENT_FALLBACK_BOOTSTRAP {hit_rate_settlement_fallback}", flush=True)

try:
    hit_rate_theoretical_result_collection = install_hit_rate_theoretical_result_collection()
except Exception as exc:
    hit_rate_theoretical_result_collection = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"HR_THEORETICAL_RESULT_COLLECTION_BOOTSTRAP {hit_rate_theoretical_result_collection}", flush=True)

try:
    hr_theoretical_startup_pass = run_hit_rate_theoretical_result_collection_once(
        base.curated.base_app.web_module.tennis_engine,
        base.curated.base_app.web_module.multisport_engine,
    )
except Exception as exc:
    hr_theoretical_startup_pass = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"HR_THEORETICAL_RESULT_COLLECTION_STARTUP_BOOTSTRAP {hr_theoretical_startup_pass}", flush=True)

try:
    multiples_home_small_systems = install_multiples_home_small_systems()
except Exception as exc:
    multiples_home_small_systems = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"MULTIPLES_HOME_SMALL_SYSTEMS_INSTALL {multiples_home_small_systems}", flush=True)

try:
    live_yankee_credit_pacing = install_live_yankee_credit_pacing()
    live_lane_caps = {"STANDARD": 375, "XS1": 62, "XS2": 63}
    multiples_efficiency.LIVE_LANE_DAILY_CAPS = dict(live_lane_caps)
    for attr in ("LANE_DAILY_CREDIT_CAPS", "LIVE_LANE_DAILY_CAPS"):
        current = getattr(live_yankee_feed, attr, None)
        if isinstance(current, dict):
            current.clear()
            current.update(live_lane_caps)
    if isinstance(live_yankee_credit_pacing, dict):
        live_yankee_credit_pacing = dict(live_yankee_credit_pacing)
        live_yankee_credit_pacing["lane_caps"] = live_lane_caps
except Exception as exc:
    live_yankee_credit_pacing = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"LIVE_YANKEE_CREDIT_PACING_INSTALL {live_yankee_credit_pacing}", flush=True)

try:
    multiples_funnel_diagnostic = install_multiples_funnel_diagnostic(
        base.curated.base_app.app,
        base.curated.base_app.db,
    )
except Exception as exc:
    multiples_funnel_diagnostic = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"MULTIPLES_FUNNEL_DIAGNOSTIC_INSTALL {multiples_funnel_diagnostic}", flush=True)

# On the 50/day free RapidAPI tier, reserve 40 calls for completed results,
# up to four for daily ATP/WTA fixture identities and up to six for weekly
# rankings; pause optional per-match stats. Tennis odds and Hit Rate picks
# continue through The Odds API. Paid-tier research limits are unchanged.
# Install before the result pipeline wraps rich.maintenance, and resolve the
# wrapped collector dynamically in the runtime worker (not an old import alias).
try:
    tennis_result_priority_budget = install_tennis_result_priority_budget()
except Exception as exc:
    tennis_result_priority_budget = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"TENNIS_RESULT_PRIORITY_BUDGET_INSTALL {tennis_result_priority_budget}", flush=True)

# Repair and keep fresh the result source before readiness health is calculated.
# This revisits today's/yesterday's rich tennis results every four hours and can
# bridge a result into tennis_results only when the provider match is uniquely
# identified by tour, exact normalised player pair, nearby date and winner id.
try:
    tennis_result_pipeline_fix = install_tennis_result_pipeline_fix(
        base.curated.base_app.db,
        tennis_engine=getattr(base.curated.base_app.web_module, "tennis_engine", None),
    )
except Exception as exc:
    tennis_result_pipeline_fix = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"TENNIS_RESULT_PIPELINE_FIX_INSTALL {tennis_result_pipeline_fix}", flush=True)

# A failed rich-result request must remain due. The guard patches the refresh
# clock so transient provider errors retry on the next maintenance cycle, and
# launches one bounded background retry immediately after startup.
try:
    tennis_result_retry_guard = install_tennis_result_retry_guard(base.curated.base_app.db)
except Exception as exc:
    tennis_result_retry_guard = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"TENNIS_RESULT_RETRY_GUARD_INSTALL {tennis_result_retry_guard}", flush=True)

# Freeze clean prospective tennis evidence from this release onward. Stored
# results are re-applied at startup and after every normal tennis cycle; TPRED4
# is then synchronised from the settled TPRED3 identity. Zero provider calls.
try:
    tennis_forward_readiness = install_tennis_forward_readiness(
        base.curated.base_app.app,
        base.curated.base_app.db,
        base.curated.base_app.BASE_STYLE,
        tennis_engine=getattr(base.curated.base_app.web_module, "tennis_engine", None),
    )
except Exception as exc:
    tennis_forward_readiness = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"TENNIS_FORWARD_READINESS_INSTALL {tennis_forward_readiness}", flush=True)

# The readiness patch adds a homepage link. Keep that final presentation wrapper
# synchronous so the established homepage async bridge remains in FastAPI's
# threadpool instead of being called from a running event loop.
try:
    tennis_forward_home_sync_fix = install_tennis_forward_home_sync_fix(
        base.curated.base_app.app,
    )
except Exception as exc:
    tennis_forward_home_sync_fix = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"TENNIS_FORWARD_HOME_SYNC_FIX_INSTALL {tennis_forward_home_sync_fix}", flush=True)

# Keep all TPRED/tennis evidence together for the user: append the four forward
# readiness lanes and settlement health to the existing /tennis-predictive page.
# This is presentation-only and reuses the already-computed readiness scoreboard.
try:
    tennis_predictive_readiness_embed = install_tennis_predictive_readiness_embed(
        base.curated.base_app.app,
        base.curated.base_app.db,
    )
except Exception as exc:
    tennis_predictive_readiness_embed = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"TENNIS_PREDICTIVE_READINESS_EMBED_INSTALL {tennis_predictive_readiness_embed}", flush=True)

# Portfolio Shadow and Quality use the SAME evolving canonical HR2 pool.
# NCAAF H2H 3.00-4.99 is promoted into ordinary HR2 qualification; no
# parallel NCAAF-only shadow feeder or separate portfolio source is needed.
# Existing frozen HR cards, prices and results remain unchanged.
try:
    from hr_portfolio_shadow import install as install_hr_portfolio_shadow
    hr_portfolio_shadow = install_hr_portfolio_shadow(
        base.curated.base_app.app,
        base.curated.base_app.db,
    )
except Exception as exc:
    hr_portfolio_shadow = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"HR_PORTFOLIO_SHADOW_INSTALL {hr_portfolio_shadow}", flush=True)

# Freeze a paired, pre-event price-quality ranker whenever the chronological
# HR portfolio forms a group. It sees exactly the same bookmaker/quote window,
# leg count and reference stake and never requests more provider data.
try:
    from hr_rolling_quality_challenger import install as install_hr_rolling_quality
    hr_rolling_quality = install_hr_rolling_quality(
        base.curated.base_app.app,
        base.curated.base_app.db,
    )
except Exception as exc:
    hr_rolling_quality = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"HR_ROLLING_QUALITY_INSTALL {hr_rolling_quality}", flush=True)

# Retain the existing historical Portfolio Telegram audit tables/route,
# then supersede its sending permissions with standard HR2-only routing below.
# All research, odds, settlement and export functions remain unchanged.
try:
    from hr_portfolio_telegram import install as install_hr_portfolio_telegram
    hr_portfolio_telegram = install_hr_portfolio_telegram(
        base.curated.base_app.app,
        base.curated.base_app.db,
        base.curated.base_app.web_module.settings,
    )
except Exception as exc:
    hr_portfolio_telegram = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"HR_PORTFOLIO_TELEGRAM_INSTALL {hr_portfolio_telegram}", flush=True)

# The user's current routing choice supersedes the November portfolio-only
# alert policy: ONLY forward HR2, genuinely same-book/manual-placeable standard
# bet-ready cards (Double/Trixie/Yankee/Heinz) may notify. Portfolio and
# Quality research continue but their Telegram sender is muted. The outbound
# Telegram transport separately rejects every source except HR2_BET_READY.
try:
    from hr2_telegram_only_patch import install as install_hr2_telegram_only
    hr2_telegram_only = install_hr2_telegram_only(
        base.curated.base_app.app,
        base.curated.base_app.db,
        base.curated.base_app.web_module.settings,
    )
except Exception as exc:
    hr2_telegram_only = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"HR2_TELEGRAM_ONLY_INSTALL {hr2_telegram_only}", flush=True)

# Read-only same-book candidate queue visibility for chronological Portfolio
# Shadow and the quality-ranked challenger, using the existing quote cache.
try:
    from hr_portfolio_queues import install as install_hr_portfolio_queues
    hr_portfolio_queues = install_hr_portfolio_queues(
        base.curated.base_app.app,
        base.curated.base_app.db,
    )
except Exception as exc:
    hr_portfolio_queues = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"HR_PORTFOLIO_QUEUES_INSTALL {hr_portfolio_queues}", flush=True)

# One cheap, read-only bootstrap health snapshot. Installation success is NOT
# evidence of recent portfolio groups or quality pairs. Surface the distinction
# explicitly from the existing Postgres ledger rather than spending more
# provider requests or adding another dashboard/experiment.
try:
    import hr_portfolio_shadow as _hrp_health
    import hr_rolling_quality_challenger as _hrq_health
    import hr_portfolio_queues as _hrqueue_health
    _db = base.curated.base_app.db
    _p = _hrp_health.status(_db)
    _q = _hrq_health.scoreboard(_db)
    _queue = _hrqueue_health.read(_db, view="portfolio", limit=4)
    _last = _db.fetchone(
        """SELECT started_at,ok,detail FROM collector_runs
           WHERE run_type='HR_PORTFOLIO_SHADOW_MAINT'
           ORDER BY id DESC LIMIT 1"""
    ) or {}
    _diag = {
        "last_portfolio_maintenance": _last.get("started_at"),
        "last_portfolio_maintenance_ok": _last.get("ok"),
        "baseline_groups_by_status": _p.get("groups", []),
        "quality_decisions": _q.get("total_decisions", 0),
        "quality_pending": _q.get("pending", 0),
        "quality_paired_settled": _q.get("paired", 0),
        "quality_skips": _q.get("skipped", {}),
        "last_quality_maintenance": _q.get("last_maintenance_at"),
        "last_shared_queue_snapshot": _queue.get("snapshot_at"),
        "queue_candidates": _queue.get("source_candidates", 0),
        "queue_quoted": _queue.get("quoted_candidates", 0),
        "queue_waiting_price": _queue.get("waiting_price_candidates", 0),
        "queue_conflicting_fixtures": _queue.get("conflicting_fixtures_abstained", 0),
        "no_additional_api_calls": True,
    }
    print(f"HR2_PORTFOLIO_QUALITY_HEALTH {_diag}", flush=True)
except Exception as exc:
    print(f"HR2_PORTFOLIO_QUALITY_HEALTH_ERROR {type(exc).__name__}: {exc}", flush=True)

# Read-only actual-frozen-price HR2 pocket audit. This counts one economic
# selection once across all four multiple formats, and never mistakes the
# theoretical target price for a placeable singles quote.
try:
    import json as _hr2_json
    from hr2_singles_pocket_audit import install as _install_hr2_singles
    from hr2_singles_pocket_audit import report as _hr2_pocket_report
    _audit_db = base.curated.base_app.db
    _audit_app = base.curated.base_app.app
    _audit_install = _install_hr2_singles(_audit_app, _audit_db)
    print(f"HR2_UNIQUE_SINGLES_POCKET_INSTALL {_audit_install}", flush=True)
    print("HR2_UNIQUE_SINGLES_POCKET_AUDIT " + _hr2_json.dumps(
        _hr2_pocket_report(_audit_db), sort_keys=True,
    ), flush=True)
except Exception as exc:
    print(f"HR2_UNIQUE_SINGLES_POCKET_AUDIT_ERROR {type(exc).__name__}: {exc}", flush=True)

# Read-only matched-pick ROI: each settled multiple is compared against singles
# formed from the exact same frozen selections and prices, at equal total stake.
# This is a diagnostic only: no settlement mutations, selections or provider calls.
try:
    from singles_vs_multiples_roi_audit import install as install_singles_vs_multiples_roi
    singles_vs_multiples_roi = install_singles_vs_multiples_roi(
        base.curated.base_app.app, base.curated.base_app.db
    )
except Exception as exc:
    singles_vs_multiples_roi = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"SINGLES_VS_MULTIPLES_INSTALL {singles_vs_multiples_roi}", flush=True)


# Reconcile the independently verified 2 November ATP Paris result against every
# locally recorded exact fixture ID, then run ordinary shadow/card settlement.
# No provider requests, backfilled picks or forced outcomes for other matches.
try:
    verified_tennis_repair = repair_verified_jacquet_machac(base.curated.base_app.db)
except Exception as exc:
    verified_tennis_repair = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"VERIFIED_TENNIS_REPAIR_BOOTSTRAP {verified_tennis_repair}", flush=True)

# The 4 November ATP Paris match must be identified separately from these
# players' opposite-result 26 October Vienna meeting. Exact date/tour/pair only.
# A canonical, independently verified result feeds all original HR/tennis ledgers.
try:
    verified_van_assche_tien = repair_verified_van_assche_tien(base.curated.base_app.db)
except Exception as exc:
    verified_van_assche_tien = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"VERIFIED_VAN_ASSCHE_TIEN_BOOTSTRAP {verified_van_assche_tien}", flush=True)


print(
    "MULTIPLES_BUDGET_REALLOCATION "
    + str({
        "hit_rate_daily_cap": HIT_RATE_DAILY_CREDITS,
        "live_yankee_daily_cap": LIVE_YANKEE_DAILY_CREDITS,
        "live_yankee_quote_refresh_minutes": LIVE_YANKEE_QUOTE_REFRESH_MINUTES,
        "live_yankee_max_events_per_cycle": LIVE_YANKEE_MAX_EVENTS_PER_CYCLE,
        "live_yankee_mid_backoff_minutes": credit_policy.MID_BACKOFF_MINUTES,
        "live_yankee_far_backoff_minutes": credit_policy.FAR_BACKOFF_MINUTES,
        "live_yankee_repeated_miss_polls": credit_policy.REPEATED_MISS_POLLS,
        "hr_cap_surfaces": {
            "collector": hit_rate_lab.DAILY_REFRESH_CREDIT_BUDGET,
            "base_policy": credit_policy.HIT_RATE_DAILY_CREDITS,
            "efficiency": multiples_efficiency.HIT_RATE_DAILY_CREDITS,
            "provider_cost": provider_cost.HR_DAILY_CREDIT_CEILING,
            "legacy_boost_normal": hr_daily_boost.NORMAL_DAILY_CAP,
            "legacy_headroom_normal": legacy_headroom.NORMAL_HIT_RATE_CAP,
            "budget_balance": budget_balance.HIT_RATE_DAILY_CREDITS,
        },
        "selection_rules_changed": False,
        "card_rules_changed": False,
        "betting_authority_changed": False,
    }),
    flush=True,
)
print(f"BETTING_LAB_PRODUCTION_RELEASE_VERSION {BUILD_VERSION}", flush=True)

app = base.app