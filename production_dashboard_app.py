"""Final Railway production bootstrap for Betting Lab.

This module imports the established adaptive dashboard stack, then installs the
production-only forward research/runtime controls explicitly under Railway's
real entrypoint. It intentionally does not rely on Python's optional
sitecustomize startup hook.
"""

import adaptive_dashboard_app as base
from hit_rate_trixie_patch import install as install_hit_rate_small_systems
from live_yankee_credit_pacing_patch import install as install_live_yankee_credit_pacing

BUILD_VERSION = "0.19.97"

# The adaptive dashboard's final homepage renderer resolves BUILD_VERSION from
# its module globals at request time, so update that release identity together
# with every public version surface before exposing the app.
base.BUILD_VERSION = BUILD_VERSION
base.adaptive_core.DASHBOARD_VERSION = BUILD_VERSION
base.dashboard_responsiveness.BUILD_VERSION = BUILD_VERSION
base.curated.dashboard_layout_v2.VERSION = BUILD_VERSION
base.curated.base_app.web_module.VERSION = BUILD_VERSION
base.curated.base_app.app.version = BUILD_VERSION

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

# Live Yankee quote pacing is installed last so older startup patches cannot
# silently restore the previous 1,500-credit unpaced ceiling. The hard ceiling
# is 2,000/day, with cumulative UTC gates that preserve later-day headroom.
try:
    live_yankee_credit_pacing = install_live_yankee_credit_pacing()
except Exception as exc:
    live_yankee_credit_pacing = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"LIVE_YANKEE_CREDIT_PACING_INSTALL {live_yankee_credit_pacing}", flush=True)
print(f"BETTING_LAB_PRODUCTION_RELEASE_VERSION {BUILD_VERSION}", flush=True)

app = base.app
