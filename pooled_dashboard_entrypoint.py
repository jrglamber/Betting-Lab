from __future__ import annotations

"""Production entrypoint that installs DB and research-safety patches first."""

import os

import db_pool_patch  # noqa: F401  -- must run before dashboard_app imports web/db instances
import bookmaker_exclusion_patch  # noqa: F401 -- exclude inaccessible prices before any Odds API consumer is built
import tennis_rich_throughput_patch  # noqa: F401 -- patch before runtime captures maintenance function
import dashboard_app as _dashboard_app
from dashboard_app import app
import dashboard_responsiveness_patch as dashboard_responsiveness
import adaptive_winner_research as adaptive_winner_research
import hit_rate_daily_boost_patch  # noqa: F401 -- historical wrapper; Oct20 reporting patch prevents stale cap conflict
from adaptive_winner_price_adjust_patch import install as install_winner_price_adjust
from first_half_goal_free_settlement_patch import install as install_fhg_free_settlement
from first_half_goal_price_capture_patch import install as install_fhg_price_capture
from first_half_goal_price_evidence_patch import install as install_fhg_price_evidence
from frozen_shadow_bet_detail_patch import install as install_frozen_shadow_bet_detail
from frozen_shadow_portfolio_patch import install as install_frozen_shadow_portfolio
from frozen_shadow_dashboard_patch import install as install_frozen_shadow_dashboard
from hit_rate_price_tolerance_patch import install as install_hr_price_tolerance
from live_yankee_price_tolerance_patch import install as install_live_yankee_price_tolerance
from multiples_budget_balance_patch import install as install_multiples_budget_balance
from multiples_credit_efficiency_patch import install as install_multiples_credit_efficiency
from multiples_today_headroom_patch import install as install_multiples_today_headroom
from oct20_runtime_reporting_fixes_patch import install as install_oct20_runtime_reporting_fixes

# The base credit policy is installed during dashboard_app bootstrap. Tighten only
# the high-frequency multiples quote cadence afterwards: preserve all selection,
# same-book and card rules while pacing non-urgent paid polling across the day.
try:
    _multiples_credit_efficiency = install_multiples_credit_efficiency(
        _dashboard_app.base_app.db
    )
    print(
        f"MULTIPLES_CREDIT_EFFICIENCY_INSTALL {_multiples_credit_efficiency}",
        flush=True,
    )
except Exception as exc:
    print(
        f"MULTIPLES_CREDIT_EFFICIENCY_INSTALL {{'ok': False, 'error': '{type(exc).__name__}: {exc}'}}",
        flush=True,
    )

# Historical 20-Oct headroom wrapper remains installed for compatibility, but a
# final current-budget patch below neutralises its expired asymmetric ceilings.
try:
    _multiples_today_headroom = install_multiples_today_headroom(
        _dashboard_app.base_app.db
    )
    print(
        f"MULTIPLES_TODAY_HEADROOM_INSTALL {_multiples_today_headroom}",
        flush=True,
    )
except Exception as exc:
    print(
        f"MULTIPLES_TODAY_HEADROOM_INSTALL {{'ok': False, 'error': '{type(exc).__name__}: {exc}'}}",
        flush=True,
    )

# Freeze a production-style singles portfolio with a common forward boundary.
# Existing component cohort freeze dates are preserved; this adds fixed reference
# stakes and deduplicated whole-portfolio accounting only. No real bets are sent.
try:
    _frozen_portfolio = install_frozen_shadow_portfolio(
        _dashboard_app.base_app.app,
        _dashboard_app.base_app.db,
        _dashboard_app.base_app.web_module,
    )
    print(f"FROZEN_SHADOW_PORTFOLIO_INSTALL {_frozen_portfolio}", flush=True)
except Exception as exc:
    print(
        f"FROZEN_SHADOW_PORTFOLIO_INSTALL {{'ok': False, 'error': '{type(exc).__name__}: {exc}'}}",
        flush=True,
    )

# Surface the frozen portfolio on the curated homepage. Presentation only: the
# accounting/freeze state above remains authoritative and no selection changes.
try:
    _frozen_dashboard = install_frozen_shadow_dashboard(_dashboard_app.base_app.db)
    print(f"FROZEN_SHADOW_DASHBOARD_INSTALL {_frozen_dashboard}", flush=True)
