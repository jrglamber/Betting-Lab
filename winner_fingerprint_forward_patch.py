from __future__ import annotations

"""Freeze discovery fingerprints and measure them prospectively.

Research/presentation only.  This patch never creates, suppresses, sizes, prices,
alerts or executes a bet.  A fingerprint can only become eligible for a manual
promotion review after enough forward settlements and independent price-quality
support have accumulated.
"""

from datetime import datetime, timezone
from html import escape
import json
import math
from statistics import mean
from typing import Any, Dict, Mapping, Optional

from fastapi.responses import JSONResponse

import adaptive_winner_research as core
from db import utc_now_iso

VERSION = "WINNER_FINGERPRINT_FORWARD_V1"
FROZEN_AT = "2026-10-27T14:34:00+00:00"
EXPORT_TABLES = ("winner_fingerprint_cohorts", "winner_fingerprint_memberships")
_INSTALLED = False
_ORIGINAL_RUN_W1 = None
_ORIGINAL_DASHBOARD_HTML = None

# Deliberately simple single-feature cohorts.  Intersections may be researched
# later, but are not retrospectively created here because that would multiply
# the curve-fitting surface.
FINGERPRINTS = (
    {
        "key": "FP_PRED4_MARKETS",
        "label": "PRED4 market selections",
        "feature": "source_lane",
        "value": "PRED4_MARKETS",
        "why": "Directly tied to PRED4 rather than a generic market bucket.",
    },
    {
        "key": "FP_FOOTBALL_150_199",
        "label": "Football at 1.50–1.99",
        "feature": "sport_odds",
        "value": "FOOTBALL|1.5-1.99",
        "why": "Large historical sample; test whether the apparent uplift survives forward pricing.",
    },
    {
        "key": "FP_TENNIS_200_299",
        "label": "Tennis at 2.00–2.99",
        "feature": "sport_odds",
        "value": "TENNIS|2-2.99",
        "why": "Potential mid-price tennis pocket with enough historical support to justify a forward watch.",
    },
    {
        "key": "FP_BTTS",
        "label": "BTTS markets",
        "feature": "market",
        "value": "btts",
        "why": "Very large historical sample; forward ROI/CLV will show whether this is value or just market structure.",
    },
    {
        "key": "FP_FOOTBALL_TOTALS",
        "label": "Football totals",
        "feature": "sport_market",
        "value": "FOOTBALL|totals",
        "why": "Large sample and directly useful to football market-selection research.",
    },
    {
        "key": "FP_ALL_150_199",
        "label": "Any sport at 1.50–1.99",
        "feature": "odds_band",
        "value": "1.5-1.99",
        "why": "Broad control cohort for the short-price effect seen across several sports.",
    },
)


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _dt(value: Any) -> Optional[datetime]:
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if out.tzinfo is None:
            out = out.replace(tzinfo=timezone.utc)
        return out.astimezone(timezone.utc)
    except Exception:
        return None


def _candidate_key(row: Mapping[str, Any]) -> str:
    return core._candidate_key(
        row.get("event_id"), row.get("market_key") or "h2h", row.get("selection"), row.get("point")
    )


