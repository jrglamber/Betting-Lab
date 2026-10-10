"""Railway entry point for Project Exit Plan — Betting Lab."""
from datetime import datetime, timezone

import web as web_module
from web import app, db, BASE_STYLE, predictive_football_pred4_engine
import sports_predictive_dashboard as sports_dashboard
from research_extensions import install as install_research_extensions
from clv_first import install as install_clv_first
from pred4_coverage_diagnostic import install as install_pred4_coverage_diagnostic
from pred4_alias_patch import apply as apply_pred4_alias_patch

# Dashboard up-rev for this research expansion. web route functions resolve VERSION
# from the module global at request time, so the visible main dashboard is updated
# without touching the large legacy web module.
web_module.VERSION = "0.19.33"

# API-Sports free access is currently historical (2022-2024). Make that visible
# rather than presenting a successful collector run as a current-data HEALTHY lane.
_base_sport_scoreboard = sports_dashboard._scoreboard

def _sport_scoreboard_with_access(db_obj, model_name):
    result = _base_sport_scoreboard(db_obj, model_name)
    season = str(result.get("season") or "")
    try:
        season_year = int(season[:4])
    except Exception:
        season_year = None
    if result.get("available") and result.get("last_run_ok") and season_year and season_year < datetime.now(timezone.utc).year:
        result["health"] = "HISTORICAL_ONLY"
        result["access_status"] = "FREE_PLAN_2022_2024"
    return result

sports_dashboard._scoreboard = _sport_scoreboard_with_access
install_sport_predictive_dashboard = sports_dashboard.install

# Import after the access-status patch so the unified research dashboard reads
# the same status function as the dedicated Sport Predictive page.
from research_progress_dashboard import install as install_research_progress_dashboard

apply_pred4_alias_patch()
install_sport_predictive_dashboard(app, db, BASE_STYLE)
install_research_extensions(app, db, BASE_STYLE)
install_clv_first(app, db, BASE_STYLE)
install_pred4_coverage_diagnostic(app, db, predictive_football_pred4_engine)
install_research_progress_dashboard(app, db, BASE_STYLE)

__all__=["app"]
