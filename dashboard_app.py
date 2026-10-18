"""Production bootstrap that layers the curated homepage over the existing app."""

import re

import app as base_app
import dashboard_layout_v2
import live_yankee_feed
from credit_policy_unlimited_patch import install as install_credit_policy_unlimited
from cumulative_pnl_dashboard_patch import install as install_cumulative_pnl
from dashboard_credit_usage_patch import install as install_dashboard_credit_usage
from dashboard_layout_v2 import install as install_dashboard_layout_v2
from dashboard_metric_parity_patch import install as install_dashboard_metric_parity
from dashboard_api_sports_health_patch import install as install_dashboard_api_sports_health
from dashboard_responsiveness_patch import install as install_dashboard_responsiveness
from historical_rescue_patch import install as install_historical_rescue
from postponed_event_revival_patch import install as install_postponed_event_revival
from settlement_rescue_final_football import install as install_final_football_rescue
from settlement_rescue_final_tennis import install as install_final_tennis_rescue
from settlement_rescue_queue_guard import install as install_rescue_queue_guard
from first_half_goal_lab import run_maintenance as run_first_half_goal_maintenance
from first_half_goal_runtime_patch import install as install_first_half_goal_runtime
from first_half_goal_settlement_patch import install as install_first_half_goal_settlement
from first_half_goal_curated_patch import install as install_first_half_goal_curated
from tennis_predictive import install_schema as install_tpred_schema
from tennis_predictive_runtime_patch import install as install_tpred_runtime
from tennis_predictive_dashboard import install as install_tpred_dashboard
from tennis_predictive_export_patch import install as install_tpred_exports
from tennis_rich_source_probe import probe as probe_tennis_rich_source
from pocket_freeze_hr2_patch import install as install_audited_pockets
from multiples_funnel_diagnostic_patch import install as install_multiples_funnel_diagnostic
from multiples_theoretical_queue_patch import install as install_multiples_theoretical_queue
from api_sports_settlement_side_patch import reconcile_ncaaf

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

# Keep the provider's real remaining-credit guard authoritative while applying
# explicit daily budgets to the two high-frequency multiples quote lanes and
# backing off repeated no-price polls when kickoff is still distant.
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

# NCAAF price-linked shadow bets can carry Odds-API mascot names while the
# provider forecast carries the school-only label. Reconcile historical settled
# accounting by the already-matched event's home/away side; zero provider calls.
try:
    ncaaf_settlement_repair = reconcile_ncaaf(base_app.db)
except Exception as exc:
    ncaaf_settlement_repair = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
print(f"NCAAF_SETTLEMENT_SIDE_REPAIR {ncaaf_settlement_repair}", flush=True)

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

# Freeze the robust pockets found in the 17-Oct full audit and extend HR2 only
# where the opportunity was absent from Live Yankee/HR1/HR2. HR1 remains
# unchanged; the HR2 policy version is bumped so historical cards/candidates are
# immutable. No singles/execution authority is changed and no provider calls are
# added by this patch.
audited_pockets = install_audited_pockets(base_app.db)
print(f"AUDITED_POCKETS_INSTALL {audited_pockets}", flush=True)

# Move the multiples observation boundary upstream: theoretical/model/reference
# selections can queue up to 72h before start, while the existing 30h paid quote
# horizon, freshness, target bands, same-book checks and betting authority stay
# unchanged. HR uses a new policy version so older forward evidence is not mixed.
multiples_theoretical_queue = install_multiples_theoretical_queue(base_app.db)
print(f"MULTIPLES_THEORETICAL_QUEUE {multiples_theoretical_queue}", flush=True)

# The original curated homepage intentionally showed only Live Yankee assembly
# state, so its Settled and ROI cells were hard-coded as em dashes. The
# cumulative-P/L patch now exposes canonical placeable settlement totals on the
# Live Yankee scoreboard; wire those values into the homepage summary row too.
_original_curated_page = dashboard_layout_v2._page


