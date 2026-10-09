"""Railway entry point for Project Exit Plan — Betting Lab."""
from web import app, db, BASE_STYLE, predictive_football_pred4_engine
from sports_predictive_dashboard import install as install_sport_predictive_dashboard
from research_extensions import install as install_research_extensions
from pred4_coverage_diagnostic import install as install_pred4_coverage_diagnostic

install_sport_predictive_dashboard(app, db, BASE_STYLE)
install_research_extensions(app, db, BASE_STYLE)
install_pred4_coverage_diagnostic(app, db, predictive_football_pred4_engine)

__all__=["app"]
