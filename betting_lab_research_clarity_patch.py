from __future__ import annotations

"""Presentation-only clarity fixes for current Betting Lab research pages.

1) Split the American-football odds-band evidence into NFL vs NCAAF (and any
   other configured American-football sport keys) before the aggregate family
   table, so price pockets are not accidentally attributed to NFL as a whole.
2) Make API-Sports PRED1 historical-only status explicit when the provider's
   latest accessible season is older than the current year.

No collection, model, threshold, staking, Yankee-source or execution rule is
changed here.
"""

from collections import defaultdict
from datetime import datetime, timezone
from html import escape
import inspect
import re
from statistics import mean

from fastapi.responses import HTMLResponse

import multisport_odds_band_patch as odds_patch
import sports_predictive_dashboard as sports_dash
import web as web_module
from execution_shadow import is_headline_clv_quality

_INSTALLED = False
_ORIGINAL_ODDS_PANEL = None
_ORIGINAL_SPORTS_SCOREBOARD = None

_BAND_ORDER = {
    "<1.50": 0,
    "1.50–1.99": 1,
    "2.00–2.99": 2,
    "3.00–3.99": 3,
    "4.00–4.99": 4,
    "5.00–7.49": 5,
    "7.50–9.99": 6,
    "10.00–14.99": 7,
    "15+": 8,
    "Unknown": 9,
}


def _tone(value) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except Exception:
        return ""
    if number > 0:
        return " style='color:#22c55e;font-weight:700'"
    if number < 0:
        return " style='color:#ef4444;font-weight:700'"
    return ""


