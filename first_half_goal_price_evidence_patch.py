from __future__ import annotations

"""Expose the already-captured FHG1 entry prices in research outputs/dashboard.

The pricing collector already freezes the first available best accessible price
for qualified FHG1 selections. This patch makes that evidence visible, exports
it, and calculates observed flat-stake single-selection ROI. It does not change
candidate generation, threshold, card construction or betting authority.
"""

from html import escape
import json
from math import prod
from typing import Any, Dict, Mapping, Sequence

import first_half_goal_lab as fhg

VERSION = "FHG1_PRICE_EVIDENCE_V2"
PRICE_TABLE = "first_half_goal_entry_prices"
_INSTALLED = False
_ORIGINAL_SCOREBOARD = None
_ORIGINAL_DASHBOARD = None
_ORIGINAL_CURATED_SECTION = None


def _rows(db):
    try:
        return [dict(x) for x in db.fetchall(
            """SELECT c.*,p.bookmaker_key,p.bookmaker_title,p.entry_odds,p.captured_at AS price_captured_at,
                      p.implied_probability,p.model_edge_pct
               FROM first_half_goal_candidates c
               LEFT JOIN first_half_goal_entry_prices p ON p.candidate_id=c.id
               ORDER BY c.id"""
        )]
    except Exception:
        return []


def _price_metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    qualified = [x for x in rows if float(x.get("estimated_probability") or 0) >= float(fhg.THRESHOLD)]
    priced = [x for x in qualified if x.get("entry_odds") is not None]
    settled = [x for x in priced if x.get("fhg_hit") is not None and str(x.get("status") or "") == "SETTLED"]
    pnl = 0.0
    for x in settled:
        odds = float(x.get("entry_odds") or 0)
        pnl += (odds - 1.0) if int(x.get("fhg_hit") or 0) else -1.0
    wins = sum(int(x.get("fhg_hit") or 0) for x in settled)
    return {
        "qualified_candidates": len(qualified),
        "priced_candidates": len(priced),
        "price_coverage_pct": len(priced) / len(qualified) * 100.0 if qualified else None,
        "settled_priced": len(settled),
        "wins": wins,
        "hit_rate_pct": wins / len(settled) * 100.0 if settled else None,
        "avg_entry_odds": sum(float(x.get("entry_odds") or 0) for x in priced) / len(priced) if priced else None,
        "avg_model_edge_pct": sum(float(x.get("model_edge_pct") or 0) for x in priced) / len(priced) if priced else None,
        "flat_pnl_units": pnl,
        "flat_roi_pct": pnl / len(settled) * 100.0 if settled else None,
    }


def _card_price_rows(db) -> list[dict[str, Any]]:
    out = []
    try:
        cards = [dict(x) for x in db.fetchall("SELECT * FROM first_half_goal_cards ORDER BY kickoff_time DESC,card_size")]
    except Exception:
        cards = []
    for card in cards:
        try:
            ids = list(json.loads(card.get("event_ids_json") or "[]"))
        except Exception:
            ids = []
        legs = []
        for eid in ids:
            try:
                row = db.fetchone(
                    """SELECT c.status,c.fhg_hit,p.entry_odds,p.bookmaker_key,p.bookmaker_title
                       FROM first_half_goal_candidates c
                       LEFT JOIN first_half_goal_entry_prices p ON p.candidate_id=c.id
                       WHERE c.event_id=?""",
                    (eid,),
                ) or {}
            except Exception:
                row = {}
            legs.append(dict(row))
        priced = [x for x in legs if x.get("entry_odds") is not None]
        synthetic = prod(float(x["entry_odds"]) for x in priced) if len(priced) == len(legs) and legs else None
        settled = bool(legs) and all(str(x.get("status") or "") == "SETTLED" and x.get("fhg_hit") is not None for x in legs)
        all_hit = settled and all(int(x.get("fhg_hit") or 0) == 1 for x in legs)
        pnl = None
        if settled and synthetic is not None:
            pnl = synthetic - 1.0 if all_hit else -1.0
        out.append({
            "id": card.get("id"),
            "slate_key": card.get("slate_key"),
            "card_size": int(card.get("card_size") or 0),
            "status": card.get("status"),
            "hit_legs": int(card.get("hit_legs") or 0),
            "settled_legs": int(card.get("settled_legs") or 0),
            "priced_legs": len(priced),
            "synthetic_best_price": synthetic,
            "synthetic_pnl_units": pnl,
            "all_hit": card.get("all_hit"),
        })
    return out


def scoreboard_with_prices(db) -> Dict[str, Any]:
    base = dict(_ORIGINAL_SCOREBOARD(db))
    rows = _rows(db)
    base["price_capture"] = True
    base["price_evidence"] = _price_metrics(rows)
    base["priced_card_reconstruction"] = {
        "definition": "product of each leg's frozen best accessible entry price; not guaranteed same-book",
        "cards": _card_price_rows(db),
    }
    base["ev_claim"] = False
    base["price_evidence_research_only"] = True
    return base


