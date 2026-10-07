"""Railway entry point for Project Exit Plan — Betting Lab."""
from web import app, db, BASE_STYLE
from sports_predictive_dashboard import install as install_sport_predictive_dashboard

install_sport_predictive_dashboard(app, db, BASE_STYLE)

__all__=["app"]