def ensure_schema(db) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS winner_fingerprint_cohorts(
            fingerprint_key TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            feature_key TEXT NOT NULL,
            feature_value TEXT NOT NULL,
            frozen_at TEXT NOT NULL,
            historical_n INTEGER,
            historical_hit_rate_pct REAL,
            historical_uplift_pp REAL,
            rationale TEXT,
            authority TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS winner_fingerprint_memberships(
            fingerprint_key TEXT NOT NULL,
            winner_sample_id INTEGER NOT NULL,
            candidate_key TEXT NOT NULL,
            matched_at TEXT NOT NULL,
            PRIMARY KEY(fingerprint_key,winner_sample_id)
        )"""
    )


def _historical_lookup(db) -> Dict[tuple[str, str], Dict[str, Any]]:
    try:
        rows = (core.winner_scoreboard(db).get("historical_discoveries") or [])
    except Exception:
        rows = []
    out: Dict[tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        out[(str(row.get("feature") or ""), str(row.get("value") or ""))] = dict(row)
    return out


def freeze_cohorts(db) -> Dict[str, Any]:
    historical = _historical_lookup(db)
    created = 0
    for spec in FINGERPRINTS:
        existing = db.fetchone(
            "SELECT fingerprint_key FROM winner_fingerprint_cohorts WHERE fingerprint_key=?",
            (spec["key"],),
        )
        if existing:
            continue
        hist = historical.get((spec["feature"], spec["value"]), {})
        now = utc_now_iso()
        db.execute(
            """INSERT INTO winner_fingerprint_cohorts(
                fingerprint_key,label,feature_key,feature_value,frozen_at,
                historical_n,historical_hit_rate_pct,historical_uplift_pp,
                rationale,authority,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                spec["key"], spec["label"], spec["feature"], spec["value"], FROZEN_AT,
                hist.get("n"), hist.get("hit_rate_pct"), hist.get("uplift_pp"),
                spec["why"], "RESEARCH_ONLY_NO_EXECUTION", now, now,
            ),
        )
        created += 1
    return {"created": created, "total": len(FINGERPRINTS), "frozen_at": FROZEN_AT}


def _features(raw_json: Any) -> Dict[str, Any]:
    try:
        payload = json.loads(str(raw_json or "{}"))
        if isinstance(payload, dict) and isinstance(payload.get("features"), dict):
            return dict(payload["features"])
    except Exception:
        pass
    return {}


def sync_memberships(db) -> Dict[str, Any]:
    cohorts = db.fetchall("SELECT * FROM winner_fingerprint_cohorts ORDER BY fingerprint_key")
    if not cohorts:
        return {"matched": 0, "samples_checked": 0}
    samples = db.fetchall(
        "SELECT id,candidate_key,created_at,features_json FROM winner_discovery_samples WHERE created_at>=? ORDER BY id",
        (FROZEN_AT,),
    )
    matched = 0
    for sample in samples:
        feat = _features(sample.get("features_json"))
        for cohort in cohorts:
            if str(feat.get(str(cohort.get("feature_key") or "")) or "") != str(cohort.get("feature_value") or ""):
                continue
            existing = db.fetchone(
                "SELECT winner_sample_id FROM winner_fingerprint_memberships WHERE fingerprint_key=? AND winner_sample_id=?",
                (cohort["fingerprint_key"], sample["id"]),
            )
            if existing:
                continue
            db.execute(
                """INSERT INTO winner_fingerprint_memberships(
                    fingerprint_key,winner_sample_id,candidate_key,matched_at
                ) VALUES(?,?,?,?)""",
                (cohort["fingerprint_key"], sample["id"], sample.get("candidate_key"), utc_now_iso()),
            )
            matched += 1
    return {"matched": matched, "samples_checked": len(samples)}


def _clv_index(db) -> Dict[str, Dict[str, Any]]:
    try:
        rows = db.fetchall(
            """SELECT event_id,market_key,selection,point,actual_clv_pct,clv_quality,created_at
               FROM clv_first_samples
               WHERE created_at>=? AND actual_clv_pct IS NOT NULL
               ORDER BY created_at,id""",
            (FROZEN_AT,),
        )
    except Exception:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        quality = str(row.get("clv_quality") or "").upper()
        if quality and quality not in {"A", "B"}:
            continue
        key = _candidate_key(row)
        if key and key not in out:
            out[key] = row
    return out


def _status(metrics: Mapping[str, Any]) -> tuple[str, str]:
    settled = int(metrics.get("settled") or 0)
    roi = _f(metrics.get("roi_pct"))
    clv_n = int(metrics.get("clv_n") or 0)
    clv = _f(metrics.get("avg_clv_pct"))
    beat = _f(metrics.get("beat_close_pct"))
    tail = _f(metrics.get("largest_winner_share_pct"))

    if settled < 10:
        return "COLLECTING", "No action — build the forward sample."
    if settled < 25:
        return "EARLY", "Too early to judge — keep collecting."
    if settled < 50:
        if roi is not None and roi > 0 and clv is not None and clv > 0:
            return "PROMISING", "Encouraging, but below the 50-settlement review threshold."
        if roi is not None and roi < 0 and clv is not None and clv < 0:
            return "WEAK", "Forward outcomes and price quality both currently disagree with the discovery."
        return "MIXED", "Mixed evidence — no action."

    price_ready = clv_n >= 20 and clv is not None and clv > 0 and beat is not None and beat >= 50.0
    outcome_ready = roi is not None and roi > 0
    tail_ok = tail is None or tail <= 40.0
    if outcome_ready and price_ready and tail_ok:
        return "PROMOTION_REVIEW", "Meets the evidence gate for a manual promotion review; still no automatic authority."
    if outcome_ready and price_ready and not tail_ok:
        return "TAIL_RISK", "Positive forward evidence, but too much profit is concentrated in one winner."
    if roi is not None and roi < 0 and clv is not None and clv < 0:
        return "WEAK", "Forward evidence currently rejects this fingerprint."
    return "MIXED", "Enough outcomes to inspect, but ROI/CLV do not yet agree."


