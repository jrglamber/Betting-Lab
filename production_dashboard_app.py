"""Final Railway production bootstrap for Betting Lab.

This module imports the established adaptive dashboard stack, then installs the
production-only forward research/runtime controls explicitly under Railway's
real entrypoint. It intentionally does not rely on Python's optional
sitecustomize startup hook.
"""

import adaptive_dashboard_app as base
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
from hit_rate_settlement_fallback_patch import install as install_hit_rate_settlement_fallback
from hit_rate_theoretical_result_collection_patch_v3 import install as install_hit_rate_theoretical_result_collection
from multiples_home_small_systems_patch import install as install_multiples_home_small_systems
from live_yankee_credit_pacing_patch import install as install_live_yankee_credit_pacing

BUILD_VERSION = "0.19.104"

# The adaptive dashboard's final homepage renderer resolves BUILD_VERSION from
# its module globals at request time, so update that release identity together
# with every public version surface before exposing the app.
base.BUILD_VERSION = BUILD_VERSION
base.adaptive_core.DASHBOARD_VERSION = BUILD_VERSION
base.dashboard_responsiveness.BUILD_VERSION = BUILD_VERSION
base.curated.dashboard_layout_v2.VERSION = BUILD_VERSION
base.curated.base_app.web_module.VERSION = BUILD_VERSION
base.curated.base_app.app.version = BUILD_VERSION

# Reallocate paid Odds-API quote capacity toward the stronger HR2 research lane.
# The adaptive bootstrap installs several historical budget wrappers. Some of
# those wrappers resolve their caps dynamically on every refresh, so changing
# only hit_rate_multiples_lab.DAILY_REFRESH_CREDIT_BUDGET is not sufficient.
# Synchronise every still-live HR authority here so 2,000 survives subsequent
# refresh cycles instead of being reset to an old 400/500/1,000 ceiling.
HIT_RATE_DAILY_CREDITS = 2_000
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
