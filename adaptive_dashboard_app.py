"""Production bootstrap for the adaptive winner/tennis research expansion.

This layers on top of dashboard_app so every existing curated dashboard/runtime
patch remains authoritative. The new research module is shadow-only.
"""

import dashboard_app as curated
from adaptive_winner_research import DASHBOARD_VERSION, install as install_adaptive_winner_research
from adaptive_winner_research_safety_patch import install as install_adaptive_safety

safety = install_adaptive_safety(curated.base_app.db)
print(f"ADAPTIVE_WINNER_RESEARCH_SAFETY {safety}", flush=True)

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

# Keep the visible build identifier unambiguous for manual Railway verification.
curated.dashboard_layout_v2.VERSION = DASHBOARD_VERSION
curated.base_app.web_module.VERSION = DASHBOARD_VERSION
curated.base_app.app.version = DASHBOARD_VERSION

app = curated.base_app.app