def fingerprint_scoreboard(db) -> Dict[str, Any]:
    sync_memberships(db)
    clv_index = _clv_index(db)
    cohorts = db.fetchall("SELECT * FROM winner_fingerprint_cohorts ORDER BY fingerprint_key")
    rows = []
    for cohort in cohorts:
        samples = db.fetchall(
            """SELECT s.* FROM winner_fingerprint_memberships m
               JOIN winner_discovery_samples s ON s.id=m.winner_sample_id
               WHERE m.fingerprint_key=? ORDER BY s.id""",
            (cohort["fingerprint_key"],),
        )
        settled = [r for r in samples if str(r.get("result") or "").upper() in {"WIN", "LOSS"}]
        wins = sum(1 for r in settled if str(r.get("result") or "").upper() == "WIN")
        pnl_values = [_f(r.get("pnl_units")) for r in settled]
        pnl_values = [x for x in pnl_values if x is not None]
        pnl = sum(pnl_values) if pnl_values else 0.0
        odds = [_f(r.get("entry_odds")) for r in settled]
        odds = [x for x in odds if x is not None and x > 1.0]
        implied = [(1.0 / x) * 100.0 for x in odds]
        positive_pnl = [x for x in pnl_values if x > 0]
        largest_share = None
        if positive_pnl and sum(positive_pnl) > 0:
            largest_share = max(positive_pnl) / sum(positive_pnl) * 100.0

        clvs = []
        for sample in settled:
            clv_row = clv_index.get(str(sample.get("candidate_key") or ""))
            value = _f((clv_row or {}).get("actual_clv_pct"))
            if value is not None:
                clvs.append(value)
        avg_clv = mean(clvs) if clvs else None
        beat_close = (sum(1 for x in clvs if x > 0) / len(clvs) * 100.0) if clvs else None

        metrics = {
            "forward_n": len(samples),
            "settled": len(settled),
            "wins": wins,
            "hit_rate_pct": wins / len(settled) * 100.0 if settled else None,
            "avg_odds": mean(odds) if odds else None,
            "avg_break_even_pct": mean(implied) if implied else None,
            "pnl_units": pnl,
            "roi_pct": pnl / len(pnl_values) * 100.0 if pnl_values else None,
            "clv_n": len(clvs),
            "avg_clv_pct": avg_clv,
            "beat_close_pct": beat_close,
            "largest_winner_share_pct": largest_share,
        }
        status, action = _status(metrics)
        rows.append({
            "fingerprint_key": cohort.get("fingerprint_key"),
            "label": cohort.get("label"),
            "feature": cohort.get("feature_key"),
            "value": cohort.get("feature_value"),
            "frozen_at": cohort.get("frozen_at"),
            "historical_n": cohort.get("historical_n"),
            "historical_hit_rate_pct": cohort.get("historical_hit_rate_pct"),
            "historical_uplift_pp": cohort.get("historical_uplift_pp"),
            "why": cohort.get("rationale"),
            "status": status,
            "action": action,
            "priority_ready": status == "PROMOTION_REVIEW",
            **metrics,
        })
    priority_ready = [r["fingerprint_key"] for r in rows if r.get("priority_ready")]
    return {
        "version": VERSION,
        "frozen_at": FROZEN_AT,
        "cohorts": rows,
        "promotion_gate": {
            "minimum_settled": 50,
            "positive_roi_required": True,
            "minimum_ab_clv_samples": 20,
            "positive_average_ab_clv_required": True,
            "beat_close_pct_minimum": 50.0,
            "largest_winner_share_pct_maximum": 40.0,
        },
        "priority_ready": priority_ready,
        "overlap_warning": "Fingerprint cohorts overlap. Do not add their counts or P&L together.",
        "authority": "RESEARCH_ONLY_NO_EXECUTION_OR_PRIORITY_CHANGE",
    }


