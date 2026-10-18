from __future__ import annotations

"""Presentation-only dashboard for TPRED1/TPRED2/TPRED3."""

from datetime import datetime, timezone
import inspect
from html import escape
from typing import Any, Dict, Mapping

from fastapi.responses import HTMLResponse

from tennis_predictive import scoreboard, pocket_report
from tennis_predictive3 import scoreboard as scoreboard3
from tennis_rich_source import status as rich_source_status

VERSION = "TPRED_DASHBOARD_V2_1"


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except Exception:
        return "—"


def _short_ts(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%d %b %H:%M UTC")
    except Exception:
        return text[:19].replace("T", " ")


def _metric(label: str, value: str, note: str = "") -> str:
    return (
        "<div class='card metric-card'>"
        f"<div class='label'>{escape(label)}</div>"
        f"<div class='value'>{escape(value)}</div>"
        f"<div class='muted'>{escape(note)}</div>"
        "</div>"
    )


def _model_panel(name: str, model: Mapping[str, Any]) -> str:
    ready = dict(model.get("evidence_ready_walkforward") or {})
    all_wf = dict(model.get("all_walkforward") or {})
    return f"""
    <div class='panel'>
      <h3>{escape(name)}</h3>
      <div class='grid compact'>
        {_metric("Walk-forward samples", str(ready.get("samples") or 0), f"{all_wf.get('samples') or 0} including low-history matches")}
        {_metric("Accuracy", _fmt(ready.get("accuracy_pct"),1,"%"), "chronological walk-forward")}
        {_metric("Brier", _fmt(ready.get("brier"),4), "lower is better")}
        {_metric("Log loss", _fmt(ready.get("log_loss"),4), "lower is better")}
        {_metric("Market Brier", _fmt(ready.get("market_brier"),4), f"{ready.get('market_samples') or 0} comparable samples")}
        {_metric("Vs market", _fmt(ready.get("brier_improvement_vs_market_pct"),1,"%"), "positive = lower Brier than market")}
      </div>
    </div>
    """


def _pocket_rows(values: Mapping[str, Any]) -> str:
    rows = []
    for label, m in values.items():
        rows.append(
            "<tr>"
            f"<td><strong>{escape(str(label))}</strong></td>"
            f"<td>{int(m.get('settled') or 0)}</td>"
            f"<td>{_fmt(m.get('net_pnl_units'),2)}</td>"
            f"<td>{_fmt(m.get('net_roi_pct'),1,'%')}</td>"
            f"<td>{int(m.get('ab_clv_samples') or 0)}</td>"
            f"<td>{_fmt(m.get('avg_ab_clv_pct'),2,'%')}</td>"
            "</tr>"
        )
    return "".join(rows)


def _rich_progress(db) -> Dict[str, Any]:
    try:
        return dict(rich_source_status(db) or {})
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _rich_label(s3: Mapping[str, Any], rich: Mapping[str, Any]) -> str:
    status = str(s3.get("status") or "BACKFILLING")
    matches = int(rich.get("matches") or s3.get("rich_matches") or 0)
    if status == "BACKFILLING":
        return f"COLLECTING {matches}"
    stats = int(rich.get("matches_with_stats") or 0)
    return f"ACTIVE {stats}/{matches}"


def _home_section(db) -> str:
    s = scoreboard(db)
    s3 = scoreboard3(db)
    rich = _rich_progress(db)
    m1 = dict((s.get("models") or {}).get("TPRED1") or {})
    m2 = dict((s.get("models") or {}).get("TPRED2") or {})
    r1 = dict(m1.get("evidence_ready_walkforward") or {})
    r2 = dict(m2.get("evidence_ready_walkforward") or {})
    r3 = dict(s3.get("evidence_ready_walkforward") or {})
    matches = int(rich.get("matches") or s3.get("rich_matches") or 0)
    with_stats = int(rich.get("matches_with_stats") or 0)
    coverage = (with_stats / matches * 100.0) if matches else 0.0
    return f"""
    <section id='tennis-predictive'>
      <div class='section-title'>
        <div><h2>Tennis Predictive Lab (TPRED)</h2>
        <div class='muted'>TPRED1 = history control. TPRED2 = market-aware control. TPRED3 = chronological rich-feature challenger. Research only; no betting authority.</div></div>
        <a href='/tennis-predictive'>Full detail →</a>
      </div>
      <div class='grid compact'>
        {_metric("TPRED1 Brier", _fmt(r1.get("brier"),4), f"{r1.get('samples') or 0} evidence-ready")}
        {_metric("TPRED2 Brier", _fmt(r2.get("brier"),4), f"{r2.get('samples') or 0} evidence-ready")}
        {_metric("TPRED3 Brier", _fmt(r3.get("brier"),4), f"{r3.get('samples') or 0} rich walk-forward")}
        {_metric("TPRED3 vs market", _fmt(r3.get("brier_improvement_vs_market_pct"),1,"%"), "positive is better")}
        {_metric("Forward TPRED3", str(s3.get("forward_open") or 0), "open frozen rich forecasts")}
        {_metric("Rich features", _rich_label(s3, rich), f"{coverage:.1f}% match-stat coverage")}
      </div>
    </section>
    """


def _page(db, base_style: str) -> str:
    s = scoreboard(db)
    s3 = scoreboard3(db)
    rich = _rich_progress(db)
    p = pocket_report(db)
    m1 = dict((s.get("models") or {}).get("TPRED1") or {})
    m2 = dict((s.get("models") or {}).get("TPRED2") or {})
    matches = int(rich.get("matches") or s3.get("rich_matches") or 0)
    with_stats = int(rich.get("matches_with_stats") or 0)
    coverage = (with_stats / matches * 100.0) if matches else 0.0
    disagreements = s.get("top_forward_disagreements") or []
    drows = []
    for r in disagreements:
        drows.append(
            "<tr>"
            f"<td>{escape(str(r.get('tour') or ''))}</td>"
            f"<td><strong>{escape(str(r.get('selection') or ''))}</strong><div class='muted'>v {escape(str(r.get('opponent') or ''))}</div></td>"
            f"<td>{_fmt(r.get('probability'),3)}</td>"
            f"<td>{_fmt(r.get('market_probability'),3)}</td>"
            f"<td>{_fmt(r.get('model_edge_vs_market_pct'),1,'%')}</td>"
            f"<td>{escape(str(r.get('confidence_tier') or ''))}</td>"
            f"<td>{escape(str(r.get('commence_time') or ''))}</td>"
            "</tr>"
        )
    dbody = "".join(drows) or "<tr><td colspan='7'>No current TPRED1/2 model-market disagreement forecasts.</td></tr>"

    return f"""<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>Tennis Predictive Lab</title><style>{base_style}</style></head><body><main class='wrap'>
    <div class='section-title'><div><h1>Tennis Predictive Lab</h1>
      <div class='muted'>Independent TPRED research stack. No execution or betting authority.</div></div><a href='/'>← Main dashboard</a></div>
    <div class='grid compact'>
      {_metric("Canonical results", str(s.get("historical_results") or 0), "existing TPRED1/2 history")}
      {_metric("Rich matches", str(matches), f"{with_stats} with cached serve/return stats")}
      {_metric("Rich stat coverage", f"{coverage:.1f}%", f"{rich.get('rankings') or s3.get('ranking_rows') or 0} ranking rows")}
      {_metric("Rich API today", str(rich.get("daily_used") or 0), f"separate safety cap {rich.get('daily_cap') or 0}/day")}
    </div>
    {_model_panel("TPRED1 — pure tennis control", m1)}
    {_model_panel("TPRED2 — market-aware control", m2)}
    {_model_panel("TPRED3 — rich chronological challenger", s3)}
    <div class='panel'><h2>TPRED3 source progress</h2>
      <div class='grid compact'>
        {_metric("Status", str(s3.get("status") or "BACKFILLING"), "research-only lifecycle")}
        {_metric("Players mapped", str(rich.get("players") or 0), "provider IDs cached locally")}
        {_metric("TPRED3 forward", str(s3.get("forward_open") or 0), f"{s3.get('forward_settled') or 0} settled")}
        {_metric("Last TPRED3 rebuild", _short_ts(s3.get("last_walkforward_at")), "rebuilds as chronological evidence grows")}
      </div>
    </div>
    <div class='panel'><h2>Current TPRED1/2 forward disagreement</h2>
      <p class='muted'>Largest frozen differences between the original TPRED stack and the latest pre-match consensus. These are observations, not bets.</p>
      <div class='table-wrap'><table><thead><tr><th>Tour</th><th>Selection</th><th>Model P</th><th>Market P</th><th>Δ</th><th>Tier</th><th>Starts</th></tr></thead><tbody>{dbody}</tbody></table></div>
    </div>
    <div class='panel'><h2>TS1 descriptive pockets</h2><p class='muted'>Tracked alongside TPRED, never used as hard-coded model rules.</p>
      <h3>Odds bands</h3><div class='table-wrap'><table><thead><tr><th>Band</th><th>Settled</th><th>Net P&L</th><th>ROI</th><th>A/B CLV n</th><th>A/B CLV</th></tr></thead><tbody>{_pocket_rows(p.get('odds_bands') or {})}</tbody></table></div>
      <h3>TS1 edge bands</h3><div class='table-wrap'><table><thead><tr><th>Band</th><th>Settled</th><th>Net P&L</th><th>ROI</th><th>A/B CLV n</th><th>A/B CLV</th></tr></thead><tbody>{_pocket_rows(p.get('edge_bands') or {})}</tbody></table></div>
      <h3>Tour</h3><div class='table-wrap'><table><thead><tr><th>Tour</th><th>Settled</th><th>Net P&L</th><th>ROI</th><th>A/B CLV n</th><th>A/B CLV</th></tr></thead><tbody>{_pocket_rows(p.get('tours') or {})}</tbody></table></div>
      <h3>Reference depth</h3><div class='table-wrap'><table><thead><tr><th>Books</th><th>Settled</th><th>Net P&L</th><th>ROI</th><th>A/B CLV n</th><th>A/B CLV</th></tr></thead><tbody>{_pocket_rows(p.get('reference_books') or {})}</tbody></table></div>
    </div>
    <div class='panel'><h2>Model governance</h2>
      <p>TPRED1 remains the result-history control. TPRED2 remains the frozen market-aware blend. TPRED3 is independent and adds dated ranking strength, surface-adjusted Elo/form, cached serve/return quality and workload.</p>
      <p class='muted'>TPRED3 learns prequentially: correction weights begin at zero, each historical prediction is made before its outcome updates the model, and the V2 safety guard independently removes the provider's winner-first result ordering before prediction. Match statistics enter player state only after the match. No selection, staking or execution authority is added.</p>
    </div>
    </main></body></html>"""


def install(app, db, base_style: str) -> Dict[str, Any]:
    paths = {getattr(r, "path", None) for r in app.router.routes}
    if "/tennis-predictive" not in paths:
        @app.get("/tennis-predictive", response_class=HTMLResponse)
        def tennis_predictive_page():
            return HTMLResponse(_page(db, base_style))

    root = next((r for r in list(app.router.routes) if getattr(r,"path",None)=="/" and "GET" in (getattr(r,"methods",set()) or set())), None)
    if root is None:
        return {"installed": False, "reason": "root_not_found", "version": VERSION}
    if getattr(root.endpoint, "_tpred_home_section", False):
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
        if "<section id='tennis-predictive'>" not in html:
            block = _home_section(db)
            anchor = "<section id='predictive'>"
            if anchor in html:
                html = html.replace(anchor, block + anchor, 1)
            else:
                html = html.replace("</body>", block + "</body>", 1)
        if "href='/tennis-predictive'" not in html:
            marker = "<a href='/tennis'>Tennis</a>"
            if marker in html:
                html = html.replace(marker, marker + " · <a href='/tennis-predictive'>TPRED</a>", 1)
        headers = {k:v for k,v in response.headers.items() if k.lower()!="content-length"}
        return HTMLResponse(html, status_code=response.status_code, headers=headers)

    wrapped_home._tpred_home_section = True
    app.add_api_route("/", wrapped_home, methods=["GET"], response_class=HTMLResponse, name=name, include_in_schema=include)
    return {"installed": True, "version": VERSION, "route": "/tennis-predictive", "homepage_section": "#tennis-predictive"}
