"""Railway entry point for Project Exit Plan — Betting Lab."""
from datetime import datetime, timezone
from html import escape
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
web_module.VERSION = "0.19.36"

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


def _expand_clv_first_sources():
    """Make CLV-First observe every current selection-producing betting lane.

    Duplicates are intentional at capture time. A selection may be represented by
    more than one model/lane; later analysis can deduplicate on the underlying
    event/market/selection key. Missing a lane would be a worse failure than
    recording the same underlying pick more than once.
    """
    additional = (
        ("football_predictive_bets", "events", "PRED1_H2H"),
        ("football_predictive_market_bets", "events", "PRED1_MARKETS"),
        ("football_predictive2_bets", "events", "PRED2_H2H"),
        ("football_predictive2_market_bets", "events", "PRED2_MARKETS"),
        ("football_predictive3_bets", "events", "PRED3_H2H"),
        ("football_predictive3_market_bets", "events", "PRED3_MARKETS"),
        ("football_predictive4_bets", "events", "PRED4_H2H"),
        ("football_predictive4_market_bets", "events", "PRED4_MARKETS"),
        # PRED5/PRED6 and related Edge Research challengers copy their own
        # selection-level CLV into this table. Include them as well even though
        # they can duplicate a PRED4 underlying selection.
        ("football_predictive_research_samples", "events", "FOOTBALL_RESEARCH"),
    )
    clv_first.SOURCE_SPECS = tuple(dict.fromkeys(tuple(clv_first.SOURCE_SPECS) + additional))


def _parse_utc(value):
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if out.tzinfo is None:
            out = out.replace(tzinfo=timezone.utc)
        return out.astimezone(timezone.utc)
    except Exception:
        return None


def _clv_first_ingestion_health():
    """Audit source-to-CLV-First coverage independently of model performance."""
    start = clv_first.forward_start(db)
    start_iso = start.isoformat()
    source_rows = []
    total_source = total_captured = total_missing = 0
    total_forward_source = total_forward_captured = total_forward_missing = 0
    source_errors = 0

    for table, _, lane in clv_first.SOURCE_SPECS:
        row = {
            "source_table": table,
            "source_lane": lane,
            "source_rows": 0,
            "captured_rows": 0,
            "missing_rows": 0,
            "forward_source_rows": 0,
            "forward_captured_rows": 0,
            "forward_missing_rows": 0,
            "latest_source_at": None,
            "latest_captured_at": None,
            "status": "HEALTHY",
        }
        try:
            src = db.fetchone(
                f"""SELECT COUNT(*) AS n,
                           MAX(created_at) AS latest_at,
                           SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END) AS forward_n
                    FROM {table}""",
                (start_iso,),
            ) or {}
            cap = db.fetchone(
                """SELECT COUNT(*) AS n,
                          MAX(created_at) AS latest_at,
                          SUM(CASE WHEN evidence_mode='FORWARD' THEN 1 ELSE 0 END) AS forward_n
                   FROM clv_first_samples WHERE source_table=?""",
                (table,),
            ) or {}
            miss = db.fetchone(
                f"""SELECT COUNT(*) AS n
                    FROM {table} t
                    WHERE NOT EXISTS (
                      SELECT 1 FROM clv_first_samples c
                      WHERE c.source_table=? AND c.source_bet_id=t.id
                    )""",
                (table,),
            ) or {}
            miss_fwd = db.fetchone(
                f"""SELECT COUNT(*) AS n
                    FROM {table} t
                    WHERE t.created_at>=?
                      AND NOT EXISTS (
                        SELECT 1 FROM clv_first_samples c
                        WHERE c.source_table=? AND c.source_bet_id=t.id
                      )""",
                (start_iso, table),
            ) or {}

            row["source_rows"] = int(src.get("n") or 0)
            row["captured_rows"] = int(cap.get("n") or 0)
            row["missing_rows"] = int(miss.get("n") or 0)
            row["forward_source_rows"] = int(src.get("forward_n") or 0)
            row["forward_captured_rows"] = int(cap.get("forward_n") or 0)
            row["forward_missing_rows"] = int(miss_fwd.get("n") or 0)
            row["latest_source_at"] = src.get("latest_at")
            row["latest_captured_at"] = cap.get("latest_at")
            if row["missing_rows"] or row["forward_missing_rows"]:
                row["status"] = "LAGGING"
        except Exception as exc:
            row["status"] = "ERROR"
            row["error"] = f"{type(exc).__name__}: {exc}"
            source_errors += 1

        total_source += row["source_rows"]
        total_captured += row["captured_rows"]
        total_missing += row["missing_rows"]
        total_forward_source += row["forward_source_rows"]
        total_forward_captured += row["forward_captured_rows"]
        total_forward_missing += row["forward_missing_rows"]
        source_rows.append(row)

    latest_run = db.fetchone(
        """SELECT started_at,finished_at,ok,detail
           FROM collector_runs
           WHERE run_type='CLV_FIRST_MAINT'
           ORDER BY id DESC LIMIT 1"""
    ) or {}
    run_at = _parse_utc(latest_run.get("finished_at") or latest_run.get("started_at"))
    run_age_minutes = None
    if run_at:
        run_age_minutes = max(0.0, (datetime.now(timezone.utc) - run_at).total_seconds() / 60.0)

    if source_errors or (latest_run and not bool(latest_run.get("ok"))):
        status = "ERROR"
    elif not latest_run:
        status = "WAITING"
    elif run_age_minutes is not None and run_age_minutes > 25:
        status = "STALE"
    elif total_missing or total_forward_missing:
        status = "LAGGING"
    else:
        status = "HEALTHY"

    coverage_pct = (total_captured / total_source * 100.0) if total_source else 100.0
    forward_coverage_pct = (
        total_forward_captured / total_forward_source * 100.0
        if total_forward_source else 100.0
    )
    return {
        "status": status,
        "forward_start_at": start_iso,
        "source_lanes": len(source_rows),
        "source_rows": total_source,
        "captured_rows": total_captured,
        "missing_rows": total_missing,
        "coverage_pct": coverage_pct,
        "forward_source_rows": total_forward_source,
        "forward_captured_rows": total_forward_captured,
        "forward_missing_rows": total_forward_missing,
        "forward_coverage_pct": forward_coverage_pct,
        "last_maintenance_at": latest_run.get("finished_at") or latest_run.get("started_at"),
        "last_maintenance_ok": latest_run.get("ok"),
        "last_maintenance_age_minutes": run_age_minutes,
        "sources": source_rows,
    }


