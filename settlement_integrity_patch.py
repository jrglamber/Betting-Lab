from __future__ import annotations

"""Settlement integrity guard for Project Exit Plan — Betting Lab.

This patch fixes a blind spot in the shared football result collector and adds a
read-only stale-open audit across the primary research ledgers.  PRED4-only
fixtures were previously absent from ResultCollector._due_events(), so no
`event_results` row was requested and PRED4's otherwise-correct settlement code
had nothing to join to.

For old PRED4 stragglers that are outside The Odds API short scores lookback, the
repair path reuses the already-stored API-Football/PXG1 completed-match row.  A
repair is accepted only when sport, date/time and both team names match at high
confidence.  No market/outcome/model threshold is changed.
"""

from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from html import escape
import inspect
import json
import types
from typing import Any, Dict, Mapping, Optional

from fastapi.responses import HTMLResponse

from db import utc_now_iso
from predictive_football_pred4 import _normalize_text
from results import settle_event_signals

VERSION = "SETTLEMENT_INTEGRITY_V1"
STALE_HOURS = 6.0
PXG_MATCH_WINDOW_HOURS = 30.0
MIN_TEAM_MATCH = 0.90
MIN_PAIR_MATCH = 1.84
MIN_BEST_MARGIN = 0.05

_INSTALLED = False

# These are the primary selection ledgers whose OPEN state should not survive
# long after a scheduled event has completed.  Derived multiple/system cards are
# intentionally excluded: their health follows from their constituent legs and
# is audited on their own dashboards.
LANES = (
    ("CORE_EXECUTION", "execution_shadow_bets", "events"),
    ("PRED1_H2H", "football_predictive_bets", "events"),
    ("PRED1_MARKETS", "football_predictive_market_bets", "events"),
    ("PRED2_H2H", "football_predictive2_bets", "events"),
    ("PRED2_MARKETS", "football_predictive2_market_bets", "events"),
    ("PRED3_H2H", "football_predictive3_bets", "events"),
    ("PRED3_MARKETS", "football_predictive3_market_bets", "events"),
    ("PRED4_H2H", "football_predictive4_bets", "events"),
    ("PRED4_MARKETS", "football_predictive4_market_bets", "events"),
    ("TENNIS", "tennis_execution_bets", "tennis_events"),
    ("MULTISPORT_H2H", "multisport_execution_bets", "multisport_events"),
    ("MULTISPORT_LINES", "multisport_line_bets", "multisport_events"),
)


