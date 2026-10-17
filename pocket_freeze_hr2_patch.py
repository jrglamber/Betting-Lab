from __future__ import annotations

"""Freeze the 17 Oct cross-sport/PRED audit pockets and extend HR2 safely.

This patch is intentionally research/multiples-only:
- freezes named watch cohorts at first startup after deployment;
- preserves all historical discovery data separately from forward results;
- leaves HR1 unchanged;
- bumps HR2 to a new policy version so old candidates/cards stay immutable;
- adds only the two audited pockets that were absent from Live Yankee/HR1/HR2;
- makes zero provider calls and has no singles/execution authority.
"""

from copy import deepcopy
from typing import Any, Dict, Mapping, Optional, Sequence

import outcome_edge as oe
import hit_rate_multiples_lab as hr
import web as web_module
from db import utc_now_iso

VERSION = "POCKET_FREEZE_HR2_2026_10_17_V1"
HR2_POLICY_VERSION = "HRM_2026-10-17_V2"
_INSTALLED = False
_ORIGINAL_WATCH_ROWS = None
_ORIGINAL_ENSURE_WATCH = None
_ORIGINAL_HR_QUALIFY = None
_ORIGINAL_HR_SOURCE_ROWS = None

# Numeric boundaries are deliberately literal/frozen. Future config changes must
# not silently alter the definition of these forward cohorts.
NEW_COHORTS = (
    (
        "PRED4_STRONG_2_TO_3_99",
        "PRED4 strong 2.00–3.99",
        "PRED4 H2H/derived-market settled selections with executable entry odds >=2.00 and <4.00 and frozen edge_pct >=5.0%.",
    ),
    (
        "PRED4_H2H_2_TO_3_99",
        "PRED4 H2H 2.00–3.99",
        "PRED4 H2H settled selections with executable entry odds >=2.00 and <4.00.",
    ),
    (
        "PRED12_BTTS_NO_2_50_2_99",
        "PRED1/2 BTTS No 2.50–2.99",
        "Unique PRED1/PRED2 BTTS No settled selections with executable entry odds >=2.50 and <3.00; duplicate underlying selections are collapsed.",
    ),
    (
        "NCAAF_3_TO_4_99",
        "NCAAF H2H 3.00–4.99",
        "Multi-Sport NCAAF-family H2H settled selections with executable entry odds >=3.00 and <5.00.",
    ),
    (
        "NFL_2_TO_2_99",
        "NFL H2H 2.00–2.99",
        "Multi-Sport NFL-family H2H settled selections with executable entry odds >=2.00 and <3.00.",
    ),
    (
        "EUROLEAGUE_1_50_1_99",
        "EuroLeague H2H 1.50–1.99",
        "EuroLeague H2H settled selections with executable entry odds >=1.50 and <2.00.",
    ),
    (
        "EUROLEAGUE_3_TO_3_99",
        "EuroLeague H2H 3.00–3.99",
        "EuroLeague H2H settled selections with executable entry odds >=3.00 and <4.00.",
    ),
    (
        "MLB_1_50_1_99_WATCH",
        "MLB H2H 1.50–1.99 watch",
        "MLB regular-season H2H settled selections with executable entry odds >=1.50 and <2.00; watch-tier only because discovery gains were recent-heavy.",
    ),
)


def _settled_rows(db, sql: str, params: Sequence[Any] = (), *, source: str = "") -> list[dict[str, Any]]:
    try:
        rows = db.fetchall(sql, tuple(params))
    except Exception:
        return []
    out = []
    for raw in rows:
        hit = oe._result_hit(raw.get("result"))
        if hit is None:
            continue
        item = dict(raw)
        item["hit"] = hit
        if source:
            item["source"] = source
        out.append(item)
    return out


