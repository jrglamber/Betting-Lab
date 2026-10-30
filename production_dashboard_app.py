"""Final Railway production bootstrap for Betting Lab.

This module imports the established adaptive dashboard stack, then installs the
HR2 small-system research streams explicitly under the real Railway entrypoint.
It intentionally does not rely on Python's optional sitecustomize startup hook.
"""

import adaptive_dashboard_app as base
from hit_rate_trixie_patch import install as install_hit_rate_small_systems

BUILD_VERSION = "0.19.96"

# The adaptive dashboard's final homepage renderer resolves BUILD_VERSION from
# its module globals at request time, so update that release identity together
# with every public version surface before exposing the app.
base.BUILD_VERSION = BUILD_VERSION
base.adaptive_core.DASHBOARD_VERSION = BUILD_VERSION
base.dashboard_responsiveness.BUILD_VERSION = BUILD_VERSION
base.curated.dashboard_layout_v2.VERSION = BUILD_VERSION
base.curated.base_app.web_module.VERSION = BUILD_VERSION
base.curated.base_app.app.version = BUILD_VERSION

# Explicit production install. v0.19.95 contained the module in the repository,
# but Railway's uvicorn entrypoint did not execute the sitecustomize bootstrap,
# so no Double/Trixie forward collection actually began. v0.19.96 is therefore
# the true prospective start for these challengers.
try:
    hit_rate_small_systems = install_hit_rate_small_systems()
except Exception as exc:
    hit_rate_small_systems = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"HIT_RATE_HR2_SMALL_SYSTEMS_INSTALL {hit_rate_small_systems}", flush=True)
print(f"BETTING_LAB_PRODUCTION_RELEASE_VERSION {BUILD_VERSION}", flush=True)

app = base.app
