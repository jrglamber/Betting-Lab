from __future__ import annotations

"""Presentation-only Multi-Sport odds-band breakdown by sport.

The legacy Multi-Sport page already shows an aggregate odds-band table. This
patch adds the more useful cross-section: each sport family gets its own odds
bands, using the same granular bands already surfaced on the main dashboard.
It does not change collection, selection, thresholds, settlement or betting
authority.
"""

from collections import defaultdict
from html import escape
import inspect
from statistics import mean

from fastapi.responses import HTMLResponse

import web as web_module
from execution_shadow import is_headline_clv_quality


_INSTALLED = False

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

_SPORT_LABELS = {
    "BASEBALL": "Baseball",
    "AMERICAN_FOOTBALL": "American football",
    "BASKETBALL": "Basketball",
    "AUSSIE_RULES": "Aussie rules",
    "RUGBY_LEAGUE": "Rugby league",
    "ICE_HOCKEY": "Ice hockey",
    "CRICKET": "Cricket",
    "OTHER": "Other",
}


def _fmt(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return escape(str(value))


def _tone_style(value) -> str:
    """Use restrained traffic-light colouring for signed performance metrics."""
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


def _price_band(row) -> str:
    try:
        odds = float(row.get("offered_odds") or 0.0)
    except Exception:
        return "Unknown"
    if odds <= 1.0:
        return "Unknown"
    if odds < 1.5:
        return "<1.50"
    if odds < 2.0:
        return "1.50–1.99"
    if odds < 3.0:
        return "2.00–2.99"
    if odds < 4.0:
        return "3.00–3.99"
    if odds < 5.0:
        return "4.00–4.99"
    if odds < 7.5:
        return "5.00–7.49"
    if odds < 10.0:
        return "7.50–9.99"
    if odds < 15.0:
        return "10.00–14.99"
    return "15+"


def _render_panel() -> str:
    db = web_module.db
    bets = db.fetchall(
        """
        SELECT b.*, e.sport_family
        FROM multisport_execution_bets b
        JOIN multisport_events e ON e.event_id=b.event_id
        ORDER BY b.id
        """
    )
    configured = db.fetchall(
        """
        SELECT DISTINCT sport_family
        FROM multisport_league_state
        WHERE targeted=1
        ORDER BY sport_family
        """
    )

    families = {str(r.get("sport_family") or "OTHER") for r in configured}
    families.update(str(r.get("sport_family") or "OTHER") for r in bets)

    grouped = defaultdict(list)
    for row in bets:
        grouped[(str(row.get("sport_family") or "OTHER"), _price_band(row))].append(row)

    sections = []
    for family in sorted(families, key=lambda x: _SPORT_LABELS.get(x, x.replace("_", " ").title())):
        label = _SPORT_LABELS.get(family, family.replace("_", " ").title())
        band_names = sorted(
            {band for fam, band in grouped if fam == family},
            key=lambda b: _BAND_ORDER.get(b, 99),
        )
        if not band_names:
            sections.append(
                f"<h3 style='margin-top:20px'>{escape(label)}</h3>"
                "<div class='muted'>No executable shadows yet.</div>"
            )
            continue

        rows_html = []
        for band in band_names:
            items = grouped[(family, band)]
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
                if r.get("clv_pct") is not None and is_headline_clv_quality(r.get("clv_quality"))
            ]
            avg_clv = mean(clvs) if clvs else None
            rows_html.append(
                f"<tr><td><strong>{escape(band)}</strong></td>"
                f"<td>{len(items)}</td><td>{len(settled)}</td>"
                f"<td{_tone_style(net)}>{_fmt(net)}u</td>"
                f"<td{_tone_style(roi)}>{_fmt(roi)}%</td>"
                f"<td{_tone_style(avg_clv)}>{_fmt(avg_clv)}%</td>"
                f"<td>{len(clvs)}</td></tr>"
            )

        sections.append(
            f"<h3 style='margin-top:20px'>{escape(label)}</h3>"
            "<div class='table-wrap'><table><thead><tr>"
            "<th>Odds</th><th>Bets</th><th>Settled</th><th>Net P&L</th>"
            "<th>Net ROI</th><th>A/B CLV</th><th>CLV n</th>"
            f"</tr></thead><tbody>{''.join(rows_html)}</tbody></table></div>"
        )

    body = "".join(sections) or "<div class='muted'>No configured Multi-Sport families yet.</div>"
    return (
        "<div class='panel'><h2>Odds bands by sport</h2>"
        "<div class='muted'>Each sport is split independently so price pockets are not hidden inside the aggregate Multi-Sport result. "
        "Bands: &lt;1.50, 1.50–1.99, 2.00–2.99, 3.00–3.99, 4.00–4.99, 5.00–7.49, 7.50–9.99, 10.00–14.99 and 15+. Descriptive research only.</div>"
        f"{body}</div>"
    )


def install() -> dict:
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True}

    app = web_module.app
    route = next(
        (
            item for item in list(app.router.routes)
            if getattr(item, "path", None) == "/multisport"
            and "GET" in (getattr(item, "methods", set()) or set())
        ),
        None,
    )
    if route is None:
        return {"installed": False, "reason": "multisport_route_not_found"}

    original_endpoint = route.endpoint
    route_name = route.name
    include_in_schema = getattr(route, "include_in_schema", True)
    app.router.routes.remove(route)

    async def multisport_page_with_sport_odds_bands():
        response = original_endpoint()
        if inspect.isawaitable(response):
            response = await response
        if not isinstance(response, HTMLResponse):
            return response

        html = response.body.decode("utf-8")
        panel = _render_panel()
        marker = "<div class='section-grid'><div class='panel'><h2>By odds band</h2>"
        if marker in html:
            html = html.replace(marker, panel + marker, 1)
        else:
            fallback = "<div class='panel'><h2>EuroLeague CLV audit</h2>"
            if fallback in html:
                html = html.replace(fallback, panel + fallback, 1)
            else:
                html = html.replace("</body>", panel + "</body>", 1)

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
        "/multisport",
        multisport_page_with_sport_odds_bands,
        methods=["GET"],
        response_class=HTMLResponse,
        name=route_name,
        include_in_schema=include_in_schema,
    )
    _INSTALLED = True
    return {"installed": True, "already_installed": False}
