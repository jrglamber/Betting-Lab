"""Final Railway production bootstrap for Betting Lab.

This module imports the established adaptive dashboard stack, then installs the
production-only forward research/runtime controls explicitly under Railway's
real entrypoint. It intentionally does not rely on Python's optional
sitecustomize startup hook.
"""

import adaptive_dashboard_app as base
import credit_policy_unlimited_patch as credit_policy
import hit_rate_multiples_lab as hit_rate_lab
import live_yankee_feed as live_yankee_feed
import live_yankee_credit_pacing_patch as live_yankee_pacing
import multiples_credit_efficiency_patch as multiples_efficiency
from hit_rate_trixie_patch import install as install_hit_rate_small_systems
from multiples_home_small_systems_patch import install as install_multiples_home_small_systems
from live_yankee_credit_pacing_patch import install as install_live_yankee_credit_pacing

BUILD_VERSION = "0.19.99"

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
# The base dashboard installs the generic target filters before this final
# production bootstrap. Those filters resolve their timing constants dynamically,
# so these overrides tighten Live Yankee polling without replacing its logic.
# Selection, same-book/card construction, settlement, staking and alerts are
# unchanged.
HIT_RATE_DAILY_CREDITS = 2_000
LIVE_YANKEE_DAILY_CREDITS = 500
LIVE_YANKEE_QUOTE_REFRESH_MINUTES = 10.0
LIVE_YANKEE_MAX_EVENTS_PER_CYCLE = 4

credit_policy.HIT_RATE_DAILY_CREDITS = HIT_RATE_DAILY_CREDITS
credit_policy.LIVE_YANKEE_DAILY_CREDITS = LIVE_YANKEE_DAILY_CREDITS
credit_policy.REPEATED_MISS_POLLS = 1
credit_policy.MID_BACKOFF_MINUTES = 60.0
credit_policy.FAR_BACKOFF_MINUTES = 120.0
credit_policy.RECENT_POLL_WINDOW_MINUTES = 180.0
credit_policy.CLOSE_TO_KICKOFF_MINUTES = 60.0

hit_rate_lab.DAILY_REFRESH_CREDIT_BUDGET = HIT_RATE_DAILY_CREDITS
live_yankee_feed.DAILY_REFRESH_CREDIT_BUDGET = LIVE_YANKEE_DAILY_CREDITS
live_yankee_feed.QUOTE_REFRESH_MINUTES = LIVE_YANKEE_QUOTE_REFRESH_MINUTES
live_yankee_feed.MAX_REFRESH_EVENTS_PER_CYCLE = LIVE_YANKEE_MAX_EVENTS_PER_CYCLE

# Replace the previous 2,000-credit Live Yankee hard ceiling with a 500-credit
# benchmark budget and spread it across the UTC day. If today's existing spend is
# already above 500, no further Live Yankee quote calls are allowed until the
# provider accounting day resets.
live_yankee_pacing.HARD_DAILY_CAP = LIVE_YANKEE_DAILY_CREDITS
live_yankee_pacing.PACING_STAGES = (
    (6, 100),
    (10, 200),
    (14, 300),
    (18, 400),
    (24, 500),
)

# Explicit HR2 small-system install. v0.19.96 is the true prospective start for
# the Double/Trixie challengers.
try:
    hit_rate_small_systems = install_hit_rate_small_systems()
except Exception as exc:
    hit_rate_small_systems = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"HIT_RATE_HR2_SMALL_SYSTEMS_INSTALL {hit_rate_small_systems}", flush=True)

# The curated homepage predates Double/Trixie and hard-codes only Yankee/Heinz.
# Surface the two active HR2 forward challengers in the Multiples Research table
# without changing collection, pricing, selection or betting authority.
try:
    multiples_home_small_systems = install_multiples_home_small_systems()
except Exception as exc:
    multiples_home_small_systems = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"MULTIPLES_HOME_SMALL_SYSTEMS_INSTALL {multiples_home_small_systems}", flush=True)

# Install the Live Yankee pacing wrapper last so older startup patches cannot
# silently restore their larger ceilings. Then correct the optional per-lane
# reporting caps to sum to the new 500-credit hard ceiling.
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
    live_yankee_credit_pacing = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
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
        "selection_rules_changed": False,
        "card_rules_changed": False,
        "betting_authority_changed": False,
    }),
    flush=True,
)
print(f"BETTING_LAB_PRODUCTION_RELEASE_VERSION {BUILD_VERSION}", flush=True)

app = base.app
