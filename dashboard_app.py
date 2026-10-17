"""Production bootstrap that layers the curated homepage over the existing app."""

import app as base_app
import dashboard_layout_v2
from credit_policy_unlimited_patch import install as install_credit_policy_unlimited
from cumulative_pnl_dashboard_patch import install as install_cumulative_pnl
from dashboard_credit_usage_patch import install as install_dashboard_credit_usage
from dashboard_layout_v2 import install as install_dashboard_layout_v2
from dashboard_metric_parity_patch import install as install_dashboard_metric_parity
from dashboard_api_sports_health_patch import install as install_dashboard_api_sports_health
from historical_rescue_patch import install as install_historical_rescue
from postponed_event_revival_patch import install as install_postponed_event_revival
from settlement_rescue_final_football import install as install_final_football_rescue
from settlement_rescue_final_tennis import install as install_final_tennis_rescue
from settlement_rescue_queue_guard import install as install_rescue_queue_guard
from first_half_goal_lab import run_maintenance as run_first_half_goal_maintenance
from first_half_goal_curated_patch import install as install_first_half_goal_curated

# Resolve legacy stale-result debt before presenting the dashboard. This only
# backfills verified event results / voids cancelled events and adds a 72h
# historical-rescue queue; it does not alter selection or betting logic.
rescue = install_historical_rescue(
    base_app.app,
    base_app.db,
    base_app.web_module,
)
print(f"HISTORICAL_RESCUE_INSTALL {rescue}", flush=True)

# Four remaining source/canonical football obligations were independently
# verified as postponed/rescheduled. Void the original event obligations rather
# than inventing scores; any rescheduled fixtures remain separate future events.
final_football = install_final_football_rescue(base_app.db)
print(f"HISTORICAL_RESCUE_FINAL_FOOTBALL {final_football}", flush=True)

# If a provider later reuses one of those event IDs with a genuinely changed
# future kickoff, allow it back into normal collection instead of leaving the
# rescheduled fixture permanently stuck POSTPONED.
revival = install_postponed_event_revival(base_app.web_module)
print(f"POSTPONED_EVENT_REVIVAL_INSTALL {revival}", flush=True)

# A same-day tennis result crossed the six-hour stale threshold while the
# provider result was delayed. Clear it under the existing tennis winner rule.
final_tennis = install_final_tennis_rescue(base_app.db)
print(f"HISTORICAL_RESCUE_FINAL_TENNIS {final_tennis}", flush=True)

# Refresh the >72h rescue queue automatically during every normal worker cycle.
queue_guard = install_rescue_queue_guard(base_app.web_module)
print(f"HISTORICAL_RESCUE_QUEUE_GUARD {queue_guard}", flush=True)

# Remove app-imposed daily credit ceilings from every configurable Odds API
# lane via Railway variables, and lift the two newer hard-coded 500/day quote
# caps here. The provider's real remaining-credit balance is still enforced.
credit_policy = install_credit_policy_unlimited()
print(f"CREDIT_POLICY_UNLIMITED {credit_policy}", flush=True)

# The curated dashboard must repeat canonical research metrics rather than
# recomputing lookalike cohorts. This is presentation-only and also keeps the
# focused EuroLeague band aligned with the current 1.50-3.99 research lane.
parity = install_dashboard_metric_parity(base_app.db)
print(f"DASHBOARD_METRIC_PARITY {parity}", flush=True)

# API-Sports health must use the same paid-status source that the predictive
# diagnostics use. This fixes the false '0 current-season games' dashboard tile.
api_health = install_dashboard_api_sports_health()
print(f"DASHBOARD_API_SPORTS_HEALTH {api_health}", flush=True)

# Restore provider-credit observability from the legacy dashboard near the top
# of the curated homepage. This is presentation-only and reads the existing
# actual_cost ledger; it does not impose any new cap.
credit_panel = install_dashboard_credit_usage(base_app.db)
print(f"DASHBOARD_PROVIDER_CREDITS {credit_panel}", flush=True)

# Surface running settled P/L on both execution-facing multiples dashboards.
# This is presentation/accounting only. Synthetic cross-book research cards are
# deliberately excluded from the placeable headline totals.
cumulative_pnl = install_cumulative_pnl(base_app.db)
print(f"CUMULATIVE_PNL_DASHBOARDS {cumulative_pnl}", flush=True)

# Seed and audit FHG1 immediately at process startup so a same-day slate is
# frozen before kickoff rather than waiting for a later worker cycle. The lane
# uses football-data.co.uk half-time scores only and has no betting authority.
try:
    first_half_goal_startup = run_first_half_goal_maintenance(base_app.db)
except Exception as exc:
    first_half_goal_startup = {
        "ok": False,
        "error": f"{type(exc).__name__}: {exc}",
        "betting_authority": False,
    }
print(f"FIRST_HALF_GOAL_STARTUP {first_half_goal_startup}", flush=True)

# Curated homepage up-rev for the FHG1 research section.
dashboard_layout_v2.VERSION = "0.19.65"
install_dashboard_layout_v2(base_app.app, base_app.db, base_app.BASE_STYLE)
first_half_goal_curated = install_first_half_goal_curated(base_app.app, base_app.db)
print(f"FIRST_HALF_GOAL_CURATED {first_half_goal_curated}", flush=True)

app = base_app.app
