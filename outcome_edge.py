from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from db import Database, utc_now_iso


ODDS_BANDS: Tuple[Tuple[str, float, Optional[float]], ...] = (
    ("1.01-1.49", 1.01, 1.50),
    ("1.50-1.99", 1.50, 2.00),
    ("2.00-2.99", 2.00, 3.00),
    ("3.00-3.99", 3.00, 4.00),
    ("4.00-4.99", 4.00, 5.00),
    ("5.00-7.49", 5.00, 7.50),
    ("7.50-9.99", 7.50, 10.00),
    ("10.00-14.99", 10.00, 15.00),
    ("15.00+", 15.00, None),
)


def odds_band(odds: float) -> str:
    value = float(odds)
    for label, lo, hi in ODDS_BANDS:
        if value >= lo and (hi is None or value < hi):
            return label
    return "OTHER"


def _point_key(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.4f}"
    except Exception:
        return str(value)


def _result_hit(value: Any) -> Optional[int]:
    result = str(value or "").upper().strip()
    if result in {"WIN", "WON"}:
        return 1
    if result in {"LOSS", "LOST"}:
        return 0
    return None


def _source_rows(db: Database) -> List[Dict[str, Any]]:
    """Return every settled stored selection used by Outcome Edge.

    PRED h2h bets and derived-market bets live in different tables, so both
    must be included explicitly. Earlier Outcome Edge versions queried
    market_key/point from the h2h-only tables and therefore silently skipped
    PRED1/PRED2/PRED3 when that query failed. v0.19 fixes that coverage gap.
    """
    queries = (
        ("CONSENSUS", """SELECT event_id,market_key,selection,point,offered_odds,result,clv_pct,clv_quality,net_pnl_units,created_at FROM execution_shadow_bets WHERE result IS NOT NULL AND offered_odds>1.0"""),
        ("PRED1", """SELECT event_id,'h2h' AS market_key,selection,NULL AS point,offered_odds,result,clv_pct,clv_quality,net_pnl_units,created_at FROM football_predictive_bets WHERE result IS NOT NULL AND offered_odds>1.0"""),
        ("PRED1", """SELECT event_id,market_key,selection,point,offered_odds,result,clv_pct,clv_quality,net_pnl_units,created_at FROM football_predictive_market_bets WHERE result IS NOT NULL AND offered_odds>1.0"""),
        ("PRED2", """SELECT event_id,'h2h' AS market_key,selection,NULL AS point,offered_odds,result,clv_pct,clv_quality,net_pnl_units,created_at FROM football_predictive2_bets WHERE result IS NOT NULL AND offered_odds>1.0"""),
        ("PRED2", """SELECT event_id,market_key,selection,point,offered_odds,result,clv_pct,clv_quality,net_pnl_units,created_at FROM football_predictive2_market_bets WHERE result IS NOT NULL AND offered_odds>1.0"""),
        ("PRED3", """SELECT event_id,'h2h' AS market_key,selection,NULL AS point,offered_odds,result,clv_pct,clv_quality,net_pnl_units,created_at FROM football_predictive3_bets WHERE result IS NOT NULL AND offered_odds>1.0"""),
        ("PRED3", """SELECT event_id,market_key,selection,point,offered_odds,result,clv_pct,clv_quality,net_pnl_units,created_at FROM football_predictive3_market_bets WHERE result IS NOT NULL AND offered_odds>1.0"""),
        ("PRED4", """SELECT event_id,'h2h' AS market_key,selection,NULL AS point,offered_odds,result,clv_pct,clv_quality,net_pnl_units,created_at FROM football_predictive4_bets WHERE result IS NOT NULL AND offered_odds>1.0"""),
        ("PRED4", """SELECT event_id,market_key,selection,point,offered_odds,result,clv_pct,clv_quality,net_pnl_units,created_at FROM football_predictive4_market_bets WHERE result IS NOT NULL AND offered_odds>1.0"""),
    )
    rows: List[Dict[str, Any]] = []
    for source, query in queries:
        try:
            part = db.fetchall(query)
        except Exception:
            continue
        for row in part:
            hit = _result_hit(row.get("result"))
            if hit is None:
                continue
            item = dict(row)
            item["source"] = source
            item["hit"] = hit
            rows.append(item)
    return rows


