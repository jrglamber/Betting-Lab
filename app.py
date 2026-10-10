"""Railway entry point for Project Exit Plan — Betting Lab."""
from datetime import datetime, timezone
import inspect
import json

from fastapi.responses import HTMLResponse

import web as web_module
from web import app, db, BASE_STYLE, predictive_football_pred4_engine
import sports_predictive_dashboard as sports_dashboard
from research_extensions import install as install_research_extensions
import clv_first
from clv_first import install as install_clv_first
from pred4_coverage_diagnostic import install as install_pred4_coverage_diagnostic
from pred4_alias_patch import apply as apply_pred4_alias_patch

# Dashboard up-rev for this research expansion. web route functions resolve VERSION
# from the module global at request time, so the visible main dashboard is updated
# without touching the large legacy web module.
web_module.VERSION = "0.19.34"

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


def _surface_clv_first_on_home():
    """Expose the independent CLV lane in the existing Deep dives nav.

    Keep the large legacy dashboard untouched: wrap its already-registered root
    handler and inject one navigation item beside Meta Edge. This changes
    presentation only; no betting/research authority is affected.
    """
    home_route = next(
        (
            route for route in list(app.router.routes)
            if getattr(route, "path", None) == "/"
            and "GET" in (getattr(route, "methods", set()) or set())
        ),
        None,
    )
    if home_route is None:
        raise RuntimeError("Betting Lab root dashboard route not found")

    original_endpoint = home_route.endpoint
    route_name = home_route.name
    include_in_schema = getattr(home_route, "include_in_schema", True)
    app.router.routes.remove(home_route)

    async def dashboard_with_clv_first():
        response = original_endpoint()
        if inspect.isawaitable(response):
            response = await response
        if not isinstance(response, HTMLResponse):
            return response

        html = response.body.decode("utf-8")
        if "href='/clv-first'" not in html:
            link = "<a href='/clv-first'>CLV-First Research</a>"
            marker = "<a href='/meta-edge'>Meta Edge</a>"
            if marker in html:
                html = html.replace(marker, marker + link, 1)
            else:
                html = html.replace("<div class='nav'>", "<div class='nav'>" + link, 1)

        headers = {
            key: value for key, value in response.headers.items()
            if key.lower() != "content-length"
        }
        return HTMLResponse(
            content=html,
            status_code=response.status_code,
            headers=headers,
        )

    app.add_api_route(
        "/",
        dashboard_with_clv_first,
        methods=["GET"],
        response_class=HTMLResponse,
        name=route_name,
        include_in_schema=include_in_schema,
    )


apply_pred4_alias_patch()
install_sport_predictive_dashboard(app, db, BASE_STYLE)
install_research_extensions(app, db, BASE_STYLE)
install_clv_first(app, db, BASE_STYLE)
try:
    print("CLV_FIRST_INSTALL " + json.dumps(clv_first.scoreboard(db), sort_keys=True, default=str), flush=True)
except Exception as exc:
    print(f"CLV_FIRST_INSTALL_ERROR {type(exc).__name__}: {exc}", flush=True)
install_pred4_coverage_diagnostic(app, db, predictive_football_pred4_engine)
install_research_progress_dashboard(app, db, BASE_STYLE)
_surface_clv_first_on_home()

__all__=["app"]
