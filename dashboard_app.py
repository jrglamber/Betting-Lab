"""Production bootstrap that layers the curated homepage over the existing app."""

import app as base_app
import dashboard_layout_v2
from dashboard_layout_v2 import install as install_dashboard_layout_v2
from historical_rescue_patch import install as install_historical_rescue

# Resolve legacy stale-result debt before presenting the dashboard. This only
# backfills verified event results / voids cancelled events and adds a 72h
# historical-rescue queue; it does not alter selection or betting logic.
rescue = install_historical_rescue(
    base_app.app,
    base_app.db,
    base_app.web_module,
)
print(f"HISTORICAL_RESCUE_INSTALL {rescue}", flush=True)

# Curated homepage up-rev for the settlement rescue release.
dashboard_layout_v2.VERSION = "0.19.56"
install_dashboard_layout_v2(base_app.app, base_app.db, base_app.BASE_STYLE)

app = base_app.app
