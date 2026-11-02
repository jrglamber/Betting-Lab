from __future__ import annotations

"""Embed forward-readiness evidence inside the existing Tennis Predictive page.

Presentation only. Reuses tennis_forward_readiness_patch.scoreboard and therefore
adds no provider calls, selection changes, staking, alerts or betting authority.
The standalone /tennis-forward-readiness route remains available as a drill-down.
"""

from html import escape
import inspect
from typing import Any, Dict

from fastapi.responses import HTMLResponse

import tennis_forward_readiness_patch as readiness

VERSION = "TENNIS_PREDICTIVE_READINESS_EMBED_V1"
_INSTALLED = False


def _fmt(value: Any, digits: int = 1, suffix: str = "") -> str:
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except Exception:
        return "—"


def _panel_html(db) -> str:
    data = readiness.scoreboard(db)
    labels = {
        "TENNIS_H2H_1_80_1_99": "H2H 1.80–1.99",
        "TENNIS_H2H_2_00_2_99": "H2H 2.00–2.99",
        "TPRED4_VALUE": "TPRED4 value",
        "H2H_TPRED4_AGREE": "H2H + TPRED4 agreement",
    }
    rows = []
    for key, item in (data.get("lanes") or {}).items():
        roi = _fmt(item.get("roi_pct"), 1, "%") if "roi_pct" in item else "—"
        clv = _fmt(item.get("avg_clv_pct"), 2, "%") if "avg_clv_pct" in item else "—"
        brier = _fmt(item.get("avg_brier"), 4) if "avg_brier" in item else "—"
        rows.append(
            "<tr>"
            f"<td><strong>{escape(labels.get(key, key))}</strong>"
            f"<div class='muted'>{escape(str((data.get('definitions') or {}).get(key, '')))}</div></td>"
            f"<td>{item.get('samples', 0)}</td>"
            f"<td>{item.get('settled', 0)}</td>"
            f"<td>{_fmt(item.get('hit_rate_pct'), 1, '%')}</td>"
            f"<td>{roi}</td><td>{clv}</td><td>{brier}</td>"
            f"<td><span class='pill'>{escape(str(item.get('evidence_stage') or '—'))}</span></td>"
            f"<td>{'YES' if item.get('review_ready') else 'NO'}</td>"
            "</tr>"
        )

    health = data.get("settlement") or {}
    health_rows = "".join(
        "<tr>"
        f"<td>{escape(str(x.get('lane') or ''))}</td>"
        f"<td>{escape(str(x.get('status') or ''))}</td>"
        f"<td>{x.get('stale_open', 0)}</td>"
        f"<td>{x.get('missing_result', 0)}</td>"
        f"<td>{x.get('stored_result_not_applied', 0)}</td>"
        "</tr>"
        for x in health.get("lanes", [])
    )

    start = escape(str(data.get("forward_start_at") or "—"))
    return f"""
<style>
#tennis-forward-readiness-inline .tfr-table-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
#tennis-forward-readiness-inline table{{min-width:820px}}
#tennis-forward-readiness-inline .tfr-note{{margin-top:8px}}
</style>
<section id='tennis-forward-readiness-inline' class='panel'>
  <h2>Forward Readiness <span class='pill'>SHADOW · NO EXECUTION AUTHORITY</span></h2>
  <p class='muted'>Clean prospective evidence from {start}. Minimum review point: {readiness.MIN_REVIEW_SETTLED} settled selections. No lane promotes automatically.</p>
  <div class='tfr-table-wrap'>
    <table>
      <thead><tr><th>Lane</th><th>Samples</th><th>Settled</th><th>Hit</th><th>ROI</th><th>Avg CLV</th><th>Brier</th><th>Stage</th><th>Review-ready</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </div>
  <p class='muted tfr-note'>The key comparison is <strong>H2H + TPRED4 agreement</strong>: whether TPRED adds useful filtering to the simple tennis H2H pockets. <a href='/tennis-forward-readiness'>Open detailed readiness view →</a></p>

  <h3>Settlement health <span class='pill'>{escape(str(health.get('status') or 'UNKNOWN'))}</span></h3>
  <p class='muted'>Stored results are reconciled at startup and after each normal tennis cycle. Missing results are shown rather than guessed.</p>
  <div class='tfr-table-wrap'>
    <table>
      <thead><tr><th>Lane</th><th>Status</th><th>Stale open</th><th>Result missing</th><th>Stored result not applied</th></tr></thead>
      <tbody>{health_rows}</tbody>
    </table>
  </div>
</section>
"""


def _inject(response, db):
    if not isinstance(response, HTMLResponse):
        return response
    html = response.body.decode("utf-8")
    if "id='tennis-forward-readiness-inline'" in html or 'id="tennis-forward-readiness-inline"' in html:
        return response
    panel = _panel_html(db)
    html = html.replace("</body>", panel + "</body>", 1) if "</body>" in html else html + panel
    headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    return HTMLResponse(content=html, status_code=response.status_code, headers=headers)


def install(app, db) -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    route = next(
        (
            r for r in list(app.router.routes)
            if getattr(r, "path", None) == "/tennis-predictive"
            and "GET" in (getattr(r, "methods", set()) or set())
        ),
        None,
    )
    if route is None:
        return {"installed": False, "version": VERSION, "error": "tennis_predictive_route_not_found"}

    original = route.endpoint
    name = route.name
    include = getattr(route, "include_in_schema", True)
    app.router.routes.remove(route)

    if inspect.iscoroutinefunction(original):
        async def wrapped():
            response = await original()
            return _inject(response, db)
    else:
        def wrapped():
            response = original()
            if inspect.isawaitable(response):
                raise RuntimeError("Unexpected awaitable from synchronous /tennis-predictive endpoint")
            return _inject(response, db)

    app.add_api_route(
        "/tennis-predictive",
        wrapped,
        methods=["GET"],
        response_class=HTMLResponse,
        name=name,
        include_in_schema=include,
    )
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "route": "/tennis-predictive",
        "embedded_lanes": [
            "TENNIS_H2H_1_80_1_99",
            "TENNIS_H2H_2_00_2_99",
            "TPRED4_VALUE",
            "H2H_TPRED4_AGREE",
        ],
        "provider_calls_added": 0,
        "presentation_only": True,
        "betting_authority_changed": False,
    }
