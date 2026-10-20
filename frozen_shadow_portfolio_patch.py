from __future__ import annotations

"""Frozen, no-money singles portfolio for genuine forward evaluation.

The component research cohorts keep their original freeze timestamps.  This
module adds one common portfolio start boundary and fixed reference stakes so
future results can be read as if the qualifying bets had actually been placed.
It never submits bookmaker orders.
"""

import json
from typing import Any, Dict, Mapping

import outcome_edge as oe
from db import utc_now_iso

VERSION = "FROZEN_SINGLES_PORTFOLIO_2026_10_20_V1"
POLICY_KEY = "SINGLES_SHADOW_V1"
_INSTALLED = False
_ORIGINAL_WATCH_ROWS = None
_ORIGINAL_ENSURE = None

# priority is used only to deduplicate the same underlying selection across
# multiple qualifying lanes. Lower number wins; if two lanes identify the same
# selection, the earliest source-created observation wins first, then priority.
POLICY = (
    {"cohort_key": "PRED4_ALL", "label": "PRED4 all qualified", "tier": "CORE", "stake_gbp": 10.0, "priority": 10, "included": True},
    {"cohort_key": "PRED12_BTTS", "label": "PRED1/2 BTTS", "tier": "CORE", "stake_gbp": 10.0, "priority": 20, "included": True},
    {"cohort_key": "NCAAF_3_TO_4_99", "label": "NCAAF H2H 3.00-4.99", "tier": "CHALLENGER", "stake_gbp": 5.0, "priority": 30, "included": True},
    {"cohort_key": "EUROLEAGUE_1_50_1_99", "label": "EuroLeague H2H 1.50-1.99", "tier": "CHALLENGER", "stake_gbp": 5.0, "priority": 40, "included": True},
    {"cohort_key": "EUROLEAGUE_3_TO_3_99", "label": "EuroLeague H2H 3.00-3.99", "tier": "CHALLENGER", "stake_gbp": 5.0, "priority": 50, "included": True},
    {"cohort_key": "NFL_2_TO_2_99", "label": "NFL H2H 2.00-2.99", "tier": "CHALLENGER", "stake_gbp": 5.0, "priority": 60, "included": True},
    # Subsets are tracked but never double-staked on top of their parent lane.
    {"cohort_key": "PRED4_STRONG_2_TO_3_99", "label": "PRED4 strong 2.00-3.99", "tier": "DIAGNOSTIC", "stake_gbp": 0.0, "priority": 100, "included": False},
    {"cohort_key": "PRED12_BTTS_NO_2_50_2_99", "label": "PRED1/2 BTTS No 2.50-2.99", "tier": "DIAGNOSTIC", "stake_gbp": 0.0, "priority": 110, "included": False},
    # Retained for historical comparison only after its failed forward test.
    {"cohort_key": "ODDS_4_TO_4_99", "label": "Broad odds 4.00-4.99", "tier": "RETIRED", "stake_gbp": 0.0, "priority": 999, "included": False},
)


def _point_key(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.4f}"
    except Exception:
        return str(value)


def _selection_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("event_id") or ""),
        str(row.get("market_key") or ""),
        str(row.get("selection") or ""),
        _point_key(row.get("point")),
    )


def _pred4_all(db) -> list[dict[str, Any]]:
    rows = [dict(r) for r in oe._source_rows(db) if str(r.get("source") or "") == "PRED4"]
    return oe._dedupe_rows(rows)


def _watch_rows(db, cohort_key: str):
    if cohort_key == "PRED4_ALL":
        return _pred4_all(db)
    return _ORIGINAL_WATCH_ROWS(db, cohort_key)


def _ensure_watch(db):
    rows = _ORIGINAL_ENSURE(db)
    existing = db.fetchone(
        "SELECT cohort_key FROM outcome_edge_watch_cohorts WHERE cohort_key='PRED4_ALL'"
    )
    if not existing:
        stamp = utc_now_iso()
        db.execute(
            """INSERT INTO outcome_edge_watch_cohorts(
                cohort_key,label,definition,frozen_at,created_at
            ) VALUES(?,?,?,?,?)""",
            (
                "PRED4_ALL",
                "PRED4 all qualified",
                "All settled PRED4 H2H/BTTS/totals selections, deduplicated by event/market/selection/point; frozen for forward-only portfolio evaluation.",
                stamp,
                stamp,
            ),
        )
    return db.fetchall("SELECT * FROM outcome_edge_watch_cohorts ORDER BY cohort_key")


