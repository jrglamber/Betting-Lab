"""Production bootstrap that layers the curated homepage over the existing app."""

import app as base_app
import dashboard_layout_v2
from dashboard_layout_v2 import install as install_dashboard_layout_v2
from historical_rescue_patch import install as install_historical_rescue
from settlement_rescue_final_tennis import install as install_final_tennis_rescue
from settlement_rescue_queue_guard import install as install_rescue_queue_guard

# Resolve legacy stale-result debt before presenting the dashboard. This only
# backfills verified event results / voids cancelled events and adds a 72h
# historical-rescue queue; it does not alter selection or betting logic.
rescue = install_historical_rescue(
    base_app.app,
    base_app.db,
    base_app.web_module,
)
print(f"HISTORICAL_RESCUE_INSTALL {rescue}", flush=True)

# A same-day tennis result crossed the six-hour stale threshold while the
# provider result was delayed. Clear it under the existing tennis winner rule.
final_tennis = install_final_tennis_rescue(base_app.db)
print(f"HISTORICAL_RESCUE_FINAL_TENNIS {final_tennis}", flush=True)

# Refresh the >72h rescue queue automatically during every normal worker cycle.
queue_guard = install_rescue_queue_guard(base_app.web_module)
print(f"HISTORICAL_RESCUE_QUEUE_GUARD {queue_guard}", flush=True)

# Curated homepage up-rev for the settlement rescue release.
dashboard_layout_v2.VERSION = "0.19.56"
install_dashboard_layout_v2(base_app.app, base_app.db, base_app.BASE_STYLE)

app = base_app.app
