"""Production bootstrap that layers the curated homepage over the existing app."""

import app as base_app
from dashboard_layout_v2 import install as install_dashboard_layout_v2

install_dashboard_layout_v2(base_app.app, base_app.db, base_app.BASE_STYLE)

app = base_app.app