def _fmt_pct(v):
    return "—" if v is None else f"{float(v):.1f}%"


def _fmt_num(v, digits=2):
    return "—" if v is None else f"{float(v):.{digits}f}"


def dashboard_with_prices(db, style):
    sc = scoreboard_with_prices(db)
    q = sc["qualified_forward"]
    pe = sc["price_evidence"]
    rows = _rows(db)
    rows = sorted(rows, key=lambda r: (0 if str(r.get("status") or "") == "OPEN" else 1, str(r.get("commence_time") or "")))[:120]
    trs = []
    for r in rows:
        dt = fhg._dt(r.get("commence_time"))
        k = dt.astimezone(fhg.LONDON).strftime("%a %d %b %H:%M") if dt else "—"
        res = "—" if r.get("fhg_hit") is None else "HIT" if int(r.get("fhg_hit") or 0) else "MISS"
        xgs = [r.get(f"pred{i}_total_xg") for i in range(1, 5)]
        xgt = " · ".join(f"P{i+1} {float(v):.2f}" for i, v in enumerate(xgs) if v is not None) or "—"
        if r.get("entry_odds") is None:
            price = "—"; book = "—"; edge = "—"
        else:
            price = f"{float(r['entry_odds']):.2f}"
            book = escape(str(r.get("bookmaker_title") or r.get("bookmaker_key") or "—"))
            edge = f"{float(r.get('model_edge_pct') or 0):+.1f}%"
        trs.append(
            f"<tr><td>{k}</td><td><b>{escape(str(r.get('home_team') or ''))} v {escape(str(r.get('away_team') or ''))}</b><br><span class='muted'>{escape(str(r.get('league') or ''))}</span></td>"
            f"<td>{float(r.get('estimated_probability') or 0)*100:.1f}% {escape(str(r.get('tier') or ''))}</td>"
            f"<td>{price}</td><td>{book}</td><td>{edge}</td>"
            f"<td>{int(r.get('home_hits') or 0)}/{int(r.get('home_n') or 0)}</td><td>{int(r.get('away_hits') or 0)}/{int(r.get('away_n') or 0)}</td>"
            f"<td>{escape(xgt)}</td><td>{escape(str(r.get('sample_quality') or ''))}</td><td>{res}</td></tr>"
        )
    if not trs:
        trs = ["<tr><td colspan='11'>No forward FHG1 candidates yet.</td></tr>"]

    crows = []
    for r in _card_price_rows(db)[:40]:
        status = "OPEN" if str(r.get("status") or "") != "SETTLED" else "WON" if int(r.get("all_hit") or 0) else "LOST"
        syn = _fmt_num(r.get("synthetic_best_price"), 2)
        pnl = "—" if r.get("synthetic_pnl_units") is None else f"{float(r['synthetic_pnl_units']):+.2f}u"
        crows.append(
            f"<tr><td>{escape(str(r.get('slate_key') or ''))}</td><td>{r['card_size']}</td>"
            f"<td>{r['priced_legs']}/{r['card_size']}</td><td>{syn}</td>"
            f"<td>{r['hit_legs']}/{r['settled_legs']}</td><td>{status}</td><td>{pnl}</td></tr>"
        )
    if not crows:
        crows = ["<tr><td colspan='7'>No qualified 8/9/10-leg slates yet.</td></tr>"]

    cards = sc["cards"]
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>First-Half Goal Lab</title><style>{style}.fhg{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}.box{{padding:12px;border:1px solid #444;border-radius:10px}}.table-wrap{{overflow-x:auto}}table{{width:100%;border-collapse:collapse}}th,td{{padding:8px;border-bottom:1px solid #333;text-align:left;white-space:nowrap}}</style></head><body><main>
    <div class='nav'><a href='/'>Home</a><a href='/research-progress'>Research Progress</a></div><h1>First-Half Goal Lab</h1>
    <div class='panel'><b>Forward-only FHG1.</b> Qualified candidates now freeze the first available best executable Over 0.5 first-half-goal price from accessible books. Price evidence is research-only; observed ROI is not a claim of future EV. PRED1–PRED4 full-match xG remains comparison-only.</div>
    <div class='fhg'><div class='box'>History<br><b>{sc['historical_half_time_matches']}</b><br>{_fmt_pct(sc['historical_first_half_goal_rate_pct'])}</div>
    <div class='box'>Qualified ≥70%<br><b>{q['candidates']}</b><br>{_fmt_pct(q['hit_rate_pct'])} hit</div>
    <div class='box'>Priced qualified<br><b>{pe['priced_candidates']}/{pe['qualified_candidates']}</b><br>{_fmt_pct(pe['price_coverage_pct'])}</div>
    <div class='box'>Observed singles ROI<br><b>{_fmt_pct(pe['flat_roi_pct'])}</b><br>{float(pe['flat_pnl_units']):+.2f}u · n={pe['settled_priced']}</div>
    <div class='box'>Avg entry odds<br><b>{_fmt_num(pe['avg_entry_odds'],2)}</b><br>model edge {_fmt_pct(pe['avg_model_edge_pct'])}</div>
    <div class='box'>Calibration<br><b>{_fmt_pct(q['avg_estimated_hit_pct'])}</b><br>gap {_fmt_pct(q['calibration_gap_pct_points'])}</div>
    <div class='box'>8-folds<br><b>{cards['8']['all_hit']}/{cards['8']['settled']}</b></div><div class='box'>9-folds<br><b>{cards['9']['all_hit']}/{cards['9']['settled']}</b></div><div class='box'>10-folds<br><b>{cards['10']['all_hit']}/{cards['10']['settled']}</b></div></div>
    <div class='panel'><h2>Forward candidates</h2><div class='table-wrap'><table><thead><tr><th>Kickoff</th><th>Match</th><th>FHG1</th><th>Frozen price</th><th>Book</th><th>Model edge</th><th>Home split</th><th>Away split</th><th>xG comparison</th><th>Sample</th><th>Result</th></tr></thead><tbody>{''.join(trs)}</tbody></table></div></div>
    <div class='panel'><h2>Qualified slate cards</h2><p class='muted'>Synthetic best-price product uses each leg's frozen best accessible price and is <strong>not guaranteed to be available at one bookmaker</strong>. Use it for research only until same-book card pricing is explicitly captured.</p><table><thead><tr><th>Slate</th><th>Legs</th><th>Priced</th><th>Synthetic price</th><th>Hits</th><th>Result</th><th>Synthetic P/L</th></tr></thead><tbody>{''.join(crows)}</tbody></table></div>
    </main></body></html>"""


def curated_section_with_prices(db):
    import first_half_goal_curated_patch as curated
    html = _ORIGINAL_CURATED_SECTION(db)
    try:
        pe = scoreboard_with_prices(db)["price_evidence"]
        priced = f"{int(pe.get('priced_candidates') or 0)}/{int(pe.get('qualified_candidates') or 0)}"
        roi = _fmt_pct(pe.get("flat_roi_pct"))
        pnl = f"{float(pe.get('flat_pnl_units') or 0):+.2f}u · n={int(pe.get('settled_priced') or 0)}"
        extra = (
            curated._metric("Priced qualified", priced, f"{_fmt_pct(pe.get('price_coverage_pct'))} price coverage")
            + curated._metric("Observed singles ROI", roi, pnl)
        )
        html = html.replace("<div class='grid compact'>", "<div class='grid compact'>" + extra, 1)
        html = html.replace(
            "Research only; no betting authority and no EV claim until price capture exists.",
            "Frozen executable prices are now captured prospectively; observed ROI remains research-only and is not a future EV claim.",
        )
    except Exception:
        pass
    return html


def install(db=None) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_SCOREBOARD, _ORIGINAL_DASHBOARD, _ORIGINAL_CURATED_SECTION
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}
    import web as web_module
    import first_half_goal_curated_patch as curated
    db = db or web_module.db
    _ORIGINAL_SCOREBOARD = fhg.scoreboard
    _ORIGINAL_DASHBOARD = fhg._dashboard
    _ORIGINAL_CURATED_SECTION = curated._section
    fhg.scoreboard = scoreboard_with_prices
    fhg._dashboard = dashboard_with_prices
    curated.scoreboard = scoreboard_with_prices
    curated._section = curated_section_with_prices
    fhg.TABLES = tuple(dict.fromkeys(tuple(fhg.TABLES) + (PRICE_TABLE,)))
    try:
        import exporter
        exporter.EXPORT_TABLES = tuple(dict.fromkeys(tuple(exporter.EXPORT_TABLES) + (PRICE_TABLE,)))
    except Exception:
        pass
    try:
        import split_exporter
        split_exporter.WEEKLY_TABLES = tuple(dict.fromkeys(tuple(split_exporter.WEEKLY_TABLES) + (PRICE_TABLE,)))
        split_exporter.MANUAL_TABLES = tuple(dict.fromkeys(tuple(split_exporter.MANUAL_TABLES) + (PRICE_TABLE,)))
    except Exception:
        pass
    _INSTALLED = True
    return {
        "installed": True,
        "version": VERSION,
        "price_table": PRICE_TABLE,
        "price_capture_visible": True,
        "homepage_price_evidence_visible": True,
        "observed_roi_visible": True,
        "selection_rules_changed": False,
        "betting_authority_changed": False,
    }
