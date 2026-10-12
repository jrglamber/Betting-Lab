from __future__ import annotations

"""Metric-parity guard for the curated Betting Lab dashboard.

The curated homepage is presentation only, so any metric that repeats an
existing research panel must use the same underlying cohort and settlement
rules as that source panel.  This patch deliberately changes no selection,
model, staking, collection, settlement or execution authority.
"""

from typing import Any, Dict, List, Mapping, Sequence

import dashboard_layout_v2 as layout
from outcome_edge import outcome_edge_report

VERSION = "DASHBOARD_METRIC_PARITY_V1"
_INSTALLED = False
_ORIGINAL_SETTLED_ROWS = None


def _verified_settled_rows(rows: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """Mirror dedicated lane headline settlement rules.

    Multi-Sport can retain a priced result whose venue-specific dead-heat rule
    is not independently verified.  Dedicated Multi-Sport reporting excludes
    those rows from headline P&L/ROI; the curated dashboard must do the same.
    Other lanes are unaffected because they do not carry this quality marker.
    """
    out: List[Mapping[str, Any]] = []
    for row in rows:
        if str(row.get("settlement_quality") or "").upper() == "UNVERIFIED_VENUE_RULE":
            continue
        pnl = row.get("net_pnl_units") if row.get("net_pnl_units") is not None else row.get("pnl_units")
        if layout._f(pnl) is not None:
            out.append(row)
    return out


def _focus_lane(db) -> Dict[str, Any]:
    """Use the exact legacy Outcome Edge 4.00-4.99 cohort on the curated page."""
    focus = dict((outcome_edge_report(db).get("focus_4_to_4_99") or {}))
    n = int(focus.get("selections") or 0)
    roi = layout._f(focus.get("flat_stake_roi_pct"))
    clv = layout._f(focus.get("avg_ab_clv_pct"))

    if n < 30:
        evidence = "TOO EARLY"
    elif roi is not None and roi > 0 and (clv is None or clv >= 0):
        evidence = "PROMISING"
    elif roi is not None and roi < 0 and clv is not None and clv < 0:
        evidence = "WEAK"
    else:
        evidence = "MIXED"

    score = min(n, 150) / 75.0
    if roi is not None:
        score += 1.0 if roi > 0 else -1.0
    if clv is not None:
        score += 1.0 if clv > 0 else -1.0

    return {
        "name": "Football 4.00–4.99",
        "kind": "Odds pocket · unique settled selections across CONSENSUS/PRED1–PRED4",
        "href": "/outcome-edge",
        "settled": n,
        "pnl": layout._f(focus.get("pnl_units")) or 0.0,
        "roi": roi,
        "hit": layout._f(focus.get("hit_rate_pct")),
        "clv": clv,
        "clv_n": int(focus.get("ab_clv_samples") or 0),
        "maturity": layout._maturity(n),
        "evidence": evidence,
        "trend": "—",
        "score": score,
    }


def _pocket_stats(db) -> List[Dict[str, Any]]:
    """Canonical curated pocket definitions.

    Football is the exact Outcome Edge lead shown on the legacy dashboard.
    Multi-Sport pockets use the same raw execution ledger and headline
    settlement-quality guard as the dedicated Multi-Sport page.
    """
    out: List[Dict[str, Any]] = [_focus_lane(db)]
    try:
        multi = [dict(r) for r in db.fetchall(
            "SELECT b.*,e.sport_key,e.league_title FROM multisport_execution_bets b "
            "JOIN multisport_events e ON e.event_id=b.event_id"
        )]
    except Exception:
        multi = []

    pockets = (
        (
            "NFL 2.00–4.99",
            lambda r: "nfl" in str(r.get("sport_key") or "").lower()
            and 2.0 <= float(r.get("offered_odds") or 0) < 5.0,
        ),
        (
            "NCAAF 3.00–4.99",
            lambda r: (
                "ncaaf" in str(r.get("sport_key") or "").lower()
                or "ncaa" in str(r.get("sport_key") or "").lower()
            ) and 3.0 <= float(r.get("offered_odds") or 0) < 5.0,
        ),
        (
            "EuroLeague 1.50–3.99",
            lambda r: "euroleague" in str(r.get("sport_key") or "").lower()
            and 1.5 <= float(r.get("offered_odds") or 0) < 4.0,
        ),
        (
            "MLB 1.50–1.99",
            lambda r: str(r.get("sport_key") or "").lower() == "baseball_mlb"
            and 1.5 <= float(r.get("offered_odds") or 0) < 2.0,
        ),
    )
    for label, predicate in pockets:
        rows = layout._filtered(multi, predicate)
        out.append(layout._lane_stats(label, "Odds pocket", rows, "/multisport"))
    return out


def _close(a: Any, b: Any, tol: float = 1e-9) -> bool:
    x, y = layout._f(a), layout._f(b)
    if x is None or y is None:
        return x is None and y is None
    return abs(x - y) <= tol


def audit(db) -> Dict[str, Any]:
    """Small startup parity check so this class of drift is visible in logs."""
    legacy = dict((outcome_edge_report(db).get("focus_4_to_4_99") or {}))
    curated = _focus_lane(db)
    football_parity = (
        int(curated.get("settled") or 0) == int(legacy.get("selections") or 0)
        and _close(curated.get("pnl"), legacy.get("pnl_units"))
        and _close(curated.get("roi"), legacy.get("flat_stake_roi_pct"))
        and _close(curated.get("hit"), legacy.get("hit_rate_pct"))
        and _close(curated.get("clv"), legacy.get("avg_ab_clv_pct"))
    )
    try:
        row = db.fetchone(
            "SELECT COUNT(*) AS n FROM multisport_execution_bets "
            "WHERE pnl_units IS NOT NULL AND settlement_quality='UNVERIFIED_VENUE_RULE'"
        ) or {}
        unverified = int(row.get("n") or 0)
    except Exception:
        unverified = 0
    return {
        "version": VERSION,
        "football_4_to_4_99_parity": football_parity,
        "football_4_to_4_99": {
            "settled": curated.get("settled"),
            "pnl_units": curated.get("pnl"),
            "roi_pct": curated.get("roi"),
            "hit_rate_pct": curated.get("hit"),
            "avg_ab_clv_pct": curated.get("clv"),
        },
        "euroleague_band": "1.50-3.99",
        "excluded_unverified_multisport_settlements": unverified,
    }


def install(db) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_SETTLED_ROWS
    if _INSTALLED:
        return {"installed": True, "already_installed": True, **audit(db)}

    _ORIGINAL_SETTLED_ROWS = layout._settled_rows
    layout._settled_rows = _verified_settled_rows
    layout._pocket_stats = _pocket_stats
    layout._CACHE["at"] = None
    layout._CACHE["snapshot"] = None
    _INSTALLED = True
    return {"installed": True, "already_installed": False, **audit(db)}
