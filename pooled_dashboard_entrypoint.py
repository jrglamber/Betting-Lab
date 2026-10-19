from __future__ import annotations

"""Production entrypoint that installs DB and tennis efficiency patches first."""

import os

import db_pool_patch  # noqa: F401  -- must run before dashboard_app imports web/db instances
import tennis_rich_throughput_patch  # noqa: F401 -- patch before runtime captures maintenance function
import dashboard_app as _dashboard_app
from dashboard_app import app
from hit_rate_price_tolerance_patch import install as install_hr_price_tolerance

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