def _ensure_policy_schema(db) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS frozen_shadow_portfolio_state(
            policy_key TEXT PRIMARY KEY,
            activated_at TEXT NOT NULL,
            version TEXT NOT NULL,
            policy_json TEXT NOT NULL,
            betting_authority INTEGER NOT NULL DEFAULT 0
        )"""
    )


def _activate_policy(db) -> dict[str, Any]:
    _ensure_policy_schema(db)
    row = db.fetchone(
        "SELECT * FROM frozen_shadow_portfolio_state WHERE policy_key=?",
        (POLICY_KEY,),
    )
    if not row:
        stamp = utc_now_iso()
        db.execute(
            """INSERT INTO frozen_shadow_portfolio_state(
                policy_key,activated_at,version,policy_json,betting_authority
            ) VALUES(?,?,?,?,0)""",
            (POLICY_KEY, stamp, VERSION, json.dumps(POLICY, sort_keys=True)),
        )
        row = db.fetchone(
            "SELECT * FROM frozen_shadow_portfolio_state WHERE policy_key=?",
            (POLICY_KEY,),
        )
    return dict(row or {})


def _after_common_start(row: Mapping[str, Any], start: str, lane_freeze: str) -> bool:
    created = str(row.get("created_at") or "")
    boundary = max(str(start or ""), str(lane_freeze or ""))
    return bool(created and created >= boundary)


def _flat_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return oe._aggregate(rows)


def scoreboard(db) -> Dict[str, Any]:
    state = _activate_policy(db)
    start = str(state.get("activated_at") or "")
    registry = {
        str(r["cohort_key"]): dict(r)
        for r in db.fetchall("SELECT * FROM outcome_edge_watch_cohorts")
    }

    lane_rows: dict[str, list[dict[str, Any]]] = {}
    lanes = []
    for rule in POLICY:
        key = str(rule["cohort_key"])
        meta = registry.get(key) or {}
        frozen_at = str(meta.get("frozen_at") or start)
        try:
            all_rows = [dict(r) for r in oe._watch_cohort_rows(db, key)]
        except Exception:
            all_rows = []
        forward = [r for r in all_rows if _after_common_start(r, start, frozen_at)]
        lane_rows[key] = forward
        metrics = _flat_metrics(forward)
        n = int(metrics.get("selections") or 0)
        stake = float(rule["stake_gbp"])
        lanes.append(
            {
                **rule,
                "lane_frozen_at": frozen_at,
                "portfolio_sample_start": max(start, frozen_at),
                "settled": n,
                "wins": int(metrics.get("wins") or 0),
                "hit_rate_pct": metrics.get("hit_rate_pct"),
                "roi_pct": metrics.get("flat_stake_roi_pct"),
                "avg_ab_clv_pct": metrics.get("avg_ab_clv_pct"),
                "reference_pnl_gbp": float(metrics.get("pnl_units") or 0.0) * stake,
                "checkpoint": "PROMOTION_REVIEW" if n >= 100 else "INTERIM_REVIEW" if n >= 50 else "ACCUMULATING",
                "review_at_n": 50,
                "promotion_decision_at_n": 100,
            }
        )

    # Reconstruct the whole portfolio without double-counting overlapping lanes.
    candidates = []
    by_key = {str(x["cohort_key"]): x for x in POLICY}
    for key, rows in lane_rows.items():
        rule = by_key[key]
        if not bool(rule["included"]) or float(rule["stake_gbp"]) <= 0:
            continue
        for row in rows:
            candidates.append((str(row.get("created_at") or ""), int(rule["priority"]), key, dict(row)))
    candidates.sort(key=lambda x: (x[0], x[1], _selection_key(x[3])))

    chosen: dict[tuple[str, str, str, str], tuple[str, dict[str, Any]]] = {}
    for _, _, lane, row in candidates:
        chosen.setdefault(_selection_key(row), (lane, row))

    turnover = pnl = 0.0
    wins = losses = 0
    attribution: dict[str, dict[str, float]] = {}
    for lane, row in chosen.values():
        rule = by_key[lane]
        stake = float(rule["stake_gbp"])
        try:
            odds = float(row.get("offered_odds") or 0.0)
        except Exception:
            continue
        hit = oe._result_hit(row.get("result"))
        if hit is None or odds <= 1:
            continue
        bet_pnl = stake * (odds - 1.0) if hit else -stake
        turnover += stake
        pnl += bet_pnl
        wins += int(hit)
        losses += int(not hit)
        bucket = attribution.setdefault(lane, {"bets": 0.0, "turnover_gbp": 0.0, "pnl_gbp": 0.0})
        bucket["bets"] += 1
        bucket["turnover_gbp"] += stake
        bucket["pnl_gbp"] += bet_pnl

    return {
        "version": VERSION,
        "policy_key": POLICY_KEY,
        "portfolio_started_at": start,
        "rules_frozen": True,
        "betting_authority": False,
        "reference_staking": "flat £10 core / £5 challengers; diagnostic subsets £0; duplicate selection staked once",
        "settled_unique_bets": wins + losses,
        "wins": wins,
        "losses": losses,
        "turnover_gbp": turnover,
        "pnl_gbp": pnl,
        "roi_pct": (pnl / turnover * 100.0) if turnover > 0 else None,
        "lane_attribution": attribution,
        "lanes": lanes,
        "governance": {
            "no_rule_changes_from_results": True,
            "interim_review_n": 50,
            "promotion_decision_n": 100,
            "real_money_enabled": False,
        },
    }


def install(app, db, web_module=None) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_WATCH_ROWS, _ORIGINAL_ENSURE
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    _ORIGINAL_WATCH_ROWS = oe._watch_cohort_rows
    _ORIGINAL_ENSURE = oe.ensure_watch_cohorts
    oe._watch_cohort_rows = _watch_rows
    oe.ensure_watch_cohorts = _ensure_watch
    if web_module is not None:
        web_module.ensure_watch_cohorts = _ensure_watch

    _ensure_watch(db)
    state = _activate_policy(db)

    @app.get("/api/frozen-shadow-portfolio")
    def frozen_shadow_portfolio_api():
        return scoreboard(db)

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "portfolio_started_at": state.get("activated_at"),
        "core": [x["cohort_key"] for x in POLICY if x["tier"] == "CORE"],
        "challengers": [x["cohort_key"] for x in POLICY if x["tier"] == "CHALLENGER"],
        "diagnostics": [x["cohort_key"] for x in POLICY if x["tier"] == "DIAGNOSTIC"],
        "retired": [x["cohort_key"] for x in POLICY if x["tier"] == "RETIRED"],
        "betting_authority": False,
    }