def _fmt(value: Any, digits: int = 1, suffix: str = "") -> str:
    x = _f(value)
    return "—" if x is None else f"{x:.{digits}f}{suffix}"


def _status_badge(status: str) -> str:
    icons = {
        "COLLECTING": "⏳",
        "EARLY": "⏳",
        "PROMISING": "👀",
        "PROMOTION_REVIEW": "✅",
        "TAIL_RISK": "⚠️",
        "MIXED": "⚠️",
        "WEAK": "✕",
    }
    return f"{icons.get(status, '•')} {status.replace('_', ' ').title()}"


def _fingerprint_panel(db) -> str:
    board = fingerprint_scoreboard(db)
    cards = []
    for row in board.get("cohorts") or []:
        settled = int(row.get("settled") or 0)
        forward_n = int(row.get("forward_n") or 0)
        history = (
            f"Historical discovery: {int(row['historical_n'])} bets · "
            f"{_fmt(row.get('historical_hit_rate_pct'),1,'%')} shrunk hit rate"
            if row.get("historical_n") is not None else "Historical discovery frozen; summary metric unavailable."
        )
        cards.append(
            "<div class='fp-card'>"
            f"<div class='fp-head'><strong>{escape(str(row.get('label') or ''))}</strong>"
            f"<span class='pill'>{escape(_status_badge(str(row.get('status') or 'COLLECTING')))}</span></div>"
            f"<div class='muted'>{escape(history)}</div>"
            f"<div class='fp-metrics'>"
            f"<div><span class='fp-k'>Forward</span><strong>{settled}/{forward_n}</strong><small>settled / matched</small></div>"
            f"<div><span class='fp-k'>Hit rate</span><strong>{_fmt(row.get('hit_rate_pct'),1,'%')}</strong><small>actual forward</small></div>"
            f"<div><span class='fp-k'>Avg odds</span><strong>{_fmt(row.get('avg_odds'),2)}</strong><small>break-even {_fmt(row.get('avg_break_even_pct'),1,'%')}</small></div>"
            f"<div><span class='fp-k'>ROI</span><strong>{_fmt(row.get('roi_pct'),1,'%')}</strong><small>P&L {_fmt(row.get('pnl_units'),2,'u')}</small></div>"
            f"<div><span class='fp-k'>CLV</span><strong>{_fmt(row.get('avg_clv_pct'),2,'%')}</strong><small>{int(row.get('clv_n') or 0)} A/B samples</small></div>"
            f"<div><span class='fp-k'>Beat close</span><strong>{_fmt(row.get('beat_close_pct'),1,'%')}</strong><small>price quality</small></div>"
            f"</div>"
            f"<p class='fp-action'><strong>What to do:</strong> {escape(str(row.get('action') or ''))}</p>"
            f"<p class='muted fp-why'>{escape(str(row.get('why') or ''))}</p>"
            "</div>"
        )
    card_html = "".join(cards) or "<p>No frozen fingerprint cohorts yet.</p>"
    return (
        "<div class='panel' id='winner-fingerprints'>"
        "<h2>Winner fingerprints — forward test</h2>"
        "<p><strong>How to read this:</strong> the historical pattern only tells us what looked interesting. "
        "The forward ROI and CLV decide whether it is useful. <strong>No fingerprint changes a bet today.</strong></p>"
        "<div class='callout'><strong>Promotion rule:</strong> at least 50 forward settlements, positive ROI, "
        "positive A/B CLV with ≥20 CLV samples, ≥50% beat-close, and no single winner providing >40% of winning profit. "
        "Passing this only triggers a manual review.</div>"
        f"<div class='fp-grid'>{card_html}</div>"
        "<p class='muted'><strong>Important:</strong> these cohorts overlap. For example, one football totals bet at 1.80 can appear in several cards. "
        "Never add their P&L together.</p>"
        "<details><summary>Show original raw discovery table</summary><div id='fp-raw-placeholder'></div></details>"
        "</div>"
    )


