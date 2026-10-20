from __future__ import annotations

"""Production entrypoint that installs DB and research-safety patches first."""

import os

import db_pool_patch  # noqa: F401  -- must run before dashboard_app imports web/db instances
import bookmaker_exclusion_patch  # noqa: F401 -- exclude inaccessible prices before any Odds API consumer is built
import tennis_rich_throughput_patch  # noqa: F401 -- patch before runtime captures maintenance function
import dashboard_app as _dashboard_app
from dashboard_app import app
import hit_rate_daily_boost_patch  # noqa: F401 -- historical wrapper; Oct20 reporting patch prevents stale cap conflict
from first_half_goal_price_capture_patch import install as install_fhg_price_capture
from frozen_shadow_portfolio_patch import install as install_frozen_shadow_portfolio
from frozen_shadow_dashboard_patch import install as install_frozen_shadow_dashboard
from hit_rate_price_tolerance_patch import install as install_hr_price_tolerance
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

# Today-only bounded headroom for the evening football slate. This sits on top of
# the efficiency cadence and automatically returns to the normal caps by UTC date.
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

# Correct the stale HR 500-credit override, add open/today counts to Frozen
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

__all__ = ["app"]
