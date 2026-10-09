from __future__ import annotations

"""Unified dashboard for the new research-only edge layers.

Presentation/export wiring only. No selection, staking, Telegram, provider-call,
XS1, or live-betting authority.
"""

from html import escape
import json
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from research_extensions import scoreboard as extension_scoreboard
from pred4_coverage_diagnostic import latest as exact_pred4_coverage
from sports_predictive_dashboard import _scoreboard as sport_scoreboard

DASHBOARD_VERSION = "0.19.32"
API_SPORTS_EXPORT_TABLES = (
    "api_sports_pred_leagues",
    "api_sports_pred_games",
    "api_sports_pred_forecasts",
    "api_sports_pred_shadow_bets",
)


def _f(v):
    try:
        return float(v)
    except Exception:
        return None


def _fmt(v, d=2):
    x = _f(v)
    return "—" if x is None else f"{x:.{d}f}"


def _pct(v):
    return "—" if _f(v) is None else f"{_fmt(v)}%"


def _tone(v):
    x = _f(v)
    return "" if x is None else "ok" if x >= 0 else "bad"


def _model_progress(db, model: str) -> Dict[str, Any]:
    try:
        rows = db.fetchall(
            "SELECT * FROM football_predictive_research_samples WHERE model_name=? AND evidence_mode='FORWARD'",
            (model,),
        )
    except Exception:
        rows = []
    accepted = [r for r in rows if str(r.get("decision") or "") == "ACCEPT"]
    settled = [r for r in rows if str(r.get("status") or "") == "SETTLED"]
    accepted_settled = [r for r in settled if str(r.get("decision") or "") == "ACCEPT" and _f(r.get("pnl_units")) is not None]
    pnl = sum(_f(r.get("pnl_units")) or 0.0 for r in accepted_settled)
    clv = [_f(r.get("clv_pct")) for r in accepted_settled if str(r.get("clv_quality") or "") in {"A", "B"} and _f(r.get("clv_pct")) is not None]
    return {
        "forward": len(rows),
        "accepted": len(accepted),
        "settled": len(accepted_settled),
        "pnl_units": pnl,
        "roi_pct": pnl / len(accepted_settled) * 100.0 if accepted_settled else None,
        "avg_ab_clv_pct": sum(clv) / len(clv) if clv else None,
    }


def _micro_progress(db) -> Dict[str, Any]:
    try:
        rows = db.fetchall(
            "SELECT * FROM football_predictive_research_samples WHERE model_name='MICRO' AND evidence_mode='FORWARD'"
        )
    except Exception:
        rows = []
    settled = [r for r in rows if str(r.get("status") or "") == "SETTLED"]
    coverage = {k: 0 for k in ("velocity", "dispersion", "leader", "persistence", "exchange_gap")}
    for r in rows:
        try:
            feat = json.loads(str(r.get("features_json") or "{}"))
        except Exception:
            feat = {}
        coverage["velocity"] += int(feat.get("implied_probability_velocity_pp_per_hour") is not None)
        coverage["dispersion"] += int(feat.get("latest_dispersion") is not None)
        coverage["leader"] += int(feat.get("current_leader") is not None)
        coverage["persistence"] += int(feat.get("anomaly_persistence_minutes") is not None)
        coverage["exchange_gap"] += int(feat.get("exchange_fixed_gap_pct") is not None)
    denom = len(rows) or 1
    return {
        "forward": len(rows),
        "settled": len(settled),
        "coverage_pct": {k: v / denom * 100.0 for k, v in coverage.items()},
    }