def _fmt(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return escape(str(value))


def _league_label(sport_key: str, league_title: str) -> str:
    key = str(sport_key or "").lower()
    if key == "americanfootball_nfl" or key.endswith("_nfl"):
        return "NFL"
    if "ncaaf" in key or "ncaa" in key:
        return "NCAAF"
    return str(league_title or sport_key or "Other American football")


def _american_football_split() -> str:
    db = web_module.db
    try:
        bets = db.fetchall(
            """
            SELECT b.*, e.sport_key, e.league_title
            FROM multisport_execution_bets b
            JOIN multisport_events e ON e.event_id=b.event_id
            WHERE e.sport_family='AMERICAN_FOOTBALL'
            ORDER BY b.id
            """
        )
    except Exception as exc:
        return f"<div class='muted'>NFL/NCAAF split unavailable: {escape(type(exc).__name__)}</div>"

    by_lane = defaultdict(list)
    for row in bets:
        label = _league_label(row.get("sport_key"), row.get("league_title"))
        by_lane[label].append(row)
    if not by_lane:
        return ""

    sections = []
    preferred = {"NFL": 0, "NCAAF": 1}
    for label in sorted(by_lane, key=lambda x: (preferred.get(x, 99), x)):
        grouped = defaultdict(list)
        for row in by_lane[label]:
            grouped[odds_patch._price_band(row)].append(row)
        rows_html = []
        for band in sorted(grouped, key=lambda b: _BAND_ORDER.get(b, 99)):
            items = grouped[band]
            settled = [
                r for r in items
                if r.get("pnl_units") is not None
                and r.get("settlement_quality") != "UNVERIFIED_VENUE_RULE"
            ]
            net = sum(
                float(r.get("net_pnl_units") if r.get("net_pnl_units") is not None else r.get("pnl_units") or 0.0)
                for r in settled
            )
            roi = (net / len(settled) * 100.0) if settled else None
            clvs = [
                float(r.get("clv_pct"))
                for r in items
                if r.get("clv_pct") is not None
                and is_headline_clv_quality(r.get("clv_quality"))
            ]
            avg_clv = mean(clvs) if clvs else None
            rows_html.append(
                f"<tr><td><strong>{escape(band)}</strong></td>"
                f"<td>{len(items)}</td><td>{len(settled)}</td>"
                f"<td{_tone(net)}>{_fmt(net)}u</td>"
                f"<td{_tone(roi)}>{_fmt(roi)}%</td>"
                f"<td{_tone(avg_clv)}>{_fmt(avg_clv)}%</td>"
                f"<td>{len(clvs)}</td></tr>"
            )
        sections.append(
            f"<h4 style='margin:18px 0 8px'>{escape(label)}</h4>"
            "<div class='table-wrap'><table><thead><tr>"
            "<th>Odds</th><th>Bets</th><th>Settled</th><th>Net P&L</th>"
            "<th>Net ROI</th><th>A/B CLV</th><th>CLV n</th>"
            f"</tr></thead><tbody>{''.join(rows_html)}</tbody></table></div>"
        )

    return (
        "<div style='margin:18px 0 24px;padding:14px;border:1px solid #334155;border-radius:12px'>"
        "<h3 style='margin-top:0'>American football · league split</h3>"
        "<div class='muted'>NFL and NCAAF are separated here before the combined American-football table below. "
        "Use this split before changing any NFL odds-band rule.</div>"
        f"{''.join(sections)}</div>"
    )


def _odds_panel() -> str:
    html = _ORIGINAL_ODDS_PANEL()
    marker = "<h3 style='margin-top:20px'>American football</h3>"
    split = _american_football_split()
    if split and marker in html:
        return html.replace(marker, split + marker, 1)
    return html


def _season_year(value) -> int | None:
    match = re.search(r"(20\d{2})", str(value or ""))
    return int(match.group(1)) if match else None


def _lane_for_model(model_name: str) -> str:
    return "NFL" if str(model_name).upper().startswith("NFL") else "EUROLEAGUE"


def _sports_scoreboard(db, model_name: str):
    out = dict(_ORIGINAL_SPORTS_SCOREBOARD(db, model_name))
    lane = _lane_for_model(model_name)
    try:
        state = db.fetchone(
            "SELECT season,league_name FROM api_sports_pred_leagues WHERE lane_key=?",
            (lane,),
        ) or {}
        games = db.fetchone(
            "SELECT COUNT(*) AS n FROM api_sports_pred_games WHERE lane_key=?",
            (lane,),
        ) or {}
        season = state.get("season") or out.get("season")
        year = _season_year(season)
        out["provider_season"] = season
        out["provider_games_synced"] = int(games.get("n") or 0)
        if year is not None and year < datetime.now(timezone.utc).year:
            out["health"] = "HISTORICAL_ONLY"
            out["provider_mode"] = "HISTORICAL_ONLY"
            out["historical_reason"] = (
                f"Latest provider-accessible season is {season}; no current-season forward forecasts are expected."
            )
    except Exception:
        pass
    return out


def _provider_notice() -> str:
    cards = []
    for model, label in sports_dash.MODELS:
        s = sports_dash._scoreboard(web_module.db, model)
        mode = str(s.get("provider_mode") or s.get("health") or "")
        if mode != "HISTORICAL_ONLY":
            continue
        cards.append(
            f"<div class='card'><div class='label'>{escape(label)} PROVIDER DATA</div>"
            f"<div class='value' style='font-size:24px'>{escape(str(s.get('provider_season') or '—'))}</div>"
            f"<div class='muted'>{int(s.get('provider_games_synced') or 0)} games synced · historical only</div></div>"
        )
    if not cards:
        return ""
    return (
        "<div class='panel' style='border-color:#a16207'><h2>Provider constraint · historical only</h2>"
        "<div class='muted'>This section is not a dead collector. The current API-Sports access does not expose the current 2026 NFL/EuroLeague seasons; "
        "the latest accessible season is historical. Therefore <strong>0 live forecasts is expected</strong> until current-season provider access changes. "
        "The stored historical games can still be used for a separate walk-forward/backtest study.</div>"
        f"<div class='grid compact' style='margin-top:12px'>{''.join(cards)}</div></div>"
    )


def _wrap_sport_predictive_route() -> bool:
    app = web_module.app
    route = next(
        (
            r for r in list(app.router.routes)
            if getattr(r, "path", None) == "/sport-predictive"
            and "GET" in (getattr(r, "methods", set()) or set())
        ),
        None,
    )
    if route is None:
        return False
    original = route.endpoint
    name = route.name
    include = getattr(route, "include_in_schema", True)
    app.router.routes.remove(route)

    async def clarified_page():
        response = original()
        if inspect.isawaitable(response):
            response = await response
        if not isinstance(response, HTMLResponse):
            return response
        html = response.body.decode("utf-8")
        notice = _provider_notice()
        if notice:
            first_panel = html.find("<div class='panel'>")
            if first_panel >= 0:
                html = html[:first_panel] + notice + html[first_panel:]
            else:
                html = html.replace("</body>", notice + "</body>", 1)
        html = html.replace(
            "No forecasts yet — waiting for the first completed collection cycle / sufficient team history.",
            "No live forecasts expected while this provider lane is historical-only.",
        )
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return HTMLResponse(content=html, status_code=response.status_code, headers=headers)

    app.add_api_route(
        "/sport-predictive",
        clarified_page,
        methods=["GET"],
        response_class=HTMLResponse,
        name=name,
        include_in_schema=include,
    )
    return True


def install() -> dict:
    global _INSTALLED, _ORIGINAL_ODDS_PANEL, _ORIGINAL_SPORTS_SCOREBOARD
    if _INSTALLED:
        return {"installed": True, "already_installed": True}

    _ORIGINAL_ODDS_PANEL = odds_patch._render_panel
    odds_patch._render_panel = _odds_panel

    _ORIGINAL_SPORTS_SCOREBOARD = sports_dash._scoreboard
    sports_dash._scoreboard = _sports_scoreboard
    predictive_route = _wrap_sport_predictive_route()

    web_module.VERSION = "0.19.41"
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "american_football_league_split": True,
        "predictive_historical_clarity": predictive_route,
        "dashboard_version": web_module.VERSION,
    }