def _surface_clv_first_health():
    """Add an end-to-end ingestion health panel to the CLV-First dashboard."""
    route = next(
        (
            item for item in list(app.router.routes)
            if getattr(item, "path", None) == "/clv-first"
            and "GET" in (getattr(item, "methods", set()) or set())
        ),
        None,
    )
    if route is None:
        raise RuntimeError("CLV-First dashboard route not found")

    original_endpoint = route.endpoint
    route_name = route.name
    include_in_schema = getattr(route, "include_in_schema", True)
    app.router.routes.remove(route)

    async def clv_first_dashboard_with_health():
        response = original_endpoint()
        if inspect.isawaitable(response):
            response = await response
        if not isinstance(response, HTMLResponse):
            return response

        health = _clv_first_ingestion_health()
        status = str(health.get("status") or "UNKNOWN")
        status_css = "ok" if status == "HEALTHY" else "warn" if status in {"WAITING", "LAGGING", "STALE"} else "bad"
        rows = []
        for item in health.get("sources", []):
            row_status = str(item.get("status") or "UNKNOWN")
            row_css = "ok" if row_status == "HEALTHY" else "warn" if row_status == "LAGGING" else "bad"
            rows.append(
                f"<tr><td><strong>{escape(str(item.get('source_lane') or ''))}</strong>"
                f"<div class='muted'>{escape(str(item.get('source_table') or ''))}</div></td>"
                f"<td class='{row_css}'>{escape(row_status)}</td>"
                f"<td>{item.get('source_rows',0)}</td><td>{item.get('captured_rows',0)}</td>"
                f"<td>{item.get('missing_rows',0)}</td><td>{item.get('forward_source_rows',0)}</td>"
                f"<td>{item.get('forward_captured_rows',0)}</td><td>{item.get('forward_missing_rows',0)}</td>"
                f"<td>{escape(str(item.get('latest_source_at') or '—'))}</td></tr>"
            )
        source_table = "".join(rows) or "<tr><td colspan='9'>No configured source lanes.</td></tr>"
        age = health.get("last_maintenance_age_minutes")
        age_text = "—" if age is None else f"{float(age):.1f} min ago"
        panel = f"""
        <div class='panel'><h2>Ingestion health <span class='pill {status_css}'>{escape(status)}</span></h2>
          <p>Coverage <strong>{health.get('captured_rows',0)}/{health.get('source_rows',0)}</strong>
          ({float(health.get('coverage_pct') or 0):.2f}%) · missing <strong>{health.get('missing_rows',0)}</strong></p>
          <p>Forward source selections <strong>{health.get('forward_source_rows',0)}</strong> · captured
          <strong>{health.get('forward_captured_rows',0)}</strong> · missed <strong>{health.get('forward_missing_rows',0)}</strong></p>
          <p class='muted'>Last CLV-First maintenance: {escape(str(health.get('last_maintenance_at') or '—'))} ({escape(age_text)}).
          A zero forward sample is safe when this panel is HEALTHY and forward source selections are also zero.</p>
          <div class='table-wrap'><table><thead><tr><th>Lane</th><th>Status</th><th>Source</th><th>Captured</th><th>Missing</th>
          <th>Forward source</th><th>Forward captured</th><th>Forward missed</th><th>Latest source</th></tr></thead>
          <tbody>{source_table}</tbody></table></div>
        </div>"""

        html = response.body.decode("utf-8")
        marker = "<div class=grid>"
        if marker in html:
            html = html.replace(marker, panel + marker, 1)
        else:
            html = html.replace("</h1>", "</h1>" + panel, 1)
        headers = {
            key: value for key, value in response.headers.items()
            if key.lower() != "content-length"
        }
        return HTMLResponse(content=html, status_code=response.status_code, headers=headers)

    app.add_api_route(
        "/clv-first",
        clv_first_dashboard_with_health,
        methods=["GET"],
        response_class=HTMLResponse,
        name=route_name,
        include_in_schema=include_in_schema,
    )

    @app.get("/api/clv-first/health")
    def clv_first_health_api():
        return _clv_first_ingestion_health()


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
_expand_clv_first_sources()
install_sport_predictive_dashboard(app, db, BASE_STYLE)
install_research_extensions(app, db, BASE_STYLE)
install_clv_first(app, db, BASE_STYLE)
_surface_clv_first_health()
try:
    print("CLV_FIRST_INSTALL " + json.dumps(clv_first.scoreboard(db), sort_keys=True, default=str), flush=True)
except Exception as exc:
    print(f"CLV_FIRST_INSTALL_ERROR {type(exc).__name__}: {exc}", flush=True)
install_pred4_coverage_diagnostic(app, db, predictive_football_pred4_engine)
install_research_progress_dashboard(app, db, BASE_STYLE)
_surface_clv_first_on_home()

__all__=["app"]