def _wrap_dashboard_html(db, base_style: str) -> str:
    html = _ORIGINAL_DASHBOARD_HTML(db, base_style)
    start = html.find("<div class='panel'><h2>Cross-sport winner fingerprints — discovery only</h2>")
    end = html.find("<div class='panel'><h2>TPRED4</h2>", start if start >= 0 else 0)
    if start >= 0 and end > start:
        raw = html[start:end]
        panel = _fingerprint_panel(db).replace("<div id='fp-raw-placeholder'></div>", raw)
        html = html[:start] + panel + html[end:]
    css = """
    .fp-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin-top:14px}
    .fp-card{border:1px solid rgba(148,163,184,.25);border-radius:14px;padding:16px;min-width:0}
    .fp-head{display:flex;justify-content:space-between;gap:10px;align-items:flex-start;margin-bottom:6px}
    .fp-head strong{font-size:1.05rem}
    .fp-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:14px 0}
    .fp-metrics>div{display:flex;flex-direction:column;gap:2px;min-width:0}
    .fp-metrics strong{font-size:1.15rem}
    .fp-metrics small,.fp-k{font-size:.76rem;opacity:.72}
    .fp-action{margin:10px 0 4px}.fp-why{margin:4px 0 0}.callout{padding:12px 14px;border-left:3px solid currentColor;opacity:.9}
    @media(max-width:640px){.fp-grid{grid-template-columns:1fr}.fp-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.fp-head{flex-direction:column}.fp-head .pill{align-self:flex-start}}
    """
    html = html.replace("</style>", css + "</style>", 1)
    return html


def _register_exports() -> None:
    try:
        import exporter
        exporter.EXPORT_TABLES = tuple(dict.fromkeys(tuple(exporter.EXPORT_TABLES) + EXPORT_TABLES))
    except Exception:
        pass
    try:
        import split_exporter
        split_exporter.WEEKLY_TABLES = tuple(dict.fromkeys(tuple(split_exporter.WEEKLY_TABLES) + EXPORT_TABLES))
    except Exception:
        pass


def install(app, db) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_RUN_W1, _ORIGINAL_DASHBOARD_HTML
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    ensure_schema(db)
    frozen = freeze_cohorts(db)
    initial_sync = sync_memberships(db)

    _ORIGINAL_RUN_W1 = core.run_w1

    def wrapped_run_w1(inner_db):
        result = _ORIGINAL_RUN_W1(inner_db)
        sync = sync_memberships(inner_db)
        if isinstance(result, dict):
            result = dict(result)
            result["fingerprint_memberships"] = sync
        return result

    core.run_w1 = wrapped_run_w1

    _ORIGINAL_DASHBOARD_HTML = core._dashboard_html
    core._dashboard_html = _wrap_dashboard_html

    if not any(getattr(r, "path", None) == "/api/winner-fingerprints/status" for r in list(app.router.routes)):
        @app.get("/api/winner-fingerprints/status")
        def api_winner_fingerprints():
            return JSONResponse(fingerprint_scoreboard(db))

    _register_exports()
    _INSTALLED = True
    board = fingerprint_scoreboard(db)
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "frozen_at": FROZEN_AT,
        "cohorts_frozen": frozen,
        "initial_memberships": initial_sync,
        "cohort_count": len(board.get("cohorts") or []),
        "priority_ready": board.get("priority_ready") or [],
        "dashboard": "plain_language_forward_scorecards",
        "raw_discovery_collapsed": True,
        "promotion_gate": board.get("promotion_gate"),
        "exports": list(EXPORT_TABLES),
        "provider_calls_added": 0,
        "selection_rules_changed": False,
        "pricing_rules_changed": False,
        "staking_changed": False,
        "priority_rules_changed": False,
        "betting_authority_changed": False,
    }


__all__ = ["VERSION", "FROZEN_AT", "FINGERPRINTS", "fingerprint_scoreboard", "install"]
