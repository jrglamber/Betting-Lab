from __future__ import annotations

"""25 Oct 2026 research-priority refresh.

Research-only changes from the latest export audit:
- freeze a wider NFL 2.00-3.99 cohort prospectively;
- freeze TS1 10.00-14.99% model-edge cohort prospectively;
- freeze broad PRED4 as a forward watch without rewriting older cohorts;
- keep explicit priority/downgrade/conflict/control labels visible on the homepage;
- export the priority registry;
- audit XS2 as missing rather than recreating an unknown strategy definition.

No staking, bet placement, sizing, alerting, or execution authority is changed.
"""

import inspect
from html import escape
from typing import Any, Dict, Sequence

from fastapi.responses import HTMLResponse

import outcome_edge as oe
import web as web_module
from db import utc_now_iso

VERSION = "RESEARCH_PRIORITIES_2026_10_25_V1"
_INSTALLED = False
_ORIGINAL_WATCH_ROWS = None
_ORIGINAL_ENSURE_WATCH = None

NEW_COHORTS = (
    (
        "NFL_2_TO_3_99",
        "NFL H2H 2.00–3.99",
        "Multi-Sport NFL-family H2H settled selections with executable entry odds >=2.00 and <4.00; frozen prospectively on 25 Oct 2026.",
    ),
    (
        "TS1_EDGE_10_TO_14_99",
        "TS1 edge 10.00–14.99%",
        "TS1 tennis settled H2H selections with frozen model edge >=10.00% and <15.00%; frozen prospectively on 25 Oct 2026.",
    ),
    (
        "PRED4_ALL_FORWARD_WATCH",
        "PRED4 broad forward watch",
        "All settled PRED4 selections across H2H and derived markets; best stored executable duplicate retained; frozen prospectively on 25 Oct 2026.",
    ),
)

PRIORITIES = (
    (10, "NFL_2_TO_3_99", "NFL 2.00–3.99", "PRIORITY_FORWARD", "/multisport", "Best current combination of sample, P&L and positive clean CLV; preserve the older NFL 2.00–2.99 freeze and track the wider band independently."),
    (20, "TS1_EDGE_10_TO_14_99", "TS1 tennis edge 10–15%", "PRIORITY_FORWARD", "/tennis", "Emerging profit + mean/median CLV pocket; sample remains small, so freeze now and require forward confirmation."),
    (30, "PRED4_ALL_FORWARD_WATCH", "PRED4 broad", "KEEP_FORWARD", "/predictive-football-pred4", "Broad PRED4 remains the football model with the strongest combined outcome/CLV evidence; track new observations without narrowing retrospectively."),
    (40, "NCAAF_3_TO_4_99", "NCAAF 3.00–4.99", "PNL_CLV_CONFLICT", "/multisport", "Large forward P&L signal conflicts with negative forward CLV; keep prominent but do not scale or call it validated."),
    (50, "PRED4_STRONG_2_TO_3_99", "PRED4 strong 2.00–3.99", "DOWNGRADED", "/outcome-edge", "Discovery performance regressed materially in the forward sample. Keep the original freeze as evidence; do not redefine it."),
    (60, "LIVE_YANKEE_TOLERANCE_V2", "Live Yankee P100/P98/P96/P94/P90/P85", "CONTINUE_FORWARD", "/live-yankee", "Continue the clean forward price-tolerance experiment. Do not infer an optimum until matched variant cards settle."),
    (70, "PRED56_ALL3_POSITIVE", "PRED5/PRED6 all-3-positive agreement", "WATCH_CLV", "/research-progress", "Results are not yet positive enough to promote, but the early price signal is stronger than the broad challenger pool."),
    (80, "HP2_HP3_LONGSHOT_CONTROLS", "HP2 / HP3 / generic 4.00–7.49", "CONTROL_ONLY", "/high-payout-shadow", "Forward results and underlying singles do not support promotion. Retain only as controls unless a genuinely independent filter creates a new cohort."),
    (90, "FHG1_PRICE_AND_SETTLEMENT_V2", "First-half goal V2 plumbing", "FIX_WIRED", "/first-half-goal", "V2 frozen entry-price capture and timely settlement are explicitly wired at production bootstrap; keep monitoring price coverage and stale settlements."),
    (100, "XS2_REPOSITORY_AUDIT", "XS2", "AUDIT_MISSING", "/xs1", "Current repository contains XS1 but no recoverable XS2 implementation/specification. Do not recreate an unknown strategy from guesswork; keep this visible until an authoritative definition is recovered."),
)