def _pred4_rows(db, *, h2h_only: bool, strong_only: bool) -> list[dict[str, Any]]:
    parts = []
    parts.extend(_settled_rows(
        db,
        """SELECT event_id,'h2h' AS market_key,selection,NULL AS point,
                  offered_odds,result,clv_pct,clv_quality,net_pnl_units,created_at,edge_pct
           FROM football_predictive4_bets
           WHERE result IS NOT NULL AND offered_odds>=2.0 AND offered_odds<4.0""",
        source="PRED4",
    ))
    if not h2h_only:
        parts.extend(_settled_rows(
            db,
            """SELECT event_id,market_key,selection,point,
                      offered_odds,result,clv_pct,clv_quality,net_pnl_units,created_at,edge_pct
               FROM football_predictive4_market_bets
               WHERE result IS NOT NULL AND offered_odds>=2.0 AND offered_odds<4.0""",
            source="PRED4",
        ))
    if strong_only:
        filtered = []
        for row in parts:
            try:
                if float(row.get("edge_pct") or 0.0) >= 5.0:
                    filtered.append(row)
            except Exception:
                continue
        parts = filtered
    return oe._dedupe_rows(parts)


def _pred12_btts_no(db) -> list[dict[str, Any]]:
    rows = []
    for table, source in (
        ("football_predictive_market_bets", "PRED1"),
        ("football_predictive2_market_bets", "PRED2"),
    ):
        rows.extend(_settled_rows(
            db,
            f"""SELECT event_id,market_key,selection,point,
                       offered_odds,result,clv_pct,clv_quality,net_pnl_units,created_at
                FROM {table}
                WHERE result IS NOT NULL AND market_key='btts'
                  AND offered_odds>=2.5 AND offered_odds<3.0""",
            source=source,
        ))
    rows = [r for r in rows if str(r.get("selection") or "").strip().lower() == "no"]
    return oe._dedupe_rows(rows)


def _multisport_rows(db, cohort_key: str) -> list[dict[str, Any]]:
    rows = _settled_rows(
        db,
        """SELECT b.event_id,'h2h' AS market_key,b.selection,NULL AS point,
                  b.offered_odds,b.result,b.clv_pct,b.clv_quality,b.net_pnl_units,b.created_at,
                  e.sport_key
           FROM multisport_execution_bets b
           JOIN multisport_events e ON e.event_id=b.event_id
           WHERE b.result IS NOT NULL AND b.offered_odds>1.0""",
        source="MULTISPORT",
    )
    out = []
    for row in rows:
        sport = str(row.get("sport_key") or "").lower()
        try:
            odds = float(row.get("offered_odds") or 0.0)
        except Exception:
            continue
        keep = False
        if cohort_key == "NCAAF_3_TO_4_99":
            keep = "ncaaf" in sport and 3.0 <= odds < 5.0
        elif cohort_key == "NFL_2_TO_2_99":
            keep = "nfl" in sport and 2.0 <= odds < 3.0
        elif cohort_key == "EUROLEAGUE_1_50_1_99":
            keep = sport == "basketball_euroleague" and 1.5 <= odds < 2.0
        elif cohort_key == "EUROLEAGUE_3_TO_3_99":
            keep = sport == "basketball_euroleague" and 3.0 <= odds < 4.0
        elif cohort_key == "MLB_1_50_1_99_WATCH":
            keep = sport == "baseball_mlb" and 1.5 <= odds < 2.0
        if keep:
            out.append(row)
    return oe._dedupe_rows(out)


def _watch_rows(db, cohort_key: str) -> list[dict[str, Any]]:
    if cohort_key == "PRED4_STRONG_2_TO_3_99":
        return _pred4_rows(db, h2h_only=False, strong_only=True)
    if cohort_key == "PRED4_H2H_2_TO_3_99":
        return _pred4_rows(db, h2h_only=True, strong_only=False)
    if cohort_key == "PRED12_BTTS_NO_2_50_2_99":
        return _pred12_btts_no(db)
    if cohort_key in {
        "NCAAF_3_TO_4_99", "NFL_2_TO_2_99",
        "EUROLEAGUE_1_50_1_99", "EUROLEAGUE_3_TO_3_99",
        "MLB_1_50_1_99_WATCH",
    }:
        return _multisport_rows(db, cohort_key)
    return _ORIGINAL_WATCH_ROWS(db, cohort_key)


