from __future__ import annotations

"""Settlement integrity v2.

Extends the PRED4 fix into an all-primary-ledger settlement guard:
- every football ledger creates a shared result obligation;
- old football result misses are repaired from high-confidence stored PXG/API-Football finals;
- stored tennis/multisport results are re-applied idempotently to open bets;
- stale-open health is schema-agnostic and separates missing-result rows from
  settlement-desynchronisation rows.

No model thresholds, candidate selection rules, staking, or live execution
permissions are changed.
"""

from datetime import datetime, timedelta, timezone
from html import escape
import inspect
import json
import types
from typing import Any, Dict, Mapping, Optional

from fastapi.responses import HTMLResponse

import settlement_integrity_patch as v1
from db import utc_now_iso
from results import settle_event_signals
from tennis_shadow import settle_tennis_event
from multisport_shadow import settle_multisport_event
from multisport_lines_shadow import settle_lines_from_stored_results
from execution_shadow import settle_execution_from_stored_results

VERSION = "SETTLEMENT_INTEGRITY_V2"
STALE_HOURS = 6.0
_INSTALLED = False

# lane, bet table, event table, result table
LANES = (
    ("CORE_EXECUTION", "execution_shadow_bets", "events", "event_results"),
    ("PRED1_H2H", "football_predictive_bets", "events", "event_results"),
    ("PRED1_MARKETS", "football_predictive_market_bets", "events", "event_results"),
    ("PRED2_H2H", "football_predictive2_bets", "events", "event_results"),
    ("PRED2_MARKETS", "football_predictive2_market_bets", "events", "event_results"),
    ("PRED3_H2H", "football_predictive3_bets", "events", "event_results"),
    ("PRED3_MARKETS", "football_predictive3_market_bets", "events", "event_results"),
    ("PRED4_H2H", "football_predictive4_bets", "events", "event_results"),
    ("PRED4_MARKETS", "football_predictive4_market_bets", "events", "event_results"),
    ("TENNIS", "tennis_execution_bets", "tennis_events", "tennis_results"),
    ("MULTISPORT_H2H", "multisport_execution_bets", "multisport_events", "multisport_results"),
    ("MULTISPORT_LINES", "multisport_line_bets", "multisport_events", "multisport_results"),
)


def _parse(value: Any) -> Optional[datetime]:
    return v1._parse(value)


def _all_missing_football_results(db, now: datetime):
    cutoff = now - timedelta(hours=STALE_HOURS)
    return db.fetchall(
        """
        SELECT DISTINCT e.*
        FROM events e
        LEFT JOIN event_results r ON r.event_id=e.event_id
        WHERE r.event_id IS NULL
          AND e.commence_time<=?
          AND (
            EXISTS (SELECT 1 FROM signals s WHERE s.event_id=e.event_id AND s.status='OPEN')
            OR EXISTS (SELECT 1 FROM canonical_bets c WHERE c.event_id=e.event_id AND c.status='OPEN')
            OR EXISTS (SELECT 1 FROM execution_shadow_bets x WHERE x.event_id=e.event_id AND x.status='OPEN')
            OR EXISTS (SELECT 1 FROM football_predictive_bets b WHERE b.event_id=e.event_id AND b.status='OPEN')
            OR EXISTS (SELECT 1 FROM football_predictive_market_bets b WHERE b.event_id=e.event_id AND b.status='OPEN')
            OR EXISTS (SELECT 1 FROM football_predictive2_bets b WHERE b.event_id=e.event_id AND b.status='OPEN')
            OR EXISTS (SELECT 1 FROM football_predictive2_market_bets b WHERE b.event_id=e.event_id AND b.status='OPEN')
            OR EXISTS (SELECT 1 FROM football_predictive3_bets b WHERE b.event_id=e.event_id AND b.status='OPEN')
            OR EXISTS (SELECT 1 FROM football_predictive3_market_bets b WHERE b.event_id=e.event_id AND b.status='OPEN')
            OR EXISTS (SELECT 1 FROM football_predictive4_bets b WHERE b.event_id=e.event_id AND b.status='OPEN')
            OR EXISTS (SELECT 1 FROM football_predictive4_market_bets b WHERE b.event_id=e.event_id AND b.status='OPEN')
          )
        ORDER BY e.commence_time,e.event_id
        """,
        (cutoff.isoformat(),),
    )