def _ensure_schema(db) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS research_priority_registry(
            priority_key TEXT PRIMARY KEY,
            rank_order INTEGER NOT NULL,
            label TEXT NOT NULL,
            status TEXT NOT NULL,
            href TEXT,
            rationale TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version TEXT NOT NULL,
            betting_authority INTEGER NOT NULL DEFAULT 0
        )"""
    )


def _sync_priorities(db) -> None:
    _ensure_schema(db)
    stamp = utc_now_iso()
    for rank_order, key, label, status, href, rationale in PRIORITIES:
        db.execute(
            """INSERT INTO research_priority_registry(
                 priority_key,rank_order,label,status,href,rationale,updated_at,version,betting_authority
               ) VALUES(?,?,?,?,?,?,?,?,0)
               ON CONFLICT(priority_key) DO UPDATE SET
                 rank_order=excluded.rank_order,label=excluded.label,status=excluded.status,
                 href=excluded.href,rationale=excluded.rationale,updated_at=excluded.updated_at,
                 version=excluded.version,betting_authority=0""",
            (key, int(rank_order), label, status, href, rationale, stamp, VERSION),
        )


def _settled_rows(db, sql: str, params: Sequence[Any] = (), *, source: str = "") -> list[dict[str, Any]]:
    try:
        rows = db.fetchall(sql, tuple(params))
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for raw in rows:
        hit = oe._result_hit(raw.get("result"))
        if hit is None:
            continue
        item = dict(raw)
        item["hit"] = hit
        if source:
            item["source"] = source
        out.append(item)
    return out


def _nfl_2_to_3_99(db) -> list[dict[str, Any]]:
    rows = _settled_rows(
        db,
        """SELECT b.event_id,'h2h' AS market_key,b.selection,NULL AS point,
                  b.offered_odds,b.result,b.clv_pct,b.clv_quality,b.net_pnl_units,b.created_at,
                  COALESCE(b.sport_key,e.sport_key) AS sport_key
           FROM multisport_execution_bets b
           LEFT JOIN multisport_events e ON e.event_id=b.event_id
           WHERE b.result IS NOT NULL AND b.offered_odds>=2.0 AND b.offered_odds<4.0""",
        source="MULTISPORT",
    )
    return oe._dedupe_rows([
        r for r in rows if "nfl" in str(r.get("sport_key") or "").lower()
    ])


def _ts1_edge_10_to_14_99(db) -> list[dict[str, Any]]:
    rows = _settled_rows(
        db,
        """SELECT event_id,'h2h' AS market_key,selection,NULL AS point,
                  offered_odds,result,clv_pct,clv_quality,net_pnl_units,created_at,edge_pct
           FROM tennis_execution_bets
           WHERE result IS NOT NULL AND edge_pct>=10.0 AND edge_pct<15.0""",
        source="TS1",
    )
    return oe._dedupe_rows(rows)


def _pred4_all(db) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows.extend(_settled_rows(
        db,
        """SELECT event_id,'h2h' AS market_key,selection,NULL AS point,
                  offered_odds,result,clv_pct,clv_quality,net_pnl_units,created_at
           FROM football_predictive4_bets
           WHERE result IS NOT NULL AND offered_odds>1.0""",
        source="PRED4",
    ))
    rows.extend(_settled_rows(
        db,
        """SELECT event_id,market_key,selection,point,
                  offered_odds,result,clv_pct,clv_quality,net_pnl_units,created_at
           FROM football_predictive4_market_bets
           WHERE result IS NOT NULL AND offered_odds>1.0""",
        source="PRED4",
    ))
    return oe._dedupe_rows(rows)


def _watch_rows(db, cohort_key: str) -> list[dict[str, Any]]:
    if cohort_key == "NFL_2_TO_3_99":
        return _nfl_2_to_3_99(db)
    if cohort_key == "TS1_EDGE_10_TO_14_99":
        return _ts1_edge_10_to_14_99(db)
    if cohort_key == "PRED4_ALL_FORWARD_WATCH":
        return _pred4_all(db)
    return _ORIGINAL_WATCH_ROWS(db, cohort_key)


def _ensure_watch(db):
    _ORIGINAL_ENSURE_WATCH(db)
    stamp = utc_now_iso()
    for key, label, definition in NEW_COHORTS:
        existing = db.fetchone(
            "SELECT cohort_key FROM outcome_edge_watch_cohorts WHERE cohort_key=?",
            (key,),
        )
        if existing:
            continue
        db.execute(
            "INSERT INTO outcome_edge_watch_cohorts(cohort_key,label,definition,frozen_at,created_at) VALUES(?,?,?,?,?)",
            (key, label, definition, stamp, stamp),
        )
    return db.fetchall("SELECT * FROM outcome_edge_watch_cohorts ORDER BY cohort_key")


def _priority_rows(db) -> list[dict[str, Any]]:
    _sync_priorities(db)
    reports = {str(x.get("cohort_key")): x for x in oe.watch_cohort_report(db)}
    rows = []
    for raw in db.fetchall("SELECT * FROM research_priority_registry ORDER BY rank_order,priority_key"):
        row = dict(raw)
        report = reports.get(str(row.get("priority_key") or ""))
        if report:
            row["discovery_sample"] = report.get("discovery_sample") or {}
            row["forward_sample"] = report.get("forward_sample") or {}
            row["all_time"] = report.get("all_time") or {}
            row["frozen_at"] = report.get("frozen_at")
        rows.append(row)
    return rows


def _fmt(value: Any, digits: int = 1, suffix: str = "") -> str:
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except Exception:
        return "—"


def _status_class(status: str) -> str:
    status = str(status or "").upper()
    if status in {"PRIORITY_FORWARD", "KEEP_FORWARD", "CONTINUE_FORWARD", "FIX_WIRED", "WATCH_CLV"}:
        return "ok"
    if status in {"PNL_CLV_CONFLICT", "DOWNGRADED", "AUDIT_MISSING"}:
        return "warn"
    return ""


def _priority_section(db) -> str:
    trs = []
    for row in _priority_rows(db):
        forward = dict(row.get("forward_sample") or {})
        all_time = dict(row.get("all_time") or {})
        status = str(row.get("status") or "")
        href = escape(str(row.get("href") or "#"))
        label = escape(str(row.get("label") or row.get("priority_key") or ""))
        rationale = escape(str(row.get("rationale") or ""))
        trs.append(
            f"<tr><td>{int(row.get('rank_order') or 0)}</td>"
            f"<td><a href='{href}'><strong>{label}</strong></a><br><span class='muted'>{rationale}</span></td>"
            f"<td><span class='pill {_status_class(status)}'>{escape(status)}</span></td>"
            f"<td>{int(all_time.get('selections') or 0)}</td>"
            f"<td>{_fmt(all_time.get('flat_stake_roi_pct'),1,'%')}</td>"
            f"<td>{_fmt(all_time.get('avg_ab_clv_pct'),2,'%')}</td>"
            f"<td>{int(forward.get('selections') or 0)}</td>"
            f"<td>{_fmt(forward.get('flat_stake_roi_pct'),1,'%')}</td>"
            f"<td>{_fmt(forward.get('avg_ab_clv_pct'),2,'%')}</td></tr>"
        )
    return (
        "<section id='priorities'><div class='section-title'><div><h2>Priority Watch — 25 Oct audit</h2>"
        "<div class='muted'>Prospective priorities and explicit downgrades. Status labels are research governance only, never betting authority.</div>"
        "</div><a href='/research-priorities'>Full priority detail →</a></div>"
        "<div class='panel'><div class='table-wrap'><table><thead><tr><th>#</th><th>Lane / rationale</th><th>Status</th>"
        "<th>All-time n</th><th>All-time ROI</th><th>All-time A/B CLV</th><th>Forward n</th><th>Forward ROI</th><th>Forward A/B CLV</th>"
        f"</tr></thead><tbody>{''.join(trs)}</tbody></table></div></div></section>"
    )


def _priority_page(db, base_style: str) -> str:
    section = _priority_section(db)
    return (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Research Priorities</title><style>{base_style}body{{max-width:1500px;margin:auto}}.table-wrap{{overflow-x:auto}}</style></head><body>"
        "<div class='nav'><a href='/'>← Betting Lab</a><a href='/outcome-edge'>Outcome Edge</a><a href='/research-progress'>Research Progress</a></div>"
        "<h1>Research Priorities <span class='pill'>SHADOW · NO EXECUTION AUTHORITY</span></h1>"
        "<p class='muted'>Frozen forward cohorts are separated from discovery history. Existing cohort boundaries are never moved by this page.</p>"
        f"{section}</body></html>"
    )


def _surface_home_priorities(app, db) -> bool:
    route = next((
        r for r in list(app.router.routes)
        if getattr(r, "path", None) == "/" and "GET" in (getattr(r, "methods", set()) or set())
    ), None)
    if route is None:
        return False
    original = route.endpoint
    name = route.name
    include = getattr(route, "include_in_schema", True)
    app.router.routes.remove(route)

    async def wrapped():
        response = original()
        if inspect.isawaitable(response):
            response = await response
        if not isinstance(response, HTMLResponse):
            return response
        html = response.body.decode("utf-8")
        if "id='priorities'" not in html:
            marker = "<section id='multiples'>"
            section = _priority_section(db)
            html = html.replace(marker, section + marker, 1) if marker in html else html.replace("</body>", section + "</body>", 1)
        if "href='#priorities'" not in html:
            marker = "<a href='#leaderboard'>Research Leaderboard</a>"
            link = "<a href='#priorities'>Priority Watch</a>"
            html = html.replace(marker, marker + link, 1) if marker in html else html
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return HTMLResponse(content=html, status_code=response.status_code, headers=headers)

    app.add_api_route("/", wrapped, methods=["GET"], response_class=HTMLResponse, name=name, include_in_schema=include)
    return True


def _patch_exports() -> None:
    table = "research_priority_registry"
    try:
        import exporter
        exporter.EXPORT_TABLES = tuple(dict.fromkeys(tuple(exporter.EXPORT_TABLES) + (table,)))
    except Exception:
        pass
    try:
        import split_exporter
        split_exporter.WEEKLY_TABLES = tuple(dict.fromkeys(tuple(split_exporter.WEEKLY_TABLES) + (table,)))
        split_exporter.MANUAL_TABLES = tuple(dict.fromkeys(tuple(split_exporter.MANUAL_TABLES) + (table,)))
    except Exception:
        pass


def install(app, db, base_style: str) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_WATCH_ROWS, _ORIGINAL_ENSURE_WATCH
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    _ORIGINAL_WATCH_ROWS = oe._watch_cohort_rows
    _ORIGINAL_ENSURE_WATCH = oe.ensure_watch_cohorts
    oe._watch_cohort_rows = _watch_rows
    oe.ensure_watch_cohorts = _ensure_watch
    web_module.ensure_watch_cohorts = _ensure_watch

    _ensure_watch(db)
    _sync_priorities(db)
    _patch_exports()

    @app.get("/api/research-priorities")
    def api_research_priorities():
        return {
            "version": VERSION,
            "authority": "RESEARCH_ONLY_NO_EXECUTION",
            "priorities": _priority_rows(db),
        }

    @app.get("/research-priorities")
    def research_priorities_page():
        return HTMLResponse(_priority_page(db, base_style))

    home_link = _surface_home_priorities(app, db)
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "new_frozen_cohorts": [x[0] for x in NEW_COHORTS],
        "priority_rows": len(PRIORITIES),
        "home_priority_panel": home_link,
        "xs2_status": "AUDIT_MISSING_NO_SPEC_RECREATED",
        "provider_calls_added": 0,
        "betting_authority": False,
    }