def _ensure_watch(db):
    # Preserve every existing cohort/frozen_at, then freeze only definitions that
    # do not yet exist. Re-deploying cannot move a cohort's forward-test start.
    rows = _ORIGINAL_ENSURE_WATCH(db)
    stamp = utc_now_iso()
    created = []
    for key, label, definition in NEW_COHORTS:
        existing = db.fetchone(
            "SELECT cohort_key,frozen_at FROM outcome_edge_watch_cohorts WHERE cohort_key=?",
            (key,),
        )
        if existing:
            continue
        db.execute(
            "INSERT INTO outcome_edge_watch_cohorts(cohort_key,label,definition,frozen_at,created_at) VALUES(?,?,?,?,?)",
            (key, label, definition, stamp, stamp),
        )
        created.append(key)
    registry = db.fetchall("SELECT * FROM outcome_edge_watch_cohorts ORDER BY cohort_key")
    frozen = {
        str(row.get("cohort_key")): row.get("frozen_at")
        for row in registry
        if str(row.get("cohort_key")) in {x[0] for x in NEW_COHORTS}
    }
    print(
        f"AUDITED_POCKET_FREEZE {{'created': {created}, 'frozen': {frozen}, 'version': '{VERSION}'}}",
        flush=True,
    )
    return registry