def _parse(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _team_score(left: Any, right: Any) -> float:
    a = _normalize_text(str(left or ""))
    b = _normalize_text(str(right or ""))
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # API-Football commonly includes/removes suffixes such as City/United after
    # the generic FC/CF tokens have already been stripped.  Containment plus the
    # opposite-team, sport and time checks is much safer than a single fuzzy name.
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 5 and shorter in longer:
        return 0.96
    return float(SequenceMatcher(None, a, b).ratio())


def _result_collector_due_events(self, now: datetime):
    """Result obligations based on unfinished work, including PRED4.

    This replaces the old hard-coded PRED1/PRED2/PRED3-only predicate.  It also
    protects against a future model being partly settled while an actual bet is
    still OPEN by checking the bet ledgers directly.
    """
    cutoff = now - timedelta(minutes=self.min_minutes_after_kickoff)
    return self.db.fetchall(
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
                OR EXISTS (SELECT 1 FROM football_predictive_predictions p WHERE p.event_id=e.event_id AND p.actual_outcome IS NULL)
                OR EXISTS (SELECT 1 FROM football_predictive_bets b WHERE b.event_id=e.event_id AND b.status='OPEN')
                OR EXISTS (SELECT 1 FROM football_predictive_market_bets b WHERE b.event_id=e.event_id AND b.status='OPEN')
                OR EXISTS (SELECT 1 FROM football_predictive2_predictions p WHERE p.event_id=e.event_id AND p.actual_outcome IS NULL)
                OR EXISTS (SELECT 1 FROM football_predictive2_bets b WHERE b.event_id=e.event_id AND b.status='OPEN')
                OR EXISTS (SELECT 1 FROM football_predictive2_market_bets b WHERE b.event_id=e.event_id AND b.status='OPEN')
                OR EXISTS (SELECT 1 FROM football_predictive3_predictions p WHERE p.event_id=e.event_id AND p.actual_outcome IS NULL)
                OR EXISTS (SELECT 1 FROM football_predictive3_bets b WHERE b.event_id=e.event_id AND b.status='OPEN')
                OR EXISTS (SELECT 1 FROM football_predictive3_market_bets b WHERE b.event_id=e.event_id AND b.status='OPEN')
                OR EXISTS (SELECT 1 FROM football_predictive4_predictions p WHERE p.event_id=e.event_id AND p.actual_outcome IS NULL)
                OR EXISTS (SELECT 1 FROM football_predictive4_bets b WHERE b.event_id=e.event_id AND b.status='OPEN')
                OR EXISTS (SELECT 1 FROM football_predictive4_market_bets b WHERE b.event_id=e.event_id AND b.status='OPEN')
              )
        ORDER BY e.commence_time ASC
        """,
        (cutoff.isoformat(),),
    )


def _pred4_missing_result_events(db, now: datetime):
    cutoff = now - timedelta(hours=STALE_HOURS)
    return db.fetchall(
        """
        SELECT DISTINCT e.*
        FROM events e
        LEFT JOIN event_results r ON r.event_id=e.event_id
        WHERE r.event_id IS NULL
          AND e.commence_time<=?
          AND (
            EXISTS (SELECT 1 FROM football_predictive4_bets b WHERE b.event_id=e.event_id AND b.status='OPEN')
            OR EXISTS (SELECT 1 FROM football_predictive4_market_bets b WHERE b.event_id=e.event_id AND b.status='OPEN')
          )
        ORDER BY e.commence_time ASC
        """,
        (cutoff.isoformat(),),
    )


def _pxg_match(db, event: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    kickoff = _parse(event.get("commence_time"))
    if kickoff is None:
        return None
    lo = (kickoff - timedelta(hours=PXG_MATCH_WINDOW_HOURS)).isoformat()
    hi = (kickoff + timedelta(hours=PXG_MATCH_WINDOW_HOURS)).isoformat()
    rows = db.fetchall(
        """
        SELECT * FROM football_pxg_current_matches
        WHERE sport_key=?
          AND home_goals IS NOT NULL AND away_goals IS NOT NULL
          AND played_at>=? AND played_at<=?
        ORDER BY played_at ASC,id ASC
        """,
        (event.get("sport_key"), lo, hi),
    )
    scored = []
    for row in rows:
        played = _parse(row.get("played_at"))
        if played is None:
            continue
        home = _team_score(event.get("home_team"), row.get("home_team"))
        away = _team_score(event.get("away_team"), row.get("away_team"))
        if home < MIN_TEAM_MATCH or away < MIN_TEAM_MATCH:
            continue
        pair = home + away
        if pair < MIN_PAIR_MATCH:
            continue
        delta = abs((played - kickoff).total_seconds()) / 3600.0
        composite = pair - min(delta, PXG_MATCH_WINDOW_HOURS) * 0.002
        scored.append((composite, pair, -delta, dict(row), home, away, delta))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    best = scored[0]
    if len(scored) > 1 and best[0] - scored[1][0] < MIN_BEST_MARGIN:
        return None
    out = dict(best[3])
    out["_home_match"] = best[4]
    out["_away_match"] = best[5]
    out["_time_delta_hours"] = best[6]
    return out


def repair_pred4_missing_results(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    due = _pred4_missing_result_events(db, now)
    repaired = []
    unresolved = []
    for event in due:
        match = _pxg_match(db, event)
        if match is None:
            unresolved.append({
                "event_id": event.get("event_id"),
                "home_team": event.get("home_team"),
                "away_team": event.get("away_team"),
                "commence_time": event.get("commence_time"),
            })
            continue
        hs = int(match["home_goals"])
        aws = int(match["away_goals"])
        provenance = {
            "repair": VERSION,
            "fixture_id": match.get("fixture_id"),
            "pxg_played_at": match.get("played_at"),
            "home_match": round(float(match.get("_home_match") or 0.0), 4),
            "away_match": round(float(match.get("_away_match") or 0.0), 4),
            "time_delta_hours": round(float(match.get("_time_delta_hours") or 0.0), 3),
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
        "unresolved_examples": unresolved[:20],
    }


def _settle_predictive_engines(web_module) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    engines = (
        ("PRED1", getattr(web_module, "predictive_football_engine", None)),
        ("PRED2", getattr(web_module, "predictive_football_pred2_engine", None)),
        ("PRED3", getattr(web_module, "predictive_football_pred3_engine", None)),
        ("PRED4", getattr(web_module, "predictive_football_pred4_engine", None)),
    )
    for label, engine in engines:
        if engine is None:
            continue
        lane: Dict[str, Any] = {}
        for method in ("settle", "settle_market_predictions"):
            fn = getattr(engine, method, None)
            if fn is None:
                continue
            try:
                lane[method] = fn()
            except Exception as exc:
                lane[f"{method}_error"] = f"{type(exc).__name__}: {exc}"
        out[label] = lane
    return out


def settlement_health(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=STALE_HOURS)
    lanes = []
    total = 0
    for lane, table, event_table in LANES:
        item: Dict[str, Any] = {
            "lane": lane,
            "table": table,
            "stale_open": 0,
            "oldest_commence_time": None,
            "status": "HEALTHY",
            "examples": [],
        }
        try:
            agg = db.fetchone(
                f"""SELECT COUNT(*) AS n,MIN(e.commence_time) AS oldest
                    FROM {table} b JOIN {event_table} e ON e.event_id=b.event_id
                    WHERE b.status='OPEN' AND e.commence_time<=?""",
                (cutoff.isoformat(),),
            ) or {}
            item["stale_open"] = int(agg.get("n") or 0)
            item["oldest_commence_time"] = agg.get("oldest")
            if item["stale_open"]:
                item["status"] = "LAGGING"
                rows = db.fetchall(
                    f"""SELECT b.id AS bet_id,b.event_id,b.market_key,b.selection,b.point,
                               e.commence_time,e.home_team,e.away_team
                        FROM {table} b JOIN {event_table} e ON e.event_id=b.event_id
                        WHERE b.status='OPEN' AND e.commence_time<=?
                        ORDER BY e.commence_time ASC,b.id ASC LIMIT 12""",
                    (cutoff.isoformat(),),
                )
                for row in rows:
                    kick = _parse(row.get("commence_time"))
                    age = ((now - kick).total_seconds() / 3600.0) if kick else None
                    item["examples"].append({
                        **dict(row),
                        "age_hours": round(age, 2) if age is not None else None,
                    })
        except Exception as exc:
            item["status"] = "ERROR"
            item["error"] = f"{type(exc).__name__}: {exc}"
        total += int(item.get("stale_open") or 0)
        lanes.append(item)
    errors = sum(1 for x in lanes if x.get("status") == "ERROR")
    status = "ERROR" if errors else "LAGGING" if total else "HEALTHY"
    return {
        "version": VERSION,
        "status": status,
        "stale_after_hours": STALE_HOURS,
        "checked_at": now.isoformat(),
        "total_stale_open": total,
        "lane_errors": errors,
        "lanes": lanes,
    }


def _health_page(web_module) -> str:
    health = settlement_health(web_module.db)
    status = str(health.get("status") or "UNKNOWN")
    css = "ok" if status == "HEALTHY" else "warn" if status == "LAGGING" else "bad"
    rows = []
    details = []
    for lane in health.get("lanes", []):
        lane_status = str(lane.get("status") or "UNKNOWN")
        lane_css = "ok" if lane_status == "HEALTHY" else "warn" if lane_status == "LAGGING" else "bad"
        rows.append(
            f"<tr><td><strong>{escape(str(lane.get('lane') or ''))}</strong><div class='muted'>{escape(str(lane.get('table') or ''))}</div></td>"
            f"<td class='{lane_css}'><strong>{escape(lane_status)}</strong></td>"
            f"<td>{int(lane.get('stale_open') or 0)}</td><td>{escape(str(lane.get('oldest_commence_time') or '—'))}</td></tr>"
        )
        for ex in lane.get("examples", []):
            market = str(ex.get("market_key") or "")
            selection = str(ex.get("selection") or "")
            point = "" if ex.get("point") is None else f" {ex.get('point')}"
            details.append(
                f"<tr><td>{escape(str(lane.get('lane') or ''))}</td>"
                f"<td>{escape(str(ex.get('home_team') or ''))} v {escape(str(ex.get('away_team') or ''))}</td>"
                f"<td>{escape(market)} · {escape(selection)}{escape(point)}</td>"
                f"<td>{escape(str(ex.get('commence_time') or ''))}</td>"
                f"<td>{'—' if ex.get('age_hours') is None else f'{float(ex['age_hours']):.1f}h'}</td></tr>"
            )
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Settlement Integrity</title><style>{web_module.BASE_STYLE}</style></head><body>
    <a href='/'>← Betting Lab</a><h1>Settlement Integrity <span class='pill {css}'>{escape(status)}</span></h1>
    <div class='sub'>Primary-ledger stale-open audit. A bet is stale when it is still OPEN more than {STALE_HOURS:.0f} hours after scheduled start.</div>
    <div class='grid'><div class='card'><div class='label'>STALE OPEN</div><div class='value'>{int(health.get('total_stale_open') or 0)}</div></div><div class='card'><div class='label'>LANES CHECKED</div><div class='value'>{len(health.get('lanes', []))}</div></div></div>
    <div class='panel'><h2>Lane health</h2><div class='table-wrap'><table><thead><tr><th>Lane</th><th>Status</th><th>Stale open</th><th>Oldest</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></div>
    <div class='panel'><h2>Stale examples</h2><div class='table-wrap'><table><thead><tr><th>Lane</th><th>Fixture</th><th>Pick</th><th>Kickoff</th><th>Age</th></tr></thead><tbody>{''.join(details) or "<tr><td colspan='5'>No stale primary selections.</td></tr>"}</tbody></table></div></div>
    <div class='muted'>PRED4 old-result repair accepts only high-confidence matches to already-stored API-Football/PXG1 final scores. Unresolved rows remain visible here rather than being guessed.</div>
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
    if root is None or getattr(root.endpoint, "_settlement_health_wrapped", False):
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
            link = "<a href='/settlement-health'>Settlement Health</a>"
            html = html.replace("<div class='nav'>", "<div class='nav'>" + link, 1)
        try:
            h = settlement_health(web_module.db)
            status = str(h.get("status") or "UNKNOWN")
            css = "ok" if status == "HEALTHY" else "warn" if status == "LAGGING" else "bad"
            panel = (
                f"<div class='panel'><h2>Settlement Integrity <span class='pill {css}'>{escape(status)}</span></h2>"
                f"<div class='summary'>Primary ledgers stale-open &gt;{STALE_HOURS:.0f}h: <strong>{int(h.get('total_stale_open') or 0)}</strong>. "
                "<a href='/settlement-health'>Audit settlement →</a></div></div>"
            )
            html = html.replace("</body>", panel + "</body>", 1)
        except Exception:
            pass
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return HTMLResponse(html, status_code=response.status_code, headers=headers)

    wrapped_root._settlement_health_wrapped = True
    app.add_api_route("/", wrapped_root, methods=["GET"], response_class=HTMLResponse, name=name, include_in_schema=include)


def install() -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    import web as web_module

    # Patch the live collector instance (and its class for any later instance).
    from results import ResultCollector
    ResultCollector._due_events = _result_collector_due_events
    web_module.result_collector._due_events = types.MethodType(_result_collector_due_events, web_module.result_collector)
    _install_routes(web_module)

    worker = web_module.worker
    original_cycle = worker.one_cycle
    original_start = worker.start

    def run_guard(stage: str) -> Dict[str, Any]:
        repair: Dict[str, Any]
        settled: Dict[str, Any] = {}
        try:
            repair = repair_pred4_missing_results(web_module.db)
            if int(repair.get("repaired") or 0):
                settled = _settle_predictive_engines(web_module)
        except Exception as exc:
            repair = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            health = settlement_health(web_module.db)
        except Exception as exc:
            health = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
        payload = {"stage": stage, "repair": repair, "settled": settled, "health": health}
        ok = str(health.get("status") or "ERROR") != "ERROR" and not repair.get("error")
        try:
            web_module.db.record_collector_run("SETTLEMENT_INTEGRITY", ok, detail=json.dumps(payload, separators=(",", ":"), default=str))
        except Exception:
            pass
        # Always print startup; subsequent cycles only when there is something to
        # inspect so normal Railway logs remain compact.
        if stage == "startup" or int(repair.get("repaired") or 0) or int(health.get("total_stale_open") or 0) or health.get("status") == "ERROR":
            print(f"SETTLEMENT_INTEGRITY_{stage.upper()} {json.dumps(payload, separators=(',', ':'), default=str)}", flush=True)
        return payload

    def cycle_with_integrity():
        # Repair old PRED4 misses before model maintenance so the same cycle can
        # consume the newly-stored result.  Then run the ordinary worker.
        run_guard("pre_cycle")
        result = original_cycle()
        # Re-audit after the normal result collector has had its chance to settle
        # recent fixtures (now including PRED4 obligations).
        run_guard("post_cycle")
        return result

    def start_with_integrity():
        # Lifespan has initialized the schema before worker.start() is called.
        run_guard("startup")
        return original_start()

    worker.one_cycle = cycle_with_integrity
    worker.start = start_with_integrity
    worker._settlement_integrity_installed = True

    web_module.VERSION = "0.19.51"
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "stale_hours": STALE_HOURS,
        "lanes": [x[0] for x in LANES],
        "dashboard_version": web_module.VERSION,
    }