def _curated_page_with_live_yankee_settlement(db, base_style: str) -> str:
    html = _original_curated_page(db, base_style)
    try:
        yankee = dict(live_yankee_feed.scoreboard(db))
        pnl = dict(yankee.get("cumulative_pnl") or {})
        settled = int(pnl.get("placeable_settled_cards") or 0)
        roi_value = pnl.get("placeable_roi_pct")
        roi = "—" if roi_value is None else f"{float(roi_value):.1f}%"
        pattern = (
            r"(<tr><td><a href='/live-yankee'><strong>Live Yankee Feed</strong></a></td>"
            r"<td>[^<]*</td>)<td>—</td><td>—</td>"
            r"(<td class='muted'>[^<]*</td></tr>)"
        )
        html = re.sub(
            pattern,
            lambda m: f"{m.group(1)}<td>{settled}</td><td>{roi}</td>{m.group(2)}",
            html,
            count=1,
        )
    except Exception:
        pass
    return html


dashboard_layout_v2._page = _curated_page_with_live_yankee_settlement

# football-data.co.uk remains the FHG1 historical/training source, but it can
# publish weekend results with a material delay. Install a settlement-only
# API-Football fallback before the startup maintenance pass. It reads only the
# half-time score of already-frozen candidates and never changes their forecast.
first_half_goal_settlement = install_first_half_goal_settlement(base_app.db)
print(f"FIRST_HALF_GOAL_SETTLEMENT {first_half_goal_settlement}", flush=True)

# Seed and audit FHG1 immediately at process startup so a same-day slate is
# frozen before kickoff rather than waiting for a later worker cycle.
try:
    first_half_goal_startup = run_first_half_goal_maintenance(base_app.db)
except Exception as exc:
    first_half_goal_startup = {
        "ok": False,
        "error": f"{type(exc).__name__}: {exc}",
        "betting_authority": False,
    }
print(f"FIRST_HALF_GOAL_STARTUP {first_half_goal_startup}", flush=True)

# Startup alone is not enough to settle completed FHG1 candidates on a long-
# running container. Attach the same idempotent maintenance pass to the normal
# worker cycle; source-specific throttles prevent repeated external requests.
first_half_goal_runtime = install_first_half_goal_runtime(
    base_app.web_module,
    base_app.db,
)
print(f"FIRST_HALF_GOAL_RUNTIME {first_half_goal_runtime}", flush=True)

# Validate the optional rich tennis source with two tiny read-only requests.
# This is diagnostics only: no TPRED rows or forecasts are changed by the probe.
try:
    tpred_rich_probe = probe_tennis_rich_source()
except Exception as exc:
    tpred_rich_probe = {
        "ok": False,
        "error": f"{type(exc).__name__}: {exc}",
        "betting_authority": False,
    }
print(f"TPRED_RICH_SOURCE_PROBE {tpred_rich_probe}", flush=True)

# TPRED is research-only and deliberately reuses the already-collected tennis
# universe. Install its schema synchronously, but let the existing tennis worker
# cycle perform historical walk-forward/forward forecast maintenance so startup
# and web serving remain responsive. Zero new provider credits are required.
tpred_schema = install_tpred_schema(base_app.db)
print(f"TPRED_SCHEMA {tpred_schema}", flush=True)
tpred_runtime = install_tpred_runtime(
    getattr(base_app.web_module, "tennis_engine", None),
    base_app.db,
)
print(f"TPRED_RUNTIME {tpred_runtime}", flush=True)
tpred_exports = install_tpred_exports()
print(f"TPRED_EXPORTS {tpred_exports}", flush=True)

# Curated homepage up-rev for FHG1, TPRED, frozen audited pockets, Live Yankee
# parity, the queue-to-price funnel, pre-price candidate queues, corrected
# API-Sports settlement accounting, recurring/timely FHG1 settlement, and
# non-blocking dashboard serving.
dashboard_layout_v2.VERSION = "0.19.74"
install_dashboard_layout_v2(base_app.app, base_app.db, base_app.BASE_STYLE)
multiples_funnel = install_multiples_funnel_diagnostic(base_app.app, base_app.db)
print(f"MULTIPLES_FUNNEL_INSTALL {multiples_funnel}", flush=True)
first_half_goal_curated = install_first_half_goal_curated(base_app.app, base_app.db)
print(f"FIRST_HALF_GOAL_CURATED {first_half_goal_curated}", flush=True)
tpred_dashboard = install_tpred_dashboard(base_app.app, base_app.db, base_app.BASE_STYLE)
print(f"TPRED_DASHBOARD {tpred_dashboard}", flush=True)
dashboard_responsiveness = install_dashboard_responsiveness(base_app.app)
print(f"DASHBOARD_RESPONSIVENESS {dashboard_responsiveness}", flush=True)

app = base_app.app