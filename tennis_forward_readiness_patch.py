from __future__ import annotations

"""Forward-only tennis readiness scoreboard and settlement reconciliation.

Purpose:
- freeze a clean validation clock for the tennis lanes we may eventually bet;
- re-apply already-stored tennis results to execution + TPRED research ledgers at
  startup and after every normal tennis cycle;
- expose a read-only scoreboard for the two H2H price bands, TPRED4 value picks,
  and the H2H + TPRED4 agreement intersection.

This patch adds no provider calls, betting authority, alerts, staking or selection
changes. Missing results remain missing rather than being guessed or fabricated.
"""

from datetime import datetime, timedelta, timezone
from html import escape
import inspect
import json
import math
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from fastapi.responses import HTMLResponse

from db import utc_now_iso
from tennis_shadow import settle_tennis_event
import tennis_predictive3 as tpred3
import adaptive_winner_research as adaptive

VERSION = "TENNIS_FORWARD_READINESS_V1"
MIN_REVIEW_SETTLED = 75
STALE_HOURS = 6.0
_INSTALLED = False


def _dt(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if out.tzinfo is None:
            out = out.replace(tzinfo=timezone.utc)
        return out.astimezone(timezone.utc)
    except Exception:
        return None


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _ensure_schema(db) -> str:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS tennis_forward_readiness_state(
            state_key TEXT PRIMARY KEY,
            state_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    row = db.fetchone(
        "SELECT state_value FROM tennis_forward_readiness_state WHERE state_key='forward_start_at'"
    )
    if row and row.get("state_value"):
        return str(row["state_value"])
    start = utc_now_iso()
    db.execute(
        """
        INSERT INTO tennis_forward_readiness_state(state_key,state_value,updated_at)
        VALUES('forward_start_at',?,?)
        ON CONFLICT(state_key) DO NOTHING
        """,
        (start, start),
    )
    row = db.fetchone(
        "SELECT state_value FROM tennis_forward_readiness_state WHERE state_key='forward_start_at'"
    ) or {}
    return str(row.get("state_value") or start)


def _stage(settled: int) -> str:
    n = int(settled or 0)
    if n < 25:
        return "SEEDING"
    if n < 50:
        return "EARLY"
    if n < MIN_REVIEW_SETTLED:
        return "BUILDING"
    if n < 100:
        return "REVIEW_WINDOW"
    return "MATURE_SAMPLE"


def _settled_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [r for r in rows if str(r.get("result") or "").upper() in {"WIN", "LOSS"}]


def _real_price_metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    settled = _settled_rows(rows)
    wins = sum(1 for r in settled if str(r.get("result") or "").upper() == "WIN")
    pnl_values = []
    for row in settled:
        pnl = _f(row.get("net_pnl_units"))
        if pnl is None:
            pnl = _f(row.get("pnl_units"))
        if pnl is not None:
            pnl_values.append(pnl)
    clv_values = [_f(r.get("clv_pct")) for r in rows]
    clv_values = [v for v in clv_values if v is not None]
    tournaments: Dict[str, int] = {}
    for row in settled:
        label = str(row.get("tournament_title") or row.get("sport_key") or "UNKNOWN")
        tournaments[label] = tournaments.get(label, 0) + 1
    top_tournament = None
    top_share = None
    if tournaments and settled:
        top_tournament, top_n = max(tournaments.items(), key=lambda item: item[1])
        top_share = top_n / len(settled) * 100.0
    roi = (sum(pnl_values) / len(pnl_values) * 100.0) if pnl_values else None
    avg_clv = sum(clv_values) / len(clv_values) if clv_values else None
    return {
        "samples": len(rows),
        "settled": len(settled),
        "wins": wins,
        "hit_rate_pct": wins / len(settled) * 100.0 if settled else None,
        "pnl_units": sum(pnl_values) if pnl_values else None,
        "roi_pct": roi,
        "average_odds": (
            sum(float(r["offered_odds"]) for r in rows if _f(r.get("offered_odds")) is not None)
            / sum(1 for r in rows if _f(r.get("offered_odds")) is not None)
            if any(_f(r.get("offered_odds")) is not None for r in rows) else None
        ),
        "clv_samples": len(clv_values),
        "avg_clv_pct": avg_clv,
        "beat_close_pct": (
            sum(1 for v in clv_values if v > 0) / len(clv_values) * 100.0
            if clv_values else None
        ),
        "largest_tournament": top_tournament,
        "largest_tournament_share_pct": top_share,
        "evidence_stage": _stage(len(settled)),
        "review_target_settled": MIN_REVIEW_SETTLED,
        "review_ready": bool(
            len(settled) >= MIN_REVIEW_SETTLED
            and roi is not None and roi > 0
            and (len(clv_values) < 20 or (avg_clv is not None and avg_clv >= 0))
            and (top_share is None or top_share <= 40.0)
        ),
    }


def _tpred4_metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    settled = [r for r in rows if r.get("outcome") is not None]
    wins = sum(int(r.get("outcome") or 0) for r in settled)
    briers = [_f(r.get("brier")) for r in settled]
    briers = [v for v in briers if v is not None]
    market_briers = []
    for row in settled:
        market = _f(row.get("market_probability"))
        if market is None:
            continue
        y = int(row.get("outcome") or 0)
        market_briers.append((market - y) ** 2)
    edges = [_f(r.get("model_edge_vs_market_pct")) for r in rows]
    edges = [v for v in edges if v is not None]
    model_brier = sum(briers) / len(briers) if briers else None
    market_brier = sum(market_briers) / len(market_briers) if market_briers else None
    return {
        "samples": len(rows),
        "settled": len(settled),
        "wins": wins,
        "hit_rate_pct": wins / len(settled) * 100.0 if settled else None,
        "avg_brier": model_brier,
        "market_brier": market_brier,
        "brier_advantage_vs_market": (
            market_brier - model_brier
            if market_brier is not None and model_brier is not None else None
        ),
        "average_model_edge_pct": sum(edges) / len(edges) if edges else None,
        "evidence_stage": _stage(len(settled)),
        "review_target_settled": MIN_REVIEW_SETTLED,
        "review_ready": bool(
            len(settled) >= MIN_REVIEW_SETTLED
            and model_brier is not None
            and market_brier is not None
            and model_brier < market_brier
        ),
    }


def reconcile_stored_results(db) -> Dict[str, Any]:
    """Idempotently propagate results already stored in tennis_results.

    This deliberately performs zero external calls. If a result is not in
    tennis_results, it remains unresolved and is surfaced by settlement health.
    """
    out: Dict[str, Any] = {"ok": True, "version": VERSION, "provider_calls": 0}
    try:
        rows = db.fetchall(
            """
            SELECT DISTINCT b.event_id,r.winner
            FROM tennis_execution_bets b
            JOIN tennis_results r ON r.event_id=b.event_id
            WHERE b.status='OPEN'
            ORDER BY b.event_id
            """
        )
        out["execution_settled"] = sum(
            settle_tennis_event(db, row["event_id"], row["winner"]) for row in rows
        )
    except Exception as exc:
        out["ok"] = False
        out["execution_error"] = f"{type(exc).__name__}: {exc}"

    try:
        out["tpred3_settled"] = int(tpred3.settle_forward_predictions(db) or 0)
    except Exception as exc:
        out["ok"] = False
        out["tpred3_error"] = f"{type(exc).__name__}: {exc}"

    # TPRED4 intentionally mirrors TPRED3's frozen forward identity. Re-upsert
    # after TPRED3 settlement so TPRED4 receives the same result timestamp and
    # outcome without changing its prediction probability.
    try:
        sync = adaptive._tpred4_current_predictions(db)
        out["tpred4_sync"] = sync
    except Exception as exc:
        out["ok"] = False
        out["tpred4_error"] = f"{type(exc).__name__}: {exc}"

    try:
        out["tennis_w1_sync"] = adaptive.run_tennis_w1(db)
    except Exception as exc:
        out["tennis_w1_error"] = f"{type(exc).__name__}: {exc}"

    try:
        db.record_collector_run(
            "TENNIS_FORWARD_SETTLEMENT_RECONCILE",
            bool(out.get("ok")),
            detail=json.dumps(out, sort_keys=True, default=str),
        )
    except Exception:
        pass
    return out


def settlement_health(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=STALE_HOURS)).isoformat()
    specs = (
        ("TENNIS_EXECUTION", "tennis_execution_bets", "status='OPEN'"),
        ("TPRED3", "tennis_predictive3_predictions", "status='OPEN'"),
        ("TPRED4", "tennis_predictive4_predictions", "status='OPEN'"),
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
            lanes.append({"lane": name, "status": "ERROR", "error": f"{type(exc).__name__}: {exc}"})
    return {
        "status": "ERROR" if any(x.get("status") == "ERROR" for x in lanes)
                  else "LAGGING" if any(x.get("status") == "LAGGING" for x in lanes)
                  else "HEALTHY",
        "stale_after_hours": STALE_HOURS,
        "checked_at": now.isoformat(),
        "lanes": lanes,
    }


def scoreboard(db) -> Dict[str, Any]:
    start = _ensure_schema(db)
    execution = db.fetchall(
        """
        SELECT b.*,e.tour,e.tournament_title,e.tournament_level,e.commence_time
        FROM tennis_execution_bets b
        JOIN tennis_events e ON e.event_id=b.event_id
        WHERE b.created_at>=?
        ORDER BY b.created_at,b.id
        """,
        (start,),
    )
    band_180 = [r for r in execution if (_f(r.get("offered_odds")) or 0) >= 1.80 and (_f(r.get("offered_odds")) or 99) < 2.00]
    band_200 = [r for r in execution if (_f(r.get("offered_odds")) or 0) >= 2.00 and (_f(r.get("offered_odds")) or 99) < 3.00]

    tpred4_all = db.fetchall(
        """
        SELECT * FROM tennis_predictive4_predictions
        WHERE model_version=? AND generated_at>=?
        ORDER BY generated_at,id
        """,
        (adaptive.TPRED4_MODEL_VERSION, start),
    )
    # One candidate side per match: the model-favoured side must also show a
    # positive gap versus the contemporaneous market estimate.
    tpred4_value = [
        r for r in tpred4_all
        if (_f(r.get("probability")) or 0) >= 0.50
        and _f(r.get("market_probability")) is not None
        and (_f(r.get("model_edge_vs_market_pct")) or -999) > 0
    ]
    tpred_lookup = {
        (str(r.get("event_id") or ""), str(r.get("selection") or "")): r
        for r in tpred4_value
    }
    agreement = []
    for row in execution:
        odds = _f(row.get("offered_odds"))
        if odds is None or not (1.80 <= odds < 3.00):
            continue
        pred = tpred_lookup.get((str(row.get("event_id") or ""), str(row.get("selection") or "")))
        if not pred:
            continue
        merged = dict(row)
        merged["tpred4_probability"] = pred.get("probability")
        merged["tpred4_edge_pct"] = pred.get("model_edge_vs_market_pct")
        agreement.append(merged)

    return {
        "version": VERSION,
        "authority": "RESEARCH_ONLY_NO_EXECUTION",
        "forward_start_at": start,
        "promotion_policy": {
            "minimum_settled_before_review": MIN_REVIEW_SETTLED,
            "automatic_promotion": False,
            "note": "Review readiness is evidence only; live betting still requires explicit approval.",
        },
        "settlement": settlement_health(db),
        "lanes": {
            "TENNIS_H2H_1_80_1_99": _real_price_metrics(band_180),
            "TENNIS_H2H_2_00_2_99": _real_price_metrics(band_200),
            "TPRED4_VALUE": _tpred4_metrics(tpred4_value),
            "H2H_TPRED4_AGREE": _real_price_metrics(agreement),
        },
        "definitions": {
            "TENNIS_H2H_1_80_1_99": "Frozen tennis execution H2H selections at 1.80-1.99.",
            "TENNIS_H2H_2_00_2_99": "Frozen tennis execution H2H selections at 2.00-2.99.",
            "TPRED4_VALUE": "TPRED4 model-favoured side with a positive contemporaneous edge versus market probability.",
            "H2H_TPRED4_AGREE": "Frozen H2H 1.80-2.99 selection where TPRED4 independently favours the same player with positive market edge.",
        },
    }


def _fmt(value: Any, digits: int = 1, suffix: str = "") -> str:
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except Exception:
        return "—"


def _dashboard_html(db, base_style: str) -> str:
    data = scoreboard(db)
    rows = []
    labels = {
        "TENNIS_H2H_1_80_1_99": "H2H 1.80–1.99",
        "TENNIS_H2H_2_00_2_99": "H2H 2.00–2.99",
        "TPRED4_VALUE": "TPRED4 value",
        "H2H_TPRED4_AGREE": "H2H + TPRED4 agreement",
    }
    for key, item in data["lanes"].items():
        roi = _fmt(item.get("roi_pct"), 1, "%") if "roi_pct" in item else "—"
        clv = _fmt(item.get("avg_clv_pct"), 2, "%") if "avg_clv_pct" in item else "—"
        brier = _fmt(item.get("avg_brier"), 4) if "avg_brier" in item else "—"
        rows.append(
            f"<tr><td><strong>{escape(labels.get(key,key))}</strong><div class='muted'>{escape(data['definitions'][key])}</div></td>"
            f"<td>{item.get('samples',0)}</td><td>{item.get('settled',0)}</td>"
            f"<td>{_fmt(item.get('hit_rate_pct'),1,'%')}</td><td>{roi}</td><td>{clv}</td><td>{brier}</td>"
            f"<td><span class='pill'>{escape(str(item.get('evidence_stage') or '—'))}</span></td>"
            f"<td>{'YES' if item.get('review_ready') else 'NO'}</td></tr>"
        )
    health = data.get("settlement") or {}
    hrows = "".join(
        f"<tr><td>{escape(str(x.get('lane') or ''))}</td><td>{escape(str(x.get('status') or ''))}</td>"
        f"<td>{x.get('stale_open',0)}</td><td>{x.get('missing_result',0)}</td><td>{x.get('stored_result_not_applied',0)}</td></tr>"
        for x in health.get("lanes", [])
    )
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Tennis Forward Readiness</title><style>{base_style}.table-wrap{{overflow-x:auto}}body{{max-width:1500px;margin:auto}}</style></head><body><a href='/'>← Betting Lab</a><h1>Tennis Forward Readiness <span class='pill'>SHADOW · NO EXECUTION AUTHORITY</span></h1><p class='muted'>Clean forward clock: {escape(str(data.get('forward_start_at') or '—'))}. Minimum review target: {MIN_REVIEW_SETTLED} settled selections. No lane promotes automatically.</p><div class='panel'><h2>Forward lanes</h2><div class='table-wrap'><table><thead><tr><th>Lane</th><th>Samples</th><th>Settled</th><th>Hit</th><th>ROI</th><th>Avg CLV</th><th>Brier</th><th>Stage</th><th>Review-ready</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></div><div class='panel'><h2>Settlement health <span class='pill'>{escape(str(health.get('status') or 'UNKNOWN'))}</span></h2><p class='muted'>Stored results are re-applied at startup and after every normal tennis cycle. Missing results are surfaced, never guessed.</p><div class='table-wrap'><table><thead><tr><th>Lane</th><th>Status</th><th>Stale open</th><th>Result missing</th><th>Stored result not applied</th></tr></thead><tbody>{hrows}</tbody></table></div></div></body></html>"""


def _surface_home_link(app) -> bool:
    route = next(
        (r for r in list(app.router.routes)
         if getattr(r, "path", None) == "/"
         and "GET" in (getattr(r, "methods", set()) or set())),
        None,
    )
    if route is None:
        return False
    original = route.endpoint
    name = route.name
    include = getattr(route, "include_in_schema", True)
    app.router.routes.remove(route)

    async def wrapped():
        response = original()
        if inspect.isawaitable(response):
            response = await response
        if not isinstance(response, HTMLResponse):
            return response
        html = response.body.decode("utf-8")
        if "href='/tennis-forward-readiness'" not in html:
            link = "<a href='/tennis-forward-readiness'>Tennis Forward Readiness</a>"
            marker = "<a href='/tennis-predictive'>Tennis Predictive</a>"
            html = html.replace(marker, marker + link, 1) if marker in html else html.replace("<div class='nav'>", "<div class='nav'>" + link, 1)
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return HTMLResponse(content=html, status_code=response.status_code, headers=headers)

    app.add_api_route("/", wrapped, methods=["GET"], response_class=HTMLResponse, name=name, include_in_schema=include)
    return True


def install(app, db, base_style: str, tennis_engine=None) -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}
    start = _ensure_schema(db)
    startup = reconcile_stored_results(db)

    @app.get("/api/tennis-forward-readiness")
    def api_status():
        return scoreboard(db)

    @app.get("/tennis-forward-readiness")
    def dashboard():
        return HTMLResponse(_dashboard_html(db, base_style))

    runtime_wrapped = False
    if tennis_engine is not None and not getattr(tennis_engine, "_tennis_forward_readiness_installed", False):
        original = tennis_engine.one_cycle

        def wrapped_one_cycle(*args, **kwargs):
            result = original(*args, **kwargs)
            reconciliation = reconcile_stored_results(db)
            if isinstance(result, dict):
                result = dict(result)
                result["tennis_forward_settlement"] = reconciliation
            return result

        tennis_engine.one_cycle = wrapped_one_cycle
        tennis_engine._tennis_forward_readiness_installed = True
        runtime_wrapped = True

    home_link = _surface_home_link(app)
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "forward_start_at": start,
        "startup_reconciliation": startup,
        "runtime_wrapped": runtime_wrapped,
        "home_link": home_link,
        "provider_calls_added": 0,
        "betting_authority": False,
        "alerts_changed": False,
        "staking_changed": False,
    }