def snapshot(db) -> Dict[str, Any]:
    ext = extension_scoreboard(db)
    exact = exact_pred4_coverage(db)
    micro = _micro_progress(db)
    nfl = sport_scoreboard(db, "NFL-PRED1")
    euro_pred = sport_scoreboard(db, "EUROLEAGUE-PRED1")
    return {
        "dashboard_version": DASHBOARD_VERSION,
        "forward_start_at": ext.get("forward_start_at"),
        "pred5": ext.get("pred5") or _model_progress(db, "PRED5"),
        "pred6": ext.get("pred6") or _model_progress(db, "PRED6"),
        "agreement": ext.get("agreement") or {"buckets": {}},
        "microstructure": micro,
        "euroleague_clv_audit": ext.get("euroleague_clv_audit") or {},
        "pred4_broad_coverage": ext.get("pred4_coverage_audit") or {},
        "pred4_exact_coverage": exact,
        "sports_models": {"NFL-PRED1": nfl, "EUROLEAGUE-PRED1": euro_pred},
    }


def _patch_exports() -> None:
    try:
        import exporter
        exporter.EXPORT_TABLES = tuple(dict.fromkeys(tuple(exporter.EXPORT_TABLES) + API_SPORTS_EXPORT_TABLES))
    except Exception:
        pass
    try:
        import split_exporter
        split_exporter.WEEKLY_TABLES = tuple(dict.fromkeys(tuple(split_exporter.WEEKLY_TABLES) + API_SPORTS_EXPORT_TABLES))
    except Exception:
        pass


def _home_panel(db) -> str:
    s = snapshot(db)
    p5, p6 = s["pred5"], s["pred6"]
    micro = s["microstructure"]
    exact = s["pred4_exact_coverage"]
    eu = s["euroleague_clv_audit"]
    buckets = s["agreement"].get("buckets", {})
    agreement_settled = sum(int(v.get("settled") or 0) for v in buckets.values())
    nfl = s["sports_models"]["NFL-PRED1"]
    eusp = s["sports_models"]["EUROLEAGUE-PRED1"]
    return f"""
    <div class='panel priority'>
      <h2>Edge Research Lab <span class='pill'>v{DASHBOARD_VERSION} · FORWARD SHADOW</span></h2>
      <div class='muted'>New PRED layers and diagnostics are frozen research only. Backfill does not count as forward evidence.</div>
      <div class='table-wrap'><table><thead><tr><th>Lane</th><th>Forward</th><th>Accepted</th><th>Settled</th><th>P&L</th><th>ROI</th><th>A/B CLV</th><th>Status</th></tr></thead><tbody>
        <tr><td><strong>PRED5 residual/value</strong></td><td>{p5.get('forward_samples',0)}</td><td>{p5.get('forward_accepted',0)}</td><td>{p5.get('settled',0)}</td><td class='{_tone(p5.get('pnl_units'))}'>{_fmt(p5.get('pnl_units'))}u</td><td>{_pct(p5.get('roi_pct'))}</td><td>{_pct(p5.get('avg_ab_clv_pct'))}</td><td>COLLECTING</td></tr>
        <tr><td><strong>PRED6 form transition</strong></td><td>{p6.get('forward_samples',0)}</td><td>{p6.get('forward_accepted',0)}</td><td>{p6.get('settled',0)}</td><td class='{_tone(p6.get('pnl_units'))}'>{_fmt(p6.get('pnl_units'))}u</td><td>{_pct(p6.get('roi_pct'))}</td><td>{_pct(p6.get('avg_ab_clv_pct'))}</td><td>COLLECTING</td></tr>
        <tr><td><strong>Agreement matrix</strong></td><td>—</td><td>—</td><td>{agreement_settled}</td><td>—</td><td>—</td><td>—</td><td>FROZEN BUCKETS</td></tr>
        <tr><td><strong>Market microstructure</strong></td><td>{micro.get('forward',0)}</td><td>observe</td><td>{micro.get('settled',0)}</td><td>—</td><td>—</td><td>—</td><td>CAPTURING</td></tr>
      </tbody></table></div>
      <div class='muted' style='margin-top:10px'>PRED4 exact coverage: <strong>{escape(str(exact.get('status','WAITING')))}</strong> · due {exact.get('due_events',0)} · existing {exact.get('existing_predictions',0)} · mechanical gaps {exact.get('fit_ok_missing_predictions',0)}. EuroLeague CLV audit: <strong>{escape(str(eu.get('status','WAITING')))}</strong> · median A/B CLV {_pct(eu.get('median_ab_clv_pct'))}. API-Sports: NFL <strong>{escape(str(nfl.get('health','WAITING')))}</strong> / EuroLeague <strong>{escape(str(eusp.get('health','WAITING')))}</strong>.</div>
      <div style='margin-top:12px'><a href='/research-progress'>Open Edge Research Lab →</a></div>
    </div>"""