def repair_all_football_missing_results(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    due = _all_missing_football_results(db, now)
    repaired = []
    unresolved = []
    for event in due:
        match = v1._find_pxg_result(db, event)
        if match is None:
            unresolved.append({
                "event_id": event.get("event_id"),
                "fixture": f"{event.get('home_team')} v {event.get('away_team')}",
                "commence_time": event.get("commence_time"),
                "sport_key": event.get("sport_key"),
            })
            continue
        hs = int(match["home_goals"])
        aws = int(match["away_goals"])
        provenance = {
            "repair": VERSION,
            "fixture_id": match.get("fixture_id"),
            "pxg_played_at": match.get("played_at"),
            "home_match": round(float(match.get("match_home") or 0), 4),
            "away_match": round(float(match.get("match_away") or 0), 4),
            "time_delta_hours": round(float(match.get("time_delta_hours") or 0), 3),
        }
        db.execute(
            """
            INSERT INTO event_results(
                event_id,fetched_at,completed_at,home_score,away_score,source,raw_json,
                settlement_quality,settlement_provenance
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            (
                event["event_id"], utc_now_iso(), match.get("played_at"), hs, aws,
                "api_football_pxg_repair", json.dumps(provenance, separators=(",", ":")),
                "A", json.dumps(provenance, separators=(",", ":")),
            ),
        )
        db.execute("UPDATE events SET status='COMPLETED' WHERE event_id=?", (event["event_id"],))
        settle_event_signals(db, event["event_id"], home_score=hs, away_score=aws)
        repaired.append({
            "event_id": event.get("event_id"),
            "fixture": f"{event.get('home_team')} v {event.get('away_team')}",
            "score": f"{hs}-{aws}",
            "fixture_id": match.get("fixture_id"),
            "time_delta_hours": provenance["time_delta_hours"],
        })
    return {
        "checked": len(due),
        "repaired": len(repaired),
        "unresolved": len(unresolved),
        "repairs": repaired,
        "unresolved_examples": unresolved[:30],
    }


def _settle_predictive(web_module) -> Dict[str, Any]:
    return v1._settle_predictive(web_module)


def _reapply_stored_results(web_module) -> Dict[str, Any]:
    db = web_module.db
    out: Dict[str, Any] = {}
    try:
        out["execution"] = settle_execution_from_stored_results(db)
    except Exception as exc:
        out["execution_error"] = f"{type(exc).__name__}: {exc}"

    try:
        out["predictive"] = _settle_predictive(web_module)
    except Exception as exc:
        out["predictive_error"] = f"{type(exc).__name__}: {exc}"

    try:
        rows = db.fetchall(
            """SELECT DISTINCT b.event_id,r.winner
               FROM tennis_execution_bets b JOIN tennis_results r ON r.event_id=b.event_id
               WHERE b.status='OPEN'"""
        )
        out["tennis"] = sum(settle_tennis_event(db, r["event_id"], r["winner"]) for r in rows)
    except Exception as exc:
        out["tennis_error"] = f"{type(exc).__name__}: {exc}"

    try:
        rows = db.fetchall(
            """SELECT DISTINCT b.event_id,r.home_score,r.away_score
               FROM multisport_execution_bets b JOIN multisport_results r ON r.event_id=b.event_id
               WHERE b.status='OPEN'"""
        )
        out["multisport_h2h"] = sum(
            settle_multisport_event(db, r["event_id"], home_score=int(r["home_score"]), away_score=int(r["away_score"]))
            for r in rows
        )
    except Exception as exc:
        out["multisport_h2h_error"] = f"{type(exc).__name__}: {exc}"

    try:
        out["multisport_lines"] = settle_lines_from_stored_results(db)
    except Exception as exc:
        out["multisport_lines_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _event_label(db, event_table: str, event_id: Any) -> str:
    return v1._event_label(db, event_table, event_id)


def _row_pick_label(row: Mapping[str, Any]) -> str:
    market = row.get("market_key") or row.get("market") or "h2h"
    selection = (
        row.get("selection") or row.get("predicted_selection") or
        row.get("predicted_outcome") or row.get("side") or "—"
    )
    point = row.get("point")
    if point is None:
        point = row.get("line_point")
    suffix = "" if point is None else f" {point}"
    return f"{market} · {selection}{suffix}"


def settlement_health(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=STALE_HOURS)
    lanes = []
    total = 0
    total_missing_result = 0
    total_with_result = 0
    errors = 0
    for lane, table, event_table, result_table in LANES:
        item: Dict[str, Any] = {
            "lane": lane,
            "table": table,
            "stale_open": 0,
            "missing_result": 0,
            "with_stored_result": 0,
            "oldest_commence_time": None,
            "status": "HEALTHY",
            "examples": [],
        }
        try:
            agg = db.fetchone(
                f"""SELECT COUNT(*) AS n,MIN(e.commence_time) AS oldest,
                           SUM(CASE WHEN r.event_id IS NULL THEN 1 ELSE 0 END) AS missing_result,
                           SUM(CASE WHEN r.event_id IS NOT NULL THEN 1 ELSE 0 END) AS with_result
                    FROM {table} b
                    JOIN {event_table} e ON e.event_id=b.event_id
                    LEFT JOIN {result_table} r ON r.event_id=b.event_id
                    WHERE b.status='OPEN' AND e.commence_time<=?""",
                (cutoff.isoformat(),),
            ) or {}
            item["stale_open"] = int(agg.get("n") or 0)
            item["missing_result"] = int(agg.get("missing_result") or 0)
            item["with_stored_result"] = int(agg.get("with_result") or 0)
            item["oldest_commence_time"] = agg.get("oldest")
            if item["stale_open"]:
                item["status"] = "LAGGING"
                examples = db.fetchall(
                    f"""SELECT b.*,e.commence_time AS event_commence_time,
                               CASE WHEN r.event_id IS NULL THEN 0 ELSE 1 END AS has_result
                        FROM {table} b
                        JOIN {event_table} e ON e.event_id=b.event_id
                        LEFT JOIN {result_table} r ON r.event_id=b.event_id
                        WHERE b.status='OPEN' AND e.commence_time<=?
                        ORDER BY e.commence_time,b.id LIMIT 20""",
                    (cutoff.isoformat(),),
                )
                for raw in examples:
                    row = dict(raw)
                    kick = _parse(row.get("event_commence_time"))
                    item["examples"].append({
                        "bet_id": row.get("id"),
                        "event_id": row.get("event_id"),
                        "fixture": _event_label(db, event_table, row.get("event_id")),
                        "pick": _row_pick_label(row),
                        "commence_time": row.get("event_commence_time"),
                        "age_hours": round((now - kick).total_seconds() / 3600.0, 2) if kick else None,
                        "has_stored_result": bool(row.get("has_result")),
                    })
        except Exception as exc:
            item["status"] = "ERROR"
            item["error"] = f"{type(exc).__name__}: {exc}"
            errors += 1
        total += item["stale_open"]
        total_missing_result += item["missing_result"]
        total_with_result += item["with_stored_result"]
        lanes.append(item)
    return {
        "version": VERSION,
        "status": "ERROR" if errors else "LAGGING" if total else "HEALTHY",
        "stale_after_hours": STALE_HOURS,
        "checked_at": now.isoformat(),
        "total_stale_open": total,
        "total_missing_result": total_missing_result,
        "total_with_stored_result": total_with_result,
        "lane_errors": errors,
        "lanes": lanes,
    }


def _health_page(web_module) -> str:
    health = settlement_health(web_module.db)
    status = str(health.get("status") or "UNKNOWN")
    css = "ok" if status == "HEALTHY" else "warn" if status == "LAGGING" else "bad"
    lane_rows = []
    detail_rows = []
    for lane in health.get("lanes", []):
        lane_status = str(lane.get("status") or "UNKNOWN")
        lane_css = "ok" if lane_status == "HEALTHY" else "warn" if lane_status == "LAGGING" else "bad"
        lane_rows.append(
            f"<tr><td><strong>{escape(str(lane.get('lane') or ''))}</strong><div class='muted'>{escape(str(lane.get('table') or ''))}</div></td>"
            f"<td class='{lane_css}'><strong>{escape(lane_status)}</strong></td>"
            f"<td>{int(lane.get('stale_open') or 0)}</td><td>{int(lane.get('missing_result') or 0)}</td>"
            f"<td>{int(lane.get('with_stored_result') or 0)}</td><td>{escape(str(lane.get('oldest_commence_time') or '—'))}</td></tr>"
        )
        for ex in lane.get("examples", []):
            age = "—" if ex.get("age_hours") is None else f"{float(ex.get('age_hours')):.1f}h"
            source = "RESULT STORED" if ex.get("has_stored_result") else "RESULT MISSING"
            detail_rows.append(
                f"<tr><td>{escape(str(lane.get('lane') or ''))}</td><td>{escape(str(ex.get('fixture') or ''))}</td>"
                f"<td>{escape(str(ex.get('pick') or ''))}</td><td>{escape(str(ex.get('commence_time') or ''))}</td>"
                f"<td>{escape(age)}</td><td>{escape(source)}</td></tr>"
            )
    details_html = "".join(detail_rows) or "<tr><td colspan='6'>No stale primary selections.</td></tr>"
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Settlement Integrity</title><style>{web_module.BASE_STYLE}</style></head><body>
    <a href='/'>← Betting Lab</a><h1>Settlement Integrity <span class='pill {css}'>{escape(status)}</span></h1>
    <div class='sub'>Primary-ledger stale-open audit. A row becomes stale after {STALE_HOURS:.0f} hours past scheduled start.</div>
    <div class='grid'><div class='card'><div class='label'>STALE OPEN</div><div class='value'>{int(health.get('total_stale_open') or 0)}</div></div><div class='card'><div class='label'>RESULT MISSING</div><div class='value'>{int(health.get('total_missing_result') or 0)}</div></div><div class='card'><div class='label'>RESULT STORED / NOT APPLIED</div><div class='value'>{int(health.get('total_with_stored_result') or 0)}</div></div><div class='card'><div class='label'>LANES CHECKED</div><div class='value'>{len(health.get('lanes', []))}</div></div></div>
    <div class='panel'><h2>Lane health</h2><div class='table-wrap'><table><thead><tr><th>Lane</th><th>Status</th><th>Stale</th><th>Missing result</th><th>Stored result</th><th>Oldest</th></tr></thead><tbody>{''.join(lane_rows)}</tbody></table></div></div>
    <div class='panel'><h2>Stale examples</h2><div class='table-wrap'><table><thead><tr><th>Lane</th><th>Fixture</th><th>Pick</th><th>Start</th><th>Age</th><th>Cause</th></tr></thead><tbody>{details_html}</tbody></table></div></div>
    <div class='muted'>Football missing-result repair only uses high-confidence stored API-Football/PXG1 finals. Ambiguous or unsupported historical results remain flagged rather than guessed.</div>
    </body></html>"""


def _install_routes(web_module) -> None:
    app = web_module.app
    if not any(getattr(r, "path", None) == "/api/settlement-health" for r in app.router.routes):
        @app.get("/api/settlement-health")
        def settlement_health_api():
            return settlement_health(web_module.db)

        @app.get("/settlement-health", response_class=HTMLResponse)
        def settlement_health_page():
            return HTMLResponse(_health_page(web_module))

    root = next((r for r in list(app.router.routes) if getattr(r, "path", None) == "/" and "GET" in (getattr(r, "methods", set()) or set())), None)
    if root is None or getattr(root.endpoint, "_settlement_health_v2_wrapped", False):
        return
    original = root.endpoint
    name = root.name
    include = getattr(root, "include_in_schema", True)
    app.router.routes.remove(root)

    async def wrapped_root():
        response = original()
        response = await response if inspect.isawaitable(response) else response
        if not isinstance(response, HTMLResponse):
            return response
        html = response.body.decode("utf-8")
        if "href='/settlement-health'" not in html:
            html = html.replace("<div class='nav'>", "<div class='nav'><a href='/settlement-health'>Settlement Health</a>", 1)
        try:
            health = settlement_health(web_module.db)
            status = str(health.get("status") or "UNKNOWN")
            css = "ok" if status == "HEALTHY" else "warn" if status == "LAGGING" else "bad"
            panel = (
                f"<div class='panel'><h2>Settlement Integrity <span class='pill {css}'>{escape(status)}</span></h2>"
                f"<div class='summary'>Primary rows OPEN &gt;{STALE_HOURS:.0f}h: <strong>{int(health.get('total_stale_open') or 0)}</strong> · "
                f"missing result <strong>{int(health.get('total_missing_result') or 0)}</strong> · stored result not applied <strong>{int(health.get('total_with_stored_result') or 0)}</strong>. "
                "<a href='/settlement-health'>Audit settlement →</a></div></div>"
            )
            html = html.replace("</body>", panel + "</body>", 1)
        except Exception:
            pass
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return HTMLResponse(html, status_code=response.status_code, headers=headers)

    wrapped_root._settlement_health_v2_wrapped = True
    app.add_api_route("/", wrapped_root, methods=["GET"], response_class=HTMLResponse, name=name, include_in_schema=include)


def install() -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    import web as web_module
    from results import ResultCollector

    # Future football result discovery: use the broad obligation predicate from v1.
    ResultCollector._due_events = v1._due_events_with_all_football_obligations
    web_module.result_collector._due_events = types.MethodType(
        v1._due_events_with_all_football_obligations, web_module.result_collector
    )
    _install_routes(web_module)

    worker = web_module.worker
    original_cycle = worker.one_cycle
    original_start = worker.start

    def run_guard(stage: str) -> Dict[str, Any]:
        try:
            repair = repair_all_football_missing_results(web_module.db)
        except Exception as exc:
            repair = {"error": f"{type(exc).__name__}: {exc}"}
        resettled = _reapply_stored_results(web_module)
        try:
            health = settlement_health(web_module.db)
        except Exception as exc:
            health = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
        payload = {"stage": stage, "repair": repair, "resettled": resettled, "health": health}
        ok = not repair.get("error") and health.get("status") != "ERROR"
        try:
            web_module.db.record_collector_run(
                "SETTLEMENT_INTEGRITY", bool(ok),
                detail=json.dumps(payload, separators=(",", ":"), default=str),
            )
        except Exception:
            pass
        if stage == "startup" or int(repair.get("repaired") or 0) or int(health.get("total_stale_open") or 0) or health.get("status") == "ERROR":
            print(f"SETTLEMENT_INTEGRITY_V2_{stage.upper()} {json.dumps(payload, separators=(',', ':'), default=str)}", flush=True)
        return payload

    def cycle_with_integrity():
        run_guard("pre_cycle")
        result = original_cycle()
        run_guard("post_cycle")
        return result

    def start_with_integrity():
        run_guard("startup")
        return original_start()

    worker.one_cycle = cycle_with_integrity
    worker.start = start_with_integrity
    worker._settlement_integrity_v2_installed = True
    web_module.VERSION = "0.19.52"
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "stale_hours": STALE_HOURS,
        "lanes": [x[0] for x in LANES],
        "dashboard_version": web_module.VERSION,
    }
