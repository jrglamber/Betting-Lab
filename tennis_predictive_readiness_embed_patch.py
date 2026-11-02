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

VERSION = "TENNIS_PREDICTIVE_READINESS_EMBED_V2_HEALTH_FIX"
_INSTALLED = False


def _fmt(value: Any, digits: int = 1, suffix: str = "") -> str:
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except Exception:
        return "—"


def _fixed_settlement_health(db, now=None) -> Dict[str, Any]:
    """Correct readiness-health SQL by qualifying the joined status column.

    The original execution query joined tennis_execution_bets and tennis_events,
    both of which expose a status column, so an unqualified status='OPEN' could
    raise an ambiguous-column error. This is diagnostic-only and adds no calls.
    """
    now = now or readiness.datetime.now(readiness.timezone.utc)
    cutoff = (now - readiness.timedelta(hours=readiness.STALE_HOURS)).isoformat()
    specs = (
        ("TENNIS_EXECUTION", "tennis_execution_bets", "b.status='OPEN'"),
        ("TPRED3", "tennis_predictive3_predictions", "b.status='OPEN'"),
        ("TPRED4", "tennis_predictive4_predictions", "b.status='OPEN'"),
    )
    lanes = []
    for name, table, status_clause in specs:
        try:
            if table == "tennis_execution_bets":
                row = db.fetchone(
                    f"""
                    SELECT COUNT(*) AS stale,
                           SUM(CASE WHEN r.event_id IS NULL THEN 1 ELSE 0 END) AS missing_result,
                           SUM(CASE WHEN r.event_id IS NOT NULL THEN 1 ELSE 0 END) AS stored_result
                    FROM {table} b
                    JOIN tennis_events e ON e.event_id=b.event_id
                    LEFT JOIN tennis_results r ON r.event_id=b.event_id
                    WHERE {status_clause} AND e.commence_time<=?
                    """,
                    (cutoff,),
                ) or {}
            else:
                row = db.fetchone(
                    f"""
                    SELECT COUNT(*) AS stale,
                           SUM(CASE WHEN r.event_id IS NULL THEN 1 ELSE 0 END) AS missing_result,
                           SUM(CASE WHEN r.event_id IS NOT NULL THEN 1 ELSE 0 END) AS stored_result
                    FROM {table} b
                    LEFT JOIN tennis_results r ON r.event_id=b.event_id
                    WHERE {status_clause} AND b.commence_time<=?
                    """,
                    (cutoff,),
                ) or {}
            stale = int(row.get("stale") or 0)
            missing = int(row.get("missing_result") or 0)
            stored = int(row.get("stored_result") or 0)
            lanes.append({
                "lane": name,
                "stale_open": stale,
                "missing_result": missing,
                "stored_result_not_applied": stored,
                "status": "HEALTHY" if stale == 0 else "LAGGING",
            })
        except Exception as exc:
            lanes.append({
                "lane": name,
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {exc}",
            })
    return {
        "status": "ERROR" if any(x.get("status") == "ERROR" for x in lanes)
                  else "LAGGING" if any(x.get("status") == "LAGGING" for x in lanes)
                  else "HEALTHY",
        "stale_after_hours": readiness.STALE_HOURS,
        "checked_at": now.isoformat(),
        "lanes": lanes,
    }


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

    # Hotfix both the embedded and standalone readiness views. scoreboard()
    # resolves settlement_health from the module at call time, so replacing this
    # function is sufficient without touching any model or settlement authority.
    readiness.settlement_health = _fixed_settlement_health

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
        "settlement_health_fix": "qualified_status_column",
        "provider_calls_added": 0,
        "presentation_only": True,
        "betting_authority_changed": False,
    }