def _hr_qualify(kind: str, row: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    out = dict(_ORIGINAL_HR_QUALIFY(kind, row))
    try:
        p = float(row.get("source_odds") or 0.0)
    except Exception:
        p = 0.0
    market = str(row.get("market_key") or "").lower()
    selection = str(row.get("selection") or "").strip().lower()
    sport = str(row.get("sport_key") or "").lower()

    # HR1 remains frozen exactly as designed on 12 Oct. These are HR2-only
    # additions because both are higher-price/value pockets with less mature
    # evidence than the strict high-hit pool.
    if kind in {"PRED1", "PRED2"}:
        if market == "btts" and selection == "no" and 2.5 <= p < 3.0:
            out["HR2"] = {
                "lane": "PRED12_BTTS_NO_2_50_2_99",
                "lo": 2.5,
                "hi": 3.0,
            }
    elif kind == "MULTISPORT":
        if sport == "basketball_euroleague" and 3.0 <= p < 4.0:
            out["HR2"] = {
                "lane": "EUROLEAGUE_H2H_3_00_3_99",
                "lo": 3.0,
                "hi": 4.0,
            }
    return out


def _predictive_hr2_rows(db, now) -> list[dict[str, Any]]:
    out = []
    for kind, table in (
        ("PRED1", "football_predictive_market_bets"),
        ("PRED2", "football_predictive2_market_bets"),
    ):
        try:
            rows = db.fetchall(
                f"""SELECT b.id AS source_id,b.created_at AS source_created_at,b.event_id,
                           e.sport_key,e.league,e.commence_time,e.home_team,e.away_team,
                           b.market_key,b.selection,b.point,b.min_odds,
                           b.offered_odds AS source_odds
                    FROM {table} b JOIN events e ON e.event_id=b.event_id
                    WHERE b.status='OPEN' AND e.status='UPCOMING'
                    ORDER BY b.created_at,b.id"""
            )
        except Exception:
            rows = []
        for raw in rows:
            row = dict(raw)
            if not hr._actionable(row.get("commence_time"), now):
                continue
            try:
                if float(row.get("source_odds") or 0) <= 1 or float(row.get("min_odds") or 0) <= 1:
                    continue
            except Exception:
                continue
            for exp, meta in _hr_qualify(kind, row).items():
                if exp != "HR2":
                    continue
                item = dict(row)
                item.update(
                    experiment_key="HR2",
                    source_lane=meta["lane"],
                    source_table=table,
                    band_min=float(meta["lo"]),
                    band_max=float(meta["hi"]),
                )
                out.append(item)
    return out


def _hr_source_rows(db, now) -> list[dict[str, Any]]:
    # The original collector now calls the patched qualifier, so EuroLeague
    # 3.00-3.99 is picked up from the existing Multi-Sport stream automatically.
    rows = list(_ORIGINAL_HR_SOURCE_ROWS(db, now))
    rows.extend(_predictive_hr2_rows(db, now))
    return sorted(rows, key=lambda r: (
        str(r.get("source_created_at") or ""),
        str(r.get("experiment_key") or ""),
        str(r.get("source_lane") or ""),
        int(r.get("source_id") or 0),
    ))


def install(db) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_WATCH_ROWS, _ORIGINAL_ENSURE_WATCH
    global _ORIGINAL_HR_QUALIFY, _ORIGINAL_HR_SOURCE_ROWS

    if _INSTALLED:
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
            "hr2_policy_version": hr.POLICY_VERSION,
        }

    _ORIGINAL_WATCH_ROWS = oe._watch_cohort_rows
    _ORIGINAL_ENSURE_WATCH = oe.ensure_watch_cohorts
    oe._watch_cohort_rows = _watch_rows
    oe.ensure_watch_cohorts = _ensure_watch
    # web.py imported ensure_watch_cohorts by value before this runtime patch.
    # Replace that bound global so the lifespan freeze uses the extended registry.
    web_module.ensure_watch_cohorts = _ensure_watch

    _ORIGINAL_HR_QUALIFY = hr._qualify
    _ORIGINAL_HR_SOURCE_ROWS = hr._source_rows
    hr._qualify = _hr_qualify
    hr._source_rows = _hr_source_rows

    policy = deepcopy(hr.POLICY)
    hr1_pockets = list(policy.get("HR1", {}).get("pockets") or [])
    hr2_pockets = list(policy.get("HR2", {}).get("pockets") or [])
    for label in (
        "PRED1/PRED2 BTTS No 2.50-2.99",
        "EuroLeague H2H 3.00-3.99",
    ):
        if label not in hr2_pockets:
            hr2_pockets.append(label)
    policy.setdefault("HR1", {})["pockets"] = hr1_pockets
    policy.setdefault("HR2", {})["pockets"] = hr2_pockets
    hr.POLICY = policy
    hr.POLICY_VERSION = HR2_POLICY_VERSION

    # Keep the original 12-Oct design evidence intact and append transparent
    # provenance for the V2 additions. These discovery figures are never reused
    # as forward evidence by the runtime.
    design = deepcopy(hr.DESIGN_BACKTEST)
    design["v2_policy_update"] = {
        "frozen_on": "2026-10-17",
        "source": "full cross-sport + PRED1-6 audit",
        "hr1_changed": False,
        "new_hr2_pockets": {
            "PRED1/PRED2 BTTS No 2.50-2.99": {
                "pred1_settled": 22,
                "pred1_roi_pct": 81.6,
                "pred2_settled": 14,
                "pred2_roi_pct": 113.0,
                "caution": "PRED1/PRED2 samples overlap; forward confirmation required",
            },
            "EuroLeague H2H 3.00-3.99": {
                "settled": 16,
                "roi_pct": 50.8,
                "caution": "small sample and raw CLV outliers; HR2 only",
            },
        },
    }
    hr.DESIGN_BACKTEST = design

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "frozen_cohorts": [x[0] for x in NEW_COHORTS],
        "freeze_occurs": "web_lifespan_after_db_init",
        "hr1_changed": False,
        "hr2_policy_version": HR2_POLICY_VERSION,
        "hr2_additions": [
            "PRED1/PRED2 BTTS No 2.50-2.99",
            "EuroLeague H2H 3.00-3.99",
        ],
        "already_covered_elsewhere": {
            "live_yankee": ["PRED4", "NCAAF 3.00-4.99", "NFL"],
            "hr1_hr2": ["EuroLeague 1.50-1.99", "NFL 2.00-2.99", "MLB 1.50-1.99"],
        },
        "provider_credits": 0,
        "singles_execution_authority": False,
    }
