from __future__ import annotations

"""Presentation-only FHG1 section for the curated Betting Lab homepage."""

import inspect
from html import escape
from typing import Any, Dict, Mapping

from fastapi.responses import HTMLResponse

from first_half_goal_lab import scoreboard

VERSION = "FHG1_CURATED_SECTION_V2"


def _pct(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value):.{digits}f}%"
    except Exception:
        return "—"


def _num(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except Exception:
        return "0"


def _metric(label: str, value: str, note: str = "") -> str:
    return (
        "<div class='card metric-card'>"
        f"<div class='label'>{escape(label)}</div>"
        f"<div class='value'>{escape(value)}</div>"
        f"<div class='muted'>{escape(note)}</div>"
        "</div>"
    )


def _tier_row(label: str, values: Mapping[str, Any]) -> str:
    return (
        "<tr>"
        f"<td><strong>{escape(label)}</strong></td>"
        f"<td>{_num(values.get('candidates'))}</td>"
        f"<td>{_num(values.get('settled'))}</td>"
        f"<td>{_num(values.get('hits'))}</td>"
        f"<td>{_pct(values.get('hit_rate_pct'))}</td>"
        f"<td>{_pct(values.get('avg_estimated_hit_pct'))}</td>"
        f"<td>{_pct(values.get('calibration_gap_pct_points'))}</td>"
        "</tr>"
    )


def _card_row(size: str, values: Mapping[str, Any]) -> str:
    return (
        "<tr>"
        f"<td><strong>{escape(size)}-leg</strong></td>"
        f"<td>{_num(values.get('created'))}</td>"
        f"<td>{_num(values.get('settled'))}</td>"
        f"<td>{_num(values.get('all_hit'))}</td>"
        f"<td>{_pct(values.get('card_hit_rate_pct'))}</td>"
        "</tr>"
    )


def _section(db) -> str:
    try:
        s = dict(scoreboard(db))
    except Exception as exc:
        return (
            "<section id='fhg1'>"
            "<div class='section-title'><div><h2>First-Half Goal Lab (FHG1)</h2>"
            "<div class='muted'>Forward-only first-half goal research.</div></div>"
            "<a href='/first-half-goals'>Full detail →</a></div>"
            "<div class='panel'><span class='pill warn'>CHECK</span> "
            f"<span class='muted'>Dashboard metrics unavailable: {escape(type(exc).__name__)}</span></div>"
            "</section>"
        )

    all_fwd = dict(s.get("all_forward") or {})
    qual = dict(s.get("qualified_forward") or {})
    tiers = dict(s.get("tiers") or {})
    cards = dict(s.get("cards") or {})

    metrics = "".join(
        (
            _metric(
                "Forward fixtures",
                _num(all_fwd.get("candidates")),
                "all frozen pre-match FHG1 observations",
            ),
            _metric(
                "Qualified ≥70%",
                _num(qual.get("candidates")),
                "A/B/C candidates at the research threshold",
            ),
            _metric(
                "Qualified hit rate",
                _pct(qual.get("hit_rate_pct")),
                f"{_num(qual.get('hits'))}/{_num(qual.get('settled'))} settled hits",
            ),
            _metric(
                "Calibration gap",
                _pct(qual.get("calibration_gap_pct_points")),
                "actual minus estimated hit rate; nearer 0 is better",
            ),
            _metric(
                "Historical baseline",
                _pct(s.get("historical_first_half_goal_rate_pct")),
                f"{_num(s.get('historical_half_time_matches'))} half-time-score matches",
            ),
            _metric(
                "Qualified estimate",
                _pct(qual.get("avg_estimated_hit_pct")),
                "mean frozen probability for settled qualified picks",
            ),
        )
    )

    tier_rows = "".join(
        _tier_row(t, dict(tiers.get(t) or {})) for t in ("A", "B", "C", "WATCH")
    )
    card_rows = "".join(
        _card_row(size, dict(cards.get(size) or {})) for size in ("8", "9", "10")
    )

    return f"""
    <section id='fhg1'>
      <div class='section-title'>
        <div><h2>First-Half Goal Lab (FHG1)</h2>
        <div class='muted'>Forward-only study of whether a first-half goal can be identified consistently. Research only; no betting authority and no EV claim until price capture exists.</div></div>
        <a href='/first-half-goals'>Full detail →</a>
      </div>
      <div class='grid compact'>{metrics}</div>
      <div class='panel' style='margin-top:12px'>
        <div class='table-wrap'><table>
          <thead><tr><th>Tier</th><th>Captured</th><th>Settled</th><th>Hits</th><th>Hit rate</th><th>Estimated</th><th>Calibration gap</th></tr></thead>
          <tbody>{tier_rows}</tbody>
        </table></div>
      </div>
      <div class='panel' style='margin-top:12px'>
        <div class='section-title' style='margin-top:0'><div><h3 style='margin:0'>8/9/10-leg card simulation</h3><div class='muted'>Same-kickoff cards built only when enough ≥70% qualifiers exist.</div></div></div>
        <div class='table-wrap'><table>
          <thead><tr><th>Card</th><th>Created</th><th>Settled</th><th>All-hit</th><th>Card hit rate</th></tr></thead>
          <tbody>{card_rows}</tbody>
        </table></div>
      </div>
    </section>
    """


def install(app, db) -> Dict[str, Any]:
    root = next(
        (
            r
            for r in list(app.router.routes)
            if getattr(r, "path", None) == "/"
            and "GET" in (getattr(r, "methods", set()) or set())
        ),
        None,
    )
    if root is None:
        return {"installed": False, "version": VERSION, "reason": "root_not_found"}
    if getattr(root.endpoint, "_fhg1_curated_section", False):
        return {"installed": True, "already_installed": True, "version": VERSION}

    original = root.endpoint
    name = root.name
    include = getattr(root, "include_in_schema", True)
    app.router.routes.remove(root)

    async def wrapped_home():
        response = original()
        if inspect.isawaitable(response):
            response = await response
        if not isinstance(response, HTMLResponse):
            return response
        html = response.body.decode("utf-8", errors="replace")

        if "href='#fhg1'" not in html:
            anchor = "<a href='/clv-first'>CLV & Price</a>"
            replacement = "<a href='#fhg1'>First-Half Goals</a>" + anchor
            if anchor in html:
                html = html.replace(anchor, replacement, 1)

        if "<section id='fhg1'>" not in html:
            block = _section(db)
            anchor = "<section id='predictive'>"
            if anchor in html:
                html = html.replace(anchor, block + anchor, 1)
            else:
                html = html.replace("</body>", block + "</body>", 1)

        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return HTMLResponse(html, status_code=response.status_code, headers=headers)

    wrapped_home._fhg1_curated_section = True
    app.add_api_route(
        "/",
        wrapped_home,
        methods=["GET"],
        response_class=HTMLResponse,
        name=name,
        include_in_schema=include,
    )
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "route": "/first-half-goals",
        "homepage_section": "#fhg1",
    }