def unique_selection_rows(db: Database) -> List[Dict[str, Any]]:
    """Collapse repeated source-model appearances of the same settled selection.

    The representative row keeps the highest *recorded executable* entry price.
    This mirrors the research question: if multiple independent lanes identify the
    same outcome, what was the best price actually captured by our stored shadows?
    The result itself is outcome-level and therefore identical across duplicates.
    """
    grouped: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    for row in _source_rows(db):
        key = (
            str(row.get("event_id") or ""),
            str(row.get("market_key") or ""),
            str(row.get("selection") or ""),
            _point_key(row.get("point")),
        )
        try:
            odds = float(row.get("offered_odds") or 0.0)
        except Exception:
            continue
        existing = grouped.get(key)
        if existing is None or odds > float(existing.get("offered_odds") or 0.0):
            item = dict(row)
            item["sources"] = {str(row.get("source"))}
            grouped[key] = item
        else:
            existing.setdefault("sources", set()).add(str(row.get("source")))
    out: List[Dict[str, Any]] = []
    for row in grouped.values():
        item = dict(row)
        sources = item.pop("sources", set())
        item["source"] = "+".join(sorted(x for x in sources if x)) or str(item.get("source") or "")
        out.append(item)
    return out


def _dedupe_rows(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("event_id") or ""),
            str(row.get("market_key") or ""),
            str(row.get("selection") or ""),
            _point_key(row.get("point")),
        )
        try:
            odds = float(row.get("offered_odds") or 0.0)
        except Exception:
            continue
        existing = grouped.get(key)
        if existing is None or odds > float(existing.get("offered_odds") or 0.0):
            grouped[key] = dict(row)
    return list(grouped.values())


def _watch_cohort_rows(db: Database, cohort_key: str) -> List[Dict[str, Any]]:
    if cohort_key == "ODDS_4_TO_7_49":
        return [r for r in unique_selection_rows(db) if 4.0 <= float(r["offered_odds"]) < 7.5]
    if cohort_key == "ODDS_4_TO_4_99":
        return [r for r in unique_selection_rows(db) if 4.0 <= float(r["offered_odds"]) < 5.0]
    if cohort_key == "PRED12_BTTS":
        rows = [
            r for r in _source_rows(db)
            if str(r.get("source")) in {"PRED1", "PRED2"}
            and str(r.get("market_key")) == "btts"
        ]
        return _dedupe_rows(rows)
    return []


def ensure_watch_cohorts(db: Database) -> List[Dict[str, Any]]:
    definitions = (
        ("PRED12_BTTS", "PRED1/PRED2 BTTS", "Unique settled BTTS selections from PRED1/PRED2; best stored executable price retained."),
        ("ODDS_4_TO_4_99", "Odds 4.00–4.99", "Unique settled selections with stored executable entry odds >=4.00 and <5.00."),
        ("ODDS_4_TO_7_49", "Odds 4.00–7.49", "Unique settled selections with stored executable entry odds >=4.00 and <7.50."),
    )
    stamp = utc_now_iso()
    for key, label, definition in definitions:
        row = db.fetchone("SELECT cohort_key FROM outcome_edge_watch_cohorts WHERE cohort_key=?", (key,))
        if not row:
            db.execute(
                "INSERT INTO outcome_edge_watch_cohorts(cohort_key,label,definition,frozen_at,created_at) VALUES(?,?,?,?,?)",
                (key, label, definition, stamp, stamp),
            )
    return db.fetchall("SELECT * FROM outcome_edge_watch_cohorts ORDER BY cohort_key")


def watch_cohort_report(db: Database) -> List[Dict[str, Any]]:
    # Cohorts are frozen by startup/maintenance. Reporting must never create a
    # cohort, otherwise the act of viewing research can change its boundary.
    registry = db.fetchall("SELECT * FROM outcome_edge_watch_cohorts ORDER BY cohort_key")
    out: List[Dict[str, Any]] = []
    for meta in registry:
        key = str(meta["cohort_key"])
        frozen_at = str(meta["frozen_at"])
        rows = _watch_cohort_rows(db, key)
        pre = [r for r in rows if str(r.get("created_at") or "") < frozen_at]
        forward = [r for r in rows if str(r.get("created_at") or "") >= frozen_at]
        out.append({
            "cohort_key": key,
            "label": meta["label"],
            "definition": meta["definition"],
            "frozen_at": frozen_at,
            "discovery_sample": _aggregate(pre),
            "forward_sample": _aggregate(forward),
            "all_time": _aggregate(rows),
        })
    return out


