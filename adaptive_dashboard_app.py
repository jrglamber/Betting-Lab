"""Production bootstrap for the adaptive winner/tennis research expansion.

This layers on top of dashboard_app so every existing curated dashboard/runtime
patch remains authoritative. The new research module is shadow-only.
"""

import re

import dashboard_app as curated
import adaptive_winner_research as adaptive_core
from adaptive_winner_research import install as install_adaptive_winner_research
from adaptive_winner_research_safety_patch import install as install_adaptive_safety
from adaptive_tpred4_identity_patch import install as install_tpred4_identity

# The live baseline was already v0.19.77 before this research expansion.
# This deployment is therefore v0.19.78. Set the shared research constant before
# installation so startup diagnostics and every visible build surface agree.
BUILD_VERSION = "0.19.78"
adaptive_core.DASHBOARD_VERSION = BUILD_VERSION
curated.dashboard_layout_v2.VERSION = BUILD_VERSION
curated.base_app.web_module.VERSION = BUILD_VERSION
curated.base_app.app.version = BUILD_VERSION

# Install model-freeze and stable historical-identity guards before the runtime
# wrapper is attached. Both are research-only and create no provider traffic.
safety = install_adaptive_safety(curated.base_app.db)
print(f"ADAPTIVE_WINNER_RESEARCH_SAFETY {safety}", flush=True)
tpred4_identity = install_tpred4_identity(curated.base_app.db)
print(f"ADAPTIVE_TPRED4_IDENTITY {tpred4_identity}", flush=True)

adaptive = install_adaptive_winner_research(
    curated.base_app.app,
    curated.base_app.db,
    curated.base_app.BASE_STYLE,
    tennis_engine=getattr(curated.base_app.web_module, "tennis_engine", None),
)
print(f"ADAPTIVE_WINNER_RESEARCH_INSTALL {adaptive}", flush=True)

# The maintenance route is useful during development but deliberately removed
# from the public production surface. Research refreshes run from the wrapped
# tennis cycle instead, so no unauthenticated request can trigger the heavy
# 100k-match initialization/rebuild work.
curated.base_app.app.router.routes[:] = [
    route for route in curated.base_app.app.router.routes
    if getattr(route, "path", None) != "/admin/adaptive-research/run"
]

# Final display guard. The curated homepage has accumulated wrapper layers and
# older bootstrap modules may assign their own VERSION values while importing.
# Wrap the actual page renderer at the end of startup so the rendered title and
# visible badge always match this production release, regardless of import order.
_original_page = curated.dashboard_layout_v2._page


def _page_with_release_version(db, base_style: str) -> str:
    html = _original_page(db, base_style)
    html = re.sub(
        r"(<title>Betting Lab v)[^<]+(</title>)",
        rf"\g<1>{BUILD_VERSION}\g<2>",
        html,
        count=1,
    )
    html = re.sub(
        r"(Betting Lab <span class='pill'>v)[^<]+(</span>)",
        rf"\g<1>{BUILD_VERSION}\g<2>",
        html,
        count=1,
    )
    return html


curated.dashboard_layout_v2._page = _page_with_release_version
curated.dashboard_layout_v2.VERSION = BUILD_VERSION
curated.base_app.web_module.VERSION = BUILD_VERSION
curated.base_app.app.version = BUILD_VERSION
print(f"BETTING_LAB_RELEASE_VERSION {BUILD_VERSION}", flush=True)

app = curated.base_app.app
