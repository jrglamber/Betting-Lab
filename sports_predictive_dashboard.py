from __future__ import annotations

"""Dashboard presentation for API-Sports NFL/EuroLeague predictive challengers.

This module is presentation-only. It reads the shared research tables created by
``api_sports_predictive.py`` and exposes a dedicated page/API plus a compact
section injected into the existing Betting Lab home dashboard. It has no model,
selection, execution, staking, or provider-call authority.
"""

from datetime import datetime, timezone
from html import escape
import json
from typing import Any, Dict, List, Mapping

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response


MODELS = (
    ("NFL-PRED1", "NFL"),
    ("EUROLEAGUE-PRED1", "EuroLeague"),
)


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return escape(str(value))


def _pct(value: Any) -> str:
    return "—" if value is None else f"{_fmt(value)}%"


def _tone(value: Any) -> str:
    if value is None:
        return ""
    try:
        return "ok" if float(value) >= 0 else "bad"
    except Exception:
        return ""


def _maturity(settled: int) -> str:
    n = int(settled or 0)
    if n >= 150:
        return "MATURE"
    if n >= 50:
        return "DEVELOPING"
    return "EARLY"


def _safe_json(value: Any) -> Dict[str, Any]:
    try:
        obj = json.loads(str(value or "{}"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _scoreboard(db, model_name: str) -> Dict[str, Any]:
    try:
        f = db.fetchone(
            """
            SELECT COUNT(*) AS forecasts,
                   SUM(CASE WHEN status='SETTLED' THEN 1 ELSE 0 END) AS settled_forecasts,
                   AVG(CASE WHEN status='SETTLED' THEN brier_score END) AS avg_brier,
                   MAX(generated_at) AS latest_forecast_at
            FROM api_sports_pred_forecasts
            WHERE model_name=?
            """,
            (model_name,),
        ) or {}
        b = db.fetchone(
            """
            SELECT COUNT(*) AS bets,
                   SUM(CASE WHEN status='SETTLED' THEN 1 ELSE 0 END) AS settled_bets,
                   SUM(CASE WHEN status='SETTLED' AND result='WIN' THEN 1 ELSE 0 END) AS wins,
                   COALESCE(SUM(CASE WHEN status='SETTLED' THEN pnl_units ELSE 0 END),0) AS pnl,
                   AVG(edge_pct) AS avg_edge_pct,
                   MAX(created_at) AS latest_bet_at
            FROM api_sports_pred_shadow_bets
            WHERE model_name=?
            """,
            (model_name,),
        ) or {}
        latest_run = db.fetchone(
            """
            SELECT started_at,finished_at,ok,detail
            FROM collector_runs
            WHERE run_type=?
            ORDER BY id DESC LIMIT 1
            """,
            (f"{model_name}_MAINT",),
        ) or {}
    except Exception as exc:
        return {
            "model": model_name,
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
            "forecasts": 0,
            "settled_forecasts": 0,
            "bets": 0,
            "settled_bets": 0,
            "wins": 0,
            "pnl_units": 0.0,
            "roi_pct": None,
            "hit_rate_pct": None,
            "avg_brier": None,
            "avg_edge_pct": None,
            "maturity": "EARLY",
            "health": "WAITING",
        }

    settled = int(b.get("settled_bets") or 0)
    wins = int(b.get("wins") or 0)
    pnl = float(b.get("pnl") or 0.0)
    detail = _safe_json(latest_run.get("detail"))
    health = "HEALTHY" if latest_run and bool(latest_run.get("ok")) else "WAITING" if not latest_run else "ERROR"
    return {
        "model": model_name,
        "available": True,
        "forecasts": int(f.get("forecasts") or 0),
        "settled_forecasts": int(f.get("settled_forecasts") or 0),
        "bets": int(b.get("bets") or 0),
        "settled_bets": settled,
        "wins": wins,
        "hit_rate_pct": (wins / settled * 100.0) if settled else None,
        "pnl_units": pnl,
        "roi_pct": (pnl / settled * 100.0) if settled else None,
        "avg_brier": f.get("avg_brier"),
        "avg_edge_pct": b.get("avg_edge_pct"),
        "latest_forecast_at": f.get("latest_forecast_at"),
        "latest_bet_at": b.get("latest_bet_at"),
        "last_run_at": latest_run.get("started_at"),
        "last_run_ok": latest_run.get("ok"),
        "last_run_detail": detail,
        "api_calls_last_run": detail.get("api_calls"),
        "api_remaining_last_run": detail.get("remaining"),
        "season": detail.get("season"),
        "league": detail.get("league"),
        "games_synced_last_run": detail.get("games_synced"),
        "maturity": _maturity(settled),
        "health": health,
    }


def _latest_forecasts(db, model_name: str, limit: int = 20) -> List[Dict[str, Any]]:
    try:
        return db.fetchall(
            """
            SELECT id,model_name,generated_at,commence_time,home_team,away_team,
                   home_probability,away_probability,home_fair_odds,away_fair_odds,
                   expected_home_points,expected_away_points,expected_margin,expected_total,
                   home_sample,away_sample,status,home_score,away_score,winner,brier_score,
                   odds_event_id
            FROM api_sports_pred_forecasts
            WHERE model_name=?
            ORDER BY id DESC LIMIT ?
            """,
            (model_name, int(limit)),
        )
    except Exception:
        return []


def _latest_bets(db, model_name: str, limit: int = 30) -> List[Dict[str, Any]]:
    try:
        return db.fetchall(
            """
            SELECT b.*,f.commence_time,f.home_team,f.away_team
            FROM api_sports_pred_shadow_bets b
            JOIN api_sports_pred_forecasts f ON f.id=b.forecast_id
            WHERE b.model_name=?
            ORDER BY b.id DESC LIMIT ?
            """,
            (model_name, int(limit)),
        )
    except Exception:
        return []


def _home_panel(db) -> str:
    rows = []
    for model, label in MODELS:
        s = _scoreboard(db, model)
        health_css = "ok" if s.get("health") == "HEALTHY" else "bad" if s.get("health") == "ERROR" else "warn"
        rows.append(
            f"<tr><td><a href='/sport-predictive'><strong>{escape(label)} PRED1</strong></a></td>"
            f"<td class='{health_css}'>{escape(str(s.get('health','—')))}</td>"
            f"<td>{s.get('forecasts',0)}</td><td>{s.get('bets',0)}</td><td>{s.get('settled_bets',0)}</td>"
            f"<td class='{_tone(s.get('pnl_units'))}'>{_fmt(s.get('pnl_units'))}u</td>"
            f"<td class='{_tone(s.get('roi_pct'))}'>{_pct(s.get('roi_pct'))}</td>"
            f"<td>{_pct(s.get('hit_rate_pct'))}</td><td>{_fmt(s.get('avg_brier'),4)}</td>"
            f"<td>{_pct(s.get('avg_edge_pct'))}</td><td>{escape(str(s.get('maturity','EARLY')))}</td></tr>"
        )
    body = "".join(rows)
    return (
        "<div class='panel priority'><h2>Sport Predictive Models "
        "<span class='pill'>API-SPORTS · SHADOW</span></h2>"
        "<div class='muted'>Independent underlying-performance challengers for NFL and EuroLeague. "
        "These are separate from the market-consensus Multi-Sport lanes and have no live betting authority.</div>"
        "<div class='table-wrap'><table><thead><tr>"
        "<th>Model</th><th>Health</th><th>Forecasts</th><th>Bets</th><th>Settled</th>"
        "<th>P&L</th><th>ROI</th><th>Hit rate</th><th>Brier</th><th>Avg model edge</th><th>Maturity</th>"
        f"</tr></thead><tbody>{body}</tbody></table></div>"
        "<div style='margin-top:12px'><a href='/sport-predictive'>Open Sport Predictive Models →</a></div></div>"
    )


def install(app: FastAPI, db, base_style: str) -> None:
    @app.get('/api/sport-predictive/status')
    def sport_predictive_status_api():
        return {model: _scoreboard(db, model) for model, _ in MODELS}

    @app.get('/api/sport-predictive/forecasts')
    def sport_predictive_forecasts_api(model: str = Query("NFL-PRED1"), limit: int = Query(100, ge=1, le=1000)):
        if model not in {x[0] for x in MODELS}:
            return JSONResponse({"error": "unknown model"}, status_code=400)
        return _latest_forecasts(db, model, limit)

    @app.get('/api/sport-predictive/bets')
    def sport_predictive_bets_api(model: str = Query("NFL-PRED1"), limit: int = Query(100, ge=1, le=1000)):
        if model not in {x[0] for x in MODELS}:
            return JSONResponse({"error": "unknown model"}, status_code=400)
        return _latest_bets(db, model, limit)

    @app.get('/sport-predictive', response_class=HTMLResponse)
    def sport_predictive_page():
        score_rows = []
        forecast_sections = []
        bet_sections = []
        for model, label in MODELS:
            s = _scoreboard(db, model)
            health_css = "ok" if s.get("health") == "HEALTHY" else "bad" if s.get("health") == "ERROR" else "warn"
            score_rows.append(
                f"<tr><td><strong>{escape(label)} PRED1</strong></td><td class='{health_css}'>{escape(str(s.get('health','—')))}</td>"
                f"<td>{s.get('forecasts',0)}</td><td>{s.get('settled_forecasts',0)}</td><td>{s.get('bets',0)}</td><td>{s.get('settled_bets',0)}</td>"
                f"<td class='{_tone(s.get('pnl_units'))}'>{_fmt(s.get('pnl_units'))}u</td>"
                f"<td class='{_tone(s.get('roi_pct'))}'>{_pct(s.get('roi_pct'))}</td><td>{_pct(s.get('hit_rate_pct'))}</td>"
                f"<td>{_fmt(s.get('avg_brier'),4)}</td><td>{_pct(s.get('avg_edge_pct'))}</td>"
                f"<td>{escape(str(s.get('maturity','EARLY')))}</td></tr>"
            )

            forecasts = _latest_forecasts(db, model, 20)
            frows = "".join(
                f"<tr><td>{escape(str(x.get('commence_time') or '—'))}</td>"
                f"<td>{escape(str(x.get('home_team') or ''))} v {escape(str(x.get('away_team') or ''))}</td>"
                f"<td>{_pct((float(x.get('home_probability'))*100.0) if x.get('home_probability') is not None else None)}</td>"
                f"<td>{_pct((float(x.get('away_probability'))*100.0) if x.get('away_probability') is not None else None)}</td>"
                f"<td>{_fmt(x.get('expected_home_points'))}–{_fmt(x.get('expected_away_points'))}</td>"
                f"<td>{x.get('home_sample',0)}/{x.get('away_sample',0)}</td><td>{escape(str(x.get('status') or '—'))}</td>"
                f"<td>{escape(str(x.get('winner') or 'PENDING'))}</td><td>{_fmt(x.get('brier_score'),4)}</td></tr>"
                for x in forecasts
            ) or "<tr><td colspan='9'>No forecasts yet — waiting for the first completed collection cycle / sufficient team history.</td></tr>"
            forecast_sections.append(
                f"<div class='panel'><h2>{escape(label)} · latest forecasts</h2><div class='table-wrap'><table><thead><tr>"
                "<th>Kickoff</th><th>Event</th><th>Home p</th><th>Away p</th><th>Expected score</th><th>Samples H/A</th><th>Status</th><th>Winner</th><th>Brier</th>"
                f"</tr></thead><tbody>{frows}</tbody></table></div></div>"
            )

            bets = _latest_bets(db, model, 30)
            brows = "".join(
                f"<tr><td>{escape(str(x.get('commence_time') or '—'))}</td><td>{escape(str(x.get('home_team') or ''))} v {escape(str(x.get('away_team') or ''))}</td>"
                f"<td>{escape(str(x.get('selection') or ''))}</td><td>{escape(str(x.get('bookmaker_key') or ''))}</td>"
                f"<td>{_fmt(x.get('offered_odds'))}</td><td>{_fmt(x.get('fair_odds'))}</td><td>{_pct(x.get('edge_pct'))}</td>"
                f"<td>{escape(str(x.get('result') or x.get('status') or 'PENDING'))}</td>"
                f"<td class='{_tone(x.get('pnl_units'))}'>{_fmt(x.get('pnl_units'))}u</td></tr>"
                for x in bets
            ) or "<tr><td colspan='9'>No ≥3% model-edge shadow bets yet.</td></tr>"
            bet_sections.append(
                f"<div class='panel'><h2>{escape(label)} · latest shadow bets</h2><div class='table-wrap'><table><thead><tr>"
                "<th>Kickoff</th><th>Event</th><th>Selection</th><th>Venue</th><th>Entry</th><th>Model fair</th><th>Edge</th><th>Result</th><th>P&L</th>"
                f"</tr></thead><tbody>{brows}</tbody></table></div></div>"
            )

        return HTMLResponse(
            f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
            <title>Sport Predictive Models</title><style>{base_style}
            body{{max-width:1400px;margin:auto}}.table-wrap{{overflow-x:auto}}
            @media(max-width:600px){{body{{padding:14px}}table{{min-width:760px}}}}
            </style></head><body>
            <a href='/'>← Betting Lab</a><h1>Sport Predictive Models <span class='pill'>NFL-PRED1 · EUROLEAGUE-PRED1</span></h1>
            <div class='sub'>Independent API-Sports performance models. Current V1 is deliberately simple: Elo + recency-weighted scoring performance, frozen pre-match before checking the stored bookmaker price. Research/shadow only.</div>
            <div class='panel'><h2>Scoreboard</h2><div class='table-wrap'><table><thead><tr>
            <th>Model</th><th>Health</th><th>Forecasts</th><th>Settled forecasts</th><th>Bets</th><th>Settled bets</th><th>P&L</th><th>ROI</th><th>Hit rate</th><th>Brier</th><th>Avg edge</th><th>Maturity</th>
            </tr></thead><tbody>{''.join(score_rows)}</tbody></table></div></div>
            {''.join(forecast_sections)}{''.join(bet_sections)}
            </body></html>"""
        )

    @app.middleware("http")
    async def inject_sport_predictive_home_panel(request, call_next):
        response = await call_next(request)
        if request.url.path != "/" or response.status_code != 200:
            return response
        content_type = str(response.headers.get("content-type") or "")
        if "text/html" not in content_type:
            return response
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        html = body.decode("utf-8", errors="replace")
        panel = _home_panel(db)
        marker = "<div class='panel'><h2>Other research lanes</h2>"
        if marker in html:
            html = html.replace(marker, panel + "\n    " + marker, 1)
        else:
            html = html.replace("</body>", panel + "</body>", 1)
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(content=html, status_code=response.status_code, headers=headers, media_type="text/html")