except Exception as exc:
    print(
        f"FROZEN_SHADOW_DASHBOARD_INSTALL {{'ok': False, 'error': '{type(exc).__name__}: {exc}'}}",
        flush=True,
    )

# Correct the stale legacy HR-cap override, add open/today counts to Frozen
# Shadow, and make TPRED market-Brier reporting explicitly paired/provisional.
# Research/reporting only: no model, staking or betting authority changes.
try:
    _oct20_runtime_reporting = install_oct20_runtime_reporting_fixes(
        _dashboard_app.base_app.db
    )
    print(f"OCT20_RUNTIME_REPORTING_FIXES_INSTALL {_oct20_runtime_reporting}", flush=True)
except Exception as exc:
    print(
        f"OCT20_RUNTIME_REPORTING_FIXES_INSTALL {{'ok': False, 'error': '{type(exc).__name__}: {exc}'}}",
        flush=True,
    )

# Add a collapsible per-bet audit trail after the open/today tile wrapper is in
# place, so the dashboard shows exact frozen odds, bookmaker, stake, settlement,
# nominal P/L and CLV without changing portfolio selection or accounting.
try:
    _frozen_bet_detail = install_frozen_shadow_bet_detail(
        _dashboard_app.base_app.app,
        _dashboard_app.base_app.db,
    )
    print(f"FROZEN_SHADOW_BET_DETAIL_INSTALL {_frozen_bet_detail}", flush=True)
except Exception as exc:
    print(
        f"FROZEN_SHADOW_BET_DETAIL_INSTALL {{'ok': False, 'error': '{type(exc).__name__}: {exc}'}}",
        flush=True,
    )

# Replace the inaccessible current-season API-Football FHG1 settlement fallback
# with an hourly, zero-subscription football-data.co.uk refresh only when an
# already-frozen candidate is >2h overdue. Canonical HT scores are reconciled.
try:
    _fhg_free_settlement = install_fhg_free_settlement(_dashboard_app.base_app.db)
    print(f"FHG1_FREE_SETTLEMENT_BOOTSTRAP {_fhg_free_settlement}", flush=True)
except Exception as exc:
    print(
        f"FHG1_FREE_SETTLEMENT_BOOTSTRAP {{'ok': False, 'error': '{type(exc).__name__}: {exc}'}}",
        flush=True,
    )

# FHG1 already freezes forecasts prospectively. Capture the missing executable
# Over 0.5 first-half-goal price once, from accessible venues only, so hit-rate
# evidence can now be converted into real forward ROI/EV evidence.
try:
    _fhg_prices = install_fhg_price_capture(_dashboard_app.base_app)
    print(f"FHG1_PRICE_CAPTURE_INSTALL {_fhg_prices}", flush=True)
except Exception as exc:
    print(
        f"FHG1_PRICE_CAPTURE_INSTALL {{'ok': False, 'error': '{type(exc).__name__}: {exc}'}}",
        flush=True,
    )

# Expose the frozen FHG prices on the FHG dashboard and in exports. This adds
# observed flat-stake single-selection ROI and card price coverage only; the
# underlying FHG model/threshold/card selection remains unchanged.
try:
    _fhg_price_evidence = install_fhg_price_evidence(_dashboard_app.base_app.db)
    print(f"FHG1_PRICE_EVIDENCE_INSTALL {_fhg_price_evidence}", flush=True)
except Exception as exc:
    print(
        f"FHG1_PRICE_EVIDENCE_INSTALL {{'ok': False, 'error': '{type(exc).__name__}: {exc}'}}",
        flush=True,
    )

