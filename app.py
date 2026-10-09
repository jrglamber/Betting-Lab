"""Railway entry point for Project Exit Plan — Betting Lab."""
from web import app, db, BASE_STYLE
from sports_predictive_dashboard import install as install_sport_predictive_dashboard
from research_extensions import install as install_research_extensions

install_sport_predictive_dashboard(app, db, BASE_STYLE)
install_research_extensions(app, db, BASE_STYLE)

__all__=["app"]
