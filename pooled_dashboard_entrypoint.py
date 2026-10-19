from __future__ import annotations

"""Production entrypoint that installs DB pooling before app bootstrap."""

import db_pool_patch  # noqa: F401  -- must run before dashboard_app imports web/db instances
from dashboard_app import app

__all__ = ["app"]