# Read-only startup evidence for the paid tennis subscription. RapidAPI's
# x-ratelimit-requests-remaining response header is already persisted in the
# request ledger; logging the latest value lets operations verify the real plan
# headroom without exposing the API key or making an extra provider call.
try:
    _quota = _dashboard_app.base_app.db.fetchone(
        """
        SELECT requested_at,provider_remaining
        FROM tennis_rich_api_ledger
        WHERE provider_remaining IS NOT NULL
        ORDER BY id DESC LIMIT 1
        """
    ) or {}
    print(
        "TPRED_RAPIDAPI_QUOTA_SNAPSHOT "
        + str(
            {
                "requested_at": _quota.get("requested_at"),
                "provider_remaining": _quota.get("provider_remaining"),
                "configured_daily_cap": int(os.getenv("TPRED_RICH_DAILY_CAP", "2500")),
            }
        ),
        flush=True,
    )
except Exception as exc:
    print(
        f"TPRED_RAPIDAPI_QUOTA_SNAPSHOT {{'error': '{type(exc).__name__}: {exc}'}}",
        flush=True,
    )

# Forward-only price-tolerance challengers reuse the HR quote cache. They add
# no provider/API requests: same-book is still mandatory and only the frozen
# minimum acceptable price is relaxed to P98/P96/P94 for shadow comparison.
try:
    _hr_price_tolerance = install_hr_price_tolerance(_dashboard_app.base_app.db)
    print(f"HR_PRICE_TOLERANCE_INSTALL {_hr_price_tolerance}", flush=True)
except Exception as exc:
    print(
        f"HR_PRICE_TOLERANCE_INSTALL {{'ok': False, 'error': '{type(exc).__name__}: {exc}'}}",
        flush=True,
    )

# Clean V2 Live Yankee tolerance research. The compatibility installer now
# bootstraps the complete Yankee runtime in deterministic order first. P100 is
# the strict control; P98/P96/P94/P90/P85 remain same-book shadow challengers,
# add zero provider calls and do not alter the manual/production Yankee feed.
try:
    _live_yankee_price_tolerance = install_live_yankee_price_tolerance(_dashboard_app.base_app.db)
    print(f"LIVE_YANKEE_PRICE_TOLERANCE_INSTALL {_live_yankee_price_tolerance}", flush=True)
except Exception as exc:
    print(
        f"LIVE_YANKEE_PRICE_TOLERANCE_INSTALL {{'ok': False, 'error': '{type(exc).__name__}: {exc}'}}",
        flush=True,
    )

# W1 still predicts winners without using price. Only the descriptive historical
# fingerprint leaderboard is price-adjusted so short-priced favourites no longer
# look novel simply because they win more often than the global population.
try:
    _winner_price_adjust = install_winner_price_adjust(_dashboard_app.base_app.db)
    print(f"ADAPTIVE_WINNER_PRICE_ADJUST_INSTALL {_winner_price_adjust}", flush=True)
except Exception as exc:
    print(
        f"ADAPTIVE_WINNER_PRICE_ADJUST_INSTALL {{'ok': False, 'error': '{type(exc).__name__}: {exc}'}}",
        flush=True,
    )

# Final quote-budget authority for the current research period: 500 credits/day
# for Live Yankee and 1000/day for Hit-Rate multiples. Existing urgency/backoff,
# same-book, candidate and pricing rules remain unchanged.
try:
    _multiples_budget_balance = install_multiples_budget_balance(
        _dashboard_app.base_app.db
    )
    print(f"MULTIPLES_BUDGET_BALANCE_INSTALL {_multiples_budget_balance}", flush=True)
except Exception as exc:
    print(
        f"MULTIPLES_BUDGET_BALANCE_INSTALL {{'ok': False, 'error': '{type(exc).__name__}: {exc}'}}",
        flush=True,
    )

# Release v0.19.83: the responsive homepage finalizer resolves this global at
# render time, so this final assignment remains authoritative even though older
# presentation patches retain their historical VERSION constants internally.
RELEASE_VERSION = "0.19.83"
dashboard_responsiveness.BUILD_VERSION = RELEASE_VERSION
adaptive_winner_research.DASHBOARD_VERSION = RELEASE_VERSION
_dashboard_app.dashboard_layout_v2.VERSION = RELEASE_VERSION
_dashboard_app.base_app.web_module.VERSION = RELEASE_VERSION
_dashboard_app.base_app.app.version = RELEASE_VERSION
print(f"BETTING_LAB_RELEASE_VERSION {RELEASE_VERSION}", flush=True)

__all__ = ["app"]