def install(app: FastAPI, db, base_style: str) -> None:
    _patch_exports()

    @app.get("/api/research-progress")
    def research_progress_api():
        return snapshot(db)

    @app.get("/research-progress", response_class=HTMLResponse)
    def research_progress_page():
        s = snapshot(db); p5=s['pred5']; p6=s['pred6']; micro=s['microstructure']; eu=s['euroleague_clv_audit']; broad=s['pred4_broad_coverage']; exact=s['pred4_exact_coverage']; buckets=s['agreement'].get('buckets',{})
        br = ''.join(f"<tr><td>{escape(str(k))}</td><td>{v.get('settled',0)}</td><td class='{_tone(v.get('pnl_units'))}'>{_fmt(v.get('pnl_units'))}u</td><td>{_pct(v.get('roi_pct'))}</td><td>{_pct(v.get('avg_ab_clv_pct'))}</td></tr>" for k,v in buckets.items()) or "<tr><td colspan='5'>No forward settlements yet.</td></tr>"
        mc=micro.get('coverage_pct',{})
        sport_rows=''.join(f"<tr><td>{escape(name)}</td><td>{escape(str(v.get('health','WAITING')))}</td><td>{escape(str(v.get('season') or '—'))}</td><td>{v.get('games_synced_last_run') or 0}</td><td>{v.get('forecasts',0)}</td><td>{v.get('bets',0)}</td><td>{v.get('settled_bets',0)}</td><td class='{_tone(v.get('pnl_units'))}'>{_fmt(v.get('pnl_units'))}u</td></tr>" for name,v in s['sports_models'].items())
        return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Edge Research Lab</title><style>{base_style}body{{max-width:1450px;margin:auto}}.table-wrap{{overflow-x:auto}}@media(max-width:600px){{body{{padding:14px}}table{{min-width:760px}}}}</style></head><body>
        <a href='/'>← Betting Lab</a><h1>Edge Research Lab <span class='pill'>v{DASHBOARD_VERSION}</span></h1><p class='muted'>Forward-only progress for the new challengers and diagnostics. No execution authority.</p>
        <div class='grid'><div class='panel'><h2>PRED5 · residual/value</h2><p>Forward {p5.get('forward_samples',0)} · accepted {p5.get('forward_accepted',0)} · settled {p5.get('settled',0)}</p><p>P&L <strong class='{_tone(p5.get('pnl_units'))}'>{_fmt(p5.get('pnl_units'))}u</strong> · ROI {_pct(p5.get('roi_pct'))} · A/B CLV {_pct(p5.get('avg_ab_clv_pct'))} · Brier {_fmt(p5.get('avg_brier'),4)}</p></div>
        <div class='panel'><h2>PRED6 · form transition</h2><p>Forward {p6.get('forward_samples',0)} · accepted {p6.get('forward_accepted',0)} · settled {p6.get('settled',0)}</p><p>P&L <strong class='{_tone(p6.get('pnl_units'))}'>{_fmt(p6.get('pnl_units'))}u</strong> · ROI {_pct(p6.get('roi_pct'))} · A/B CLV {_pct(p6.get('avg_ab_clv_pct'))} · Brier {_fmt(p6.get('avg_brier'),4)}</p></div></div>
        <div class='panel'><h2>PRED1 / PRED2 / PRED4 agreement matrix</h2><div class='table-wrap'><table><thead><tr><th>Frozen bucket</th><th>Settled</th><th>P&L</th><th>ROI</th><th>A/B CLV</th></tr></thead><tbody>{br}</tbody></table></div></div>
        <div class='panel'><h2>Market microstructure capture</h2><p>Forward samples <strong>{micro.get('forward',0)}</strong> · settled {micro.get('settled',0)}</p><div class='table-wrap'><table><thead><tr><th>Feature</th><th>Forward coverage</th></tr></thead><tbody><tr><td>Price velocity</td><td>{_pct(mc.get('velocity'))}</td></tr><tr><td>Latest dispersion</td><td>{_pct(mc.get('dispersion'))}</td></tr><tr><td>Bookmaker leader</td><td>{_pct(mc.get('leader'))}</td></tr><tr><td>Anomaly persistence</td><td>{_pct(mc.get('persistence'))}</td></tr><tr><td>Exchange vs fixed-book gap</td><td>{_pct(mc.get('exchange_gap'))}</td></tr></tbody></table></div></div>
        <div class='grid'><div class='panel'><h2>EuroLeague CLV forensic audit</h2><p>Status <strong>{escape(str(eu.get('status','WAITING')))}</strong> · settled {eu.get('settled_bets',0)} · P&L {_fmt(eu.get('pnl_units'))}u · ROI {_pct(eu.get('roi_pct'))}</p><p>Mean A/B CLV {_pct(eu.get('avg_ab_clv_pct'))} · median {_pct(eu.get('median_ab_clv_pct'))} · 10% trimmed {_pct(eu.get('trimmed_ab_clv_pct'))} · consensus {_pct(eu.get('avg_consensus_clv_pct'))}</p><p>Extreme observations |CLV| &gt;50%: {eu.get('abs_clv_gt_50_count',0)} · &gt;100%: {eu.get('abs_clv_gt_100_count',0)}</p></div>
        <div class='panel'><h2>PRED4 coverage</h2><p><strong>Exact engine audit:</strong> {escape(str(exact.get('status','WAITING')))} · due {exact.get('due_events',0)} · existing predictions {exact.get('existing_predictions',0)} · mechanical gaps {exact.get('fit_ok_missing_predictions',0)}</p><p><strong>Broad 72h audit:</strong> upcoming {broad.get('upcoming_events',0)} · eligible {broad.get('broadly_eligible_events',0)} · due/no prediction {broad.get('eligible_without_prediction',0)}</p><p class='muted'>{escape(json.dumps(exact.get('reasons',{}),sort_keys=True))}</p></div></div>
        <div class='panel'><h2>API-Sports independent models</h2><div class='table-wrap'><table><thead><tr><th>Model</th><th>Access/health</th><th>Season</th><th>Games synced</th><th>Forecasts</th><th>Bets</th><th>Settled</th><th>P&L</th></tr></thead><tbody>{sport_rows}</tbody></table></div><p class='muted'>The free API-Sports plan currently only exposes seasons 2022–2024. Historical parsing/model validation can continue; current 2026 forward forecasts require upgraded access.</p><p><a href='/sport-predictive'>Open detailed sport predictive page →</a></p></div>
        <p class='muted'>Forward start: {escape(str(s.get('forward_start_at') or '—'))}. New research tables and API-Sports model tables are wired into Full/Weekly exports.</p></body></html>""")

    @app.middleware("http")
    async def inject_research_progress_home_panel(request, call_next):
        response = await call_next(request)
        if request.url.path != "/" or response.status_code != 200 or "text/html" not in str(response.headers.get("content-type") or ""):
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
        headers = dict(response.headers); headers.pop("content-length", None)
        return Response(content=html, status_code=response.status_code, headers=headers, media_type="text/html")