def _wilson_interval(wins: int, n: int, z: float = 1.959963984540054) -> Tuple[Optional[float], Optional[float]]:
    if n <= 0:
        return None, None
    p = wins / n
    denom = 1.0 + (z * z) / n
    centre = (p + (z * z) / (2.0 * n)) / denom
    margin = z * math.sqrt((p * (1.0 - p) / n) + (z * z) / (4.0 * n * n)) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _aggregate(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    wins = sum(int(r.get("hit") or 0) for r in rows)
    losses = n - wins
    if n <= 0:
        return {
            "selections": 0, "wins": 0, "losses": 0, "hit_rate_pct": None,
            "mean_implied_probability_pct": None, "hit_minus_implied_pp": None,
            "flat_stake_roi_pct": None, "avg_odds": None, "median_odds": None,
            "avg_ab_clv_pct": None, "ab_clv_samples": 0,
            "wilson_low_pct": None, "wilson_high_pct": None,
        }
    odds = [float(r["offered_odds"]) for r in rows]
    ordered = sorted(odds)
    mid = n // 2
    median = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0
    implied = sum(1.0 / x for x in odds) / n
    flat_pnl = sum((float(r["offered_odds"]) - 1.0) if int(r.get("hit") or 0) else -1.0 for r in rows)
    ab_clv = []
    for r in rows:
        if str(r.get("clv_quality") or "").upper() not in {"A", "B"}:
            continue
        try:
            ab_clv.append(float(r.get("clv_pct")))
        except Exception:
            pass
    lo, hi = _wilson_interval(wins, n)
    hit_rate = wins / n
    return {
        "selections": n,
        "wins": wins,
        "losses": losses,
        "hit_rate_pct": hit_rate * 100.0,
        "mean_implied_probability_pct": implied * 100.0,
        "hit_minus_implied_pp": (hit_rate - implied) * 100.0,
        "flat_stake_roi_pct": (flat_pnl / n) * 100.0,
        "avg_odds": sum(odds) / n,
        "median_odds": median,
        "avg_ab_clv_pct": (sum(ab_clv) / len(ab_clv)) if ab_clv else None,
        "ab_clv_samples": len(ab_clv),
        "wilson_low_pct": None if lo is None else lo * 100.0,
        "wilson_high_pct": None if hi is None else hi * 100.0,
    }


def outcome_edge_report(db: Database) -> Dict[str, Any]:
    unique = unique_selection_rows(db)
    overall = _aggregate(unique)

    bands = []
    for label, _, _ in ODDS_BANDS:
        subset = [r for r in unique if odds_band(float(r["offered_odds"])) == label]
        if not subset:
            continue
        row = {"odds_band": label}
        row.update(_aggregate(subset))
        bands.append(row)

    markets = []
    for market in sorted({str(r.get("market_key") or "") for r in unique if r.get("market_key")}):
        subset = [r for r in unique if str(r.get("market_key")) == market]
        row = {"market_key": market}
        row.update(_aggregate(subset))
        markets.append(row)

    source_rows = _source_rows(db)
    sources = []
    for source in sorted({str(r.get("source") or "") for r in source_rows}):
        subset = [r for r in source_rows if str(r.get("source")) == source]
        row = {"source": source}
        row.update(_aggregate(subset))
        sources.append(row)

    focus = [r for r in unique if 4.0 <= float(r["offered_odds"]) < 7.5]
    return {
        "definition": "Unique settled event/market/selection/point; best stored executable entry price retained across CONSENSUS/PRED1/PRED2/PRED3/PRED4 duplicates.",
        "overall": overall,
        "focus_4_to_7_49": _aggregate(focus),
        "odds_bands": bands,
        "markets": markets,
        "sources_raw_not_deduped": sources,
        "frozen_watch_cohorts": watch_cohort_report(db),
    }
