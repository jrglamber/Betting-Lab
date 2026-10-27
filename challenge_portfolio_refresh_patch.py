from __future__ import annotations

"""Refresh the lightweight £10→£500 challenge export after the 27-Oct audit.

This is export/presentation logic only.  It does not change any model, candidate
creation rule, bookmaker polling, staking authority, alerting or execution path.

The existing /export/challenge.zip route is deliberately retained.  That route
resolves challenge_export_patch.build_challenge_export at request time, so this
patch can wrap the builder after the route has already been installed by the
responsive dashboard bootstrap.
"""

import csv
import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

import challenge_export_patch as base

VERSION = "CHALLENGE_EXPORT_V3_10_TO_500_2026_10_27"
REVIEWED_AT = "2026-10-27T09:15:00+00:00"
FROZEN_BOUNDARY = "2026-10-20T06:44:17+01:00"
_INSTALLED = False
_ORIGINAL_BUILD = None
_RELEASE_VERSION = None


PORTFOLIO_ROWS = [
    {
        "lane_key": "PRED4_ALL",
        "label": "PRED4 broad",
        "status": "CORE_REDUCED_CONFIDENCE",
        "stake_gbp": 10.0,
        "rule": "All currently qualified PRED4 H2H/derived-market selections; duplicate underlying selections are staked once.",
        "rationale": "Keep the frozen broad lane. Overall P&L/CLV remain positive, but the forward margin has weakened, so confidence is reduced rather than the lane being retrospectively narrowed.",
    },
    {
        "lane_key": "PRED12_BTTS",
        "label": "PRED1/PRED2 BTTS",
        "status": "WATCH",
        "stake_gbp": 0.0,
        "rule": "PRED1/PRED2 BTTS selections.",
        "rationale": "Downgraded from £10 Core after materially negative post-freeze ROI and negative CLV. Keep collecting; do not stake from the challenge bankroll.",
    },
    {
        "lane_key": "NCAAF_3_TO_4_99",
        "label": "NCAAF H2H 3.00–4.99",
        "status": "WATCH_PNL_CLV_CONFLICT",
        "stake_gbp": 0.0,
        "rule": "Multi-Sport NCAAF-family H2H with executable odds >=3.00 and <5.00.",
        "rationale": "Outcome profit remains interesting but forward CLV is materially negative. Downgraded from £5 Challenger pending resolution of the P&L/CLV conflict.",
    },
    {
        "lane_key": "NFL_2_TO_2_99",
        "label": "NFL H2H 2.00–2.99",
        "status": "CHALLENGER",
        "stake_gbp": 5.0,
        "rule": "Multi-Sport NFL-family H2H with executable odds >=2.00 and <3.00.",
        "rationale": "Retained as the cleanest existing challenger: positive outcome evidence and positive CLV, with the original frozen boundary preserved.",
    },
    {
        "lane_key": "EUROLEAGUE_1_50_1_99",
        "label": "EuroLeague H2H 1.50–1.99",
        "status": "CHALLENGER",
        "stake_gbp": 5.0,
        "rule": "EuroLeague H2H with executable odds >=1.50 and <2.00.",
        "rationale": "Retained at £5. Evidence is still small, so no promotion or expansion.",
    },
    {
        "lane_key": "EUROLEAGUE_3_TO_3_99",
        "label": "EuroLeague H2H 3.00–3.99",
        "status": "CHALLENGER",
        "stake_gbp": 5.0,
        "rule": "EuroLeague H2H with executable odds >=3.00 and <4.00.",
        "rationale": "Retained at £5. Evidence is still small/outlier-sensitive, so no promotion or expansion.",
    },
    {
        "lane_key": "PRED4_STRONG_2_TO_3_99",
        "label": "PRED4 strong 2.00–3.99",
        "status": "DIAGNOSTIC",
        "stake_gbp": 0.0,
        "rule": "PRED4 selections with executable odds >=2.00 and <4.00 and frozen edge >=5%.",
        "rationale": "Original diagnostic freeze retained. Do not double-stake selections already covered by PRED4 broad.",
    },
    {
        "lane_key": "PRED12_BTTS_NO_2_50_2_99",
        "label": "PRED1/2 BTTS No 2.50–2.99",
        "status": "DIAGNOSTIC",
        "stake_gbp": 0.0,
        "rule": "Unique PRED1/PRED2 BTTS No selections with executable odds >=2.50 and <3.00.",
        "rationale": "Diagnostic subset retained only. Broad PRED1/PRED2 BTTS is now Watch/£0.",
    },
    {
        "lane_key": "ODDS_4_TO_4_99",
        "label": "Generic odds 4.00–4.99",
        "status": "RETIRED",
        "stake_gbp": 0.0,
        "rule": "Generic selections priced >=4.00 and <5.00.",
        "rationale": "Retired as a standalone challenge lane. It may still appear as a diagnostic tag on selections belonging to another retained lane.",
    },
    {
        "lane_key": "CLV_FIRST_GT0",
        "label": "CLV-First predicted CLV > 0",
        "status": "FILTER_ONLY",
        "stake_gbp": 0.0,
        "rule": "Expose predicted_clv_pct and a >0 research filter for challenge candidates where a forward CLV-First sample exists.",
        "rationale": "Forward separation is encouraging, but the filter is not yet betting authority and must not alter the frozen stake allocation automatically.",
    },
    {
        "lane_key": "FHG1_GE70",
        "label": "First-half goal >=70%",
        "status": "WATCH_HIT_RATE_ONLY",
        "stake_gbp": 0.0,
        "rule": "FHG1 candidates with frozen estimated hit probability >=70%.",
        "rationale": "Early hit-rate evidence is strong, but entry-price capture has not yet produced enough executable pricing to establish ROI/EV.",
    },
    {
        "lane_key": "NCAAF_PRED1",
        "label": "NCAAF-PRED1",
        "status": "WATCH",
        "stake_gbp": 0.0,
        "rule": "API-Sports NCAAF-PRED1 price-linked shadow bets.",
        "rationale": "The repaired linker is now producing bets, but the settled sample remains small and tail-heavy. Track separately from normal Multi-Sport NCAAF.",
    },
    {
        "lane_key": "LIVE_YANKEE_TOLERANCE_V2",
        "label": "Live Yankee price tolerance V2",
        "status": "RESEARCH_ONLY",
        "stake_gbp": 0.0,
        "rule": "P100/P98/P96/P94/P90/P85 matched-variant research.",
        "rationale": "Continue unchanged. Tolerance differentiation has begun, but no matched four-leg same-book V2 card evidence exists yet.",
    },
]


def _number(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _read_csv(data: bytes) -> list[dict[str, str]]:
    if not data:
        return []
    text = data.decode("utf-8-sig", errors="replace")
    return [dict(row) for row in csv.DictReader(io.StringIO(text))]


def _write_csv(rows: list[Mapping[str, Any]]) -> bytes:
    if not rows:
        return b""
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            key = str(key)
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: "" if row.get(k) is None else row.get(k) for k in fieldnames})
    return out.getvalue().encode("utf-8")


def _clv_first_index(db) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        rows = db.fetchall(
            """SELECT id,source_table,source_bet_id,predicted_clv_pct,
                      predicted_positive_prob,confidence,decision,threshold_bucket,
                      evidence_mode,created_at,status
               FROM clv_first_samples
               WHERE evidence_mode='FORWARD'
               ORDER BY id"""
        )
    except Exception:
        return index
    for raw in rows:
        row = dict(raw)
        table = str(row.get("source_table") or "")
        source_id = str(row.get("source_bet_id") or "")
        if table and source_id:
            index[(table, source_id)] = row
    return index


def _is_ncaaf(row: Mapping[str, Any]) -> bool:
    text = " ".join(
        str(row.get(k) or "") for k in ("sport_key", "league", "source", "notes")
    ).lower()
    return "ncaaf" in text or "college" in text and "football" in text


def _is_nfl(row: Mapping[str, Any]) -> bool:
    text = " ".join(
        str(row.get(k) or "") for k in ("sport_key", "league", "source", "notes")
    ).lower()
    return "nfl" in text


def _is_euroleague(row: Mapping[str, Any]) -> bool:
    text = " ".join(
        str(row.get(k) or "") for k in ("sport_key", "league", "source", "notes")
    ).lower()
    return "euroleague" in text


def _portfolio_match(row: Mapping[str, Any]) -> tuple[str, str, float, str]:
    source = str(row.get("source") or "")
    market = str(row.get("market_key") or "").lower()
    selection = str(row.get("selection") or "").lower()
    odds = _number(row.get("offered_odds"))
    fair_p = _number(row.get("fair_probability"))

    if source in {"PRED4_H2H", "PRED4_MARKET"}:
        return (
            "PRED4_ALL",
            "CORE_REDUCED_CONFIDENCE",
            10.0,
            "Broad PRED4 retained; confidence reduced after forward weakening.",
        )

    if source in {"PRED1_MARKET", "PRED2_MARKET"} and market == "btts":
        return (
            "PRED12_BTTS",
            "WATCH",
            0.0,
            "PRED1/PRED2 BTTS downgraded from Core after negative post-freeze outcome/CLV evidence.",
        )

    if source == "MULTISPORT_H2H" and odds is not None and _is_ncaaf(row) and 3.0 <= odds < 5.0:
        return (
            "NCAAF_3_TO_4_99",
            "WATCH_PNL_CLV_CONFLICT",
            0.0,
            "Normal Multi-Sport NCAAF 3.00–4.99 downgraded while forward CLV remains materially negative.",
        )

    if source == "MULTISPORT_H2H" and odds is not None and _is_nfl(row) and 2.0 <= odds < 3.0:
        return (
            "NFL_2_TO_2_99",
            "CHALLENGER",
            5.0,
            "NFL 2.00–2.99 remains the cleanest £5 challenger pocket.",
        )

    if source == "MULTISPORT_H2H" and odds is not None and _is_euroleague(row) and 1.5 <= odds < 2.0:
        return (
            "EUROLEAGUE_1_50_1_99",
            "CHALLENGER",
            5.0,
            "EuroLeague 1.50–1.99 retained at £5; sample remains small.",
        )

    if source == "MULTISPORT_H2H" and odds is not None and _is_euroleague(row) and 3.0 <= odds < 4.0:
        return (
            "EUROLEAGUE_3_TO_3_99",
            "CHALLENGER",
            5.0,
            "EuroLeague 3.00–3.99 retained at £5; sample remains small/outlier-sensitive.",
        )

    if source == "FHG1" and fair_p is not None and fair_p >= 0.70:
        return (
            "FHG1_GE70",
            "WATCH_HIT_RATE_ONLY",
            0.0,
            "Strong hit-rate evidence, but no established forward price/ROI evidence yet.",
        )

    if source == "NCAAF-PRED1":
        return (
            "NCAAF_PRED1",
            "WATCH",
            0.0,
            "Price-link pipeline is repaired; keep separate from normal NCAAF until the forward sample matures.",
        )

    if source == "LIVE_YANKEE_QUEUE":
        return (
            "LIVE_YANKEE_TOLERANCE_V2",
            "RESEARCH_ONLY",
            0.0,
            "Tolerance experiment continues unchanged; no staking authority in the £10→£500 singles portfolio.",
        )

    return ("", "UNALLOCATED_RESEARCH", 0.0, "Not currently allocated to the frozen £10→£500 portfolio.")


def _diagnostic_tags(row: Mapping[str, Any]) -> str:
    tags: list[str] = []
    source = str(row.get("source") or "")
    market = str(row.get("market_key") or "").lower()
    selection = str(row.get("selection") or "").lower()
    odds = _number(row.get("offered_odds"))
    edge = _number(row.get("edge_pct"))

    if source in {"PRED4_H2H", "PRED4_MARKET"} and odds is not None and 2.0 <= odds < 4.0 and edge is not None and edge >= 5.0:
        tags.append("PRED4_STRONG_2_TO_3_99")
    if source in {"PRED1_MARKET", "PRED2_MARKET"} and market == "btts" and selection == "no" and odds is not None and 2.5 <= odds < 3.0:
        tags.append("PRED12_BTTS_NO_2_50_2_99")
    if odds is not None and 4.0 <= odds < 5.0:
        tags.append("ODDS_4_TO_4_99_RETIRED")
    return ";".join(tags)


def _annotate_candidates(db, rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    clv_index = _clv_first_index(db)
    out: list[dict[str, Any]] = []
    for raw in rows:
        row: dict[str, Any] = dict(raw)
        lane, status, stake, note = _portfolio_match(row)
        source_table = str(row.get("source_table") or "")
        source_id = str(row.get("source_id") or "")
        clv = clv_index.get((source_table, source_id), {})
        predicted_clv = _number(clv.get("predicted_clv_pct"))
        if predicted_clv is None:
            clv_filter = "NO_FORWARD_SAMPLE"
        elif predicted_clv > 0:
            clv_filter = "PASS_GT0_RESEARCH_ONLY"
        else:
            clv_filter = "FAIL_LE0_RESEARCH_ONLY"

        event_id = str(row.get("event_id") or "")
        market = str(row.get("market_key") or "")
        selection = str(row.get("selection") or "")
        point = str(row.get("point") or "")
        row.update({
            "challenge_lane": lane,
            "challenge_status": status,
            "challenge_stake_gbp": stake,
            "challenge_note": note,
            "challenge_reviewed_at": REVIEWED_AT,
            "stake_once_key": "|".join((event_id, market, selection, point)),
            "diagnostic_tags": _diagnostic_tags(row),
            "clv_first_predicted_clv_pct": clv.get("predicted_clv_pct"),
            "clv_first_predicted_positive_prob": clv.get("predicted_positive_prob"),
            "clv_first_confidence": clv.get("confidence"),
            "clv_first_decision": clv.get("decision"),
            "clv_first_threshold_bucket": clv.get("threshold_bucket"),
            "clv_first_filter": clv_filter,
            "clv_first_filter_authority": "RESEARCH_ONLY_DO_NOT_AUTO_CHANGE_STAKE",
        })
        out.append(row)
    return out


def _portfolio_csv() -> bytes:
    rows = []
    for row in PORTFOLIO_ROWS:
        item = dict(row)
        item.update({
            "reviewed_at": REVIEWED_AT,
            "frozen_boundary": FROZEN_BOUNDARY,
            "duplicate_rule": "same underlying event/market/selection/point is counted and staked once",
            "paddy_power_excluded": True,
            "betting_authority": False,
        })
        rows.append(item)
    return _write_csv(rows)


def _portfolio_summary() -> dict[str, Any]:
    return {
        "version": VERSION,
        "reviewed_at": REVIEWED_AT,
        "frozen_boundary": FROZEN_BOUNDARY,
        "purpose": "£10→£500 manual challenge decision support",
        "active_staking_lanes": [
            "PRED4_ALL",
            "NFL_2_TO_2_99",
            "EUROLEAGUE_1_50_1_99",
            "EUROLEAGUE_3_TO_3_99",
        ],
        "watch_lanes": [
            "PRED12_BTTS",
            "NCAAF_3_TO_4_99",
            "FHG1_GE70",
            "NCAAF_PRED1",
        ],
        "research_only": [
            "CLV_FIRST_GT0",
            "LIVE_YANKEE_TOLERANCE_V2",
            "PRED4_STRONG_2_TO_3_99",
            "PRED12_BTTS_NO_2_50_2_99",
        ],
        "retired": ["ODDS_4_TO_4_99"],
        "rules": {
            "duplicate_selections": "stake once across overlapping lanes",
            "bookmaker_exclusion": "Paddy Power excluded",
            "clv_first": "predicted CLV >0 is displayed as a research-only filter and does not automatically change stake",
            "fhg": "watch/hit-rate only until forward executable prices establish ROI/EV",
            "ncaaf_pred1": "track separately from normal Multi-Sport NCAAF",
            "review_trigger": "re-review at materially larger forward samples, including the existing 50/100-settled and ~8-week checkpoints",
        },
        "betting_authority": False,
    }


def _rebuild_zip(files: dict[str, bytes]) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return payload.getvalue()


def build_challenge_export_v3(db, settings, app_version: str):
    # Use the production release passed to install(), not the historical build
    # value captured when dashboard_responsiveness first installed the route.
    release_version = str(_RELEASE_VERSION or app_version)
    raw_zip, filename = _ORIGINAL_BUILD(db, settings, release_version)

    with zipfile.ZipFile(io.BytesIO(raw_zip), "r") as z:
        files = {name: z.read(name) for name in z.namelist()}

    candidates = _read_csv(files.get("challenge_candidates.csv", b""))
    annotated = _annotate_candidates(db, candidates)
    files["challenge_candidates.csv"] = _write_csv(annotated)
    files["challenge_portfolio.csv"] = _portfolio_csv()
    files["challenge_portfolio_summary.json"] = json.dumps(
        _portfolio_summary(), indent=2, ensure_ascii=False
    ).encode("utf-8")

    try:
        summary = json.loads(files.get("challenge_summary.json", b"{}").decode("utf-8"))
    except Exception:
        summary = {}
    summary.update({
        "portfolio_version": VERSION,
        "portfolio_reviewed_at": REVIEWED_AT,
        "portfolio_candidate_annotations": True,
        "clv_first_gt0_filter_column": True,
        "clv_first_filter_authority": "RESEARCH_ONLY",
        "duplicate_stake_rule": "same underlying event/market/selection/point is staked once",
        "paddy_power_excluded": True,
        "challenge_portfolio_file": "challenge_portfolio.csv",
        "challenge_portfolio_summary_file": "challenge_portfolio_summary.json",
        "betting_authority": False,
    })
    files["challenge_summary.json"] = json.dumps(summary, indent=2, ensure_ascii=False).encode("utf-8")

    try:
        manifest = json.loads(files.get("manifest.json", b"{}").decode("utf-8"))
    except Exception:
        manifest = {}
    manifest["app_version"] = release_version
    manifest["export_version"] = VERSION
    manifest["portfolio_reviewed_at"] = REVIEWED_AT
    manifest_files = list(manifest.get("files") or [])
    for extra in ("challenge_portfolio.csv", "challenge_portfolio_summary.json"):
        if extra not in manifest_files:
            manifest_files.append(extra)
    manifest["files"] = manifest_files
    files["manifest.json"] = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")

    readme = files.get("README.txt", b"").decode("utf-8", errors="replace").rstrip()
    readme += (
        "\n\n£10→£500 PORTFOLIO REFRESH — 27 OCT 2026\n"
        "challenge_portfolio.csv = frozen lane/status/stake policy after the 27-Oct review.\n"
        "challenge_candidates.csv now includes challenge lane/status/stake, diagnostic tags, a stake-once key, and CLV-First forward filter fields.\n"
        "PRED4 broad remains £10 Core with reduced confidence. NFL 2.00–2.99 and the two EuroLeague bands remain £5 Challengers.\n"
        "PRED1/PRED2 BTTS and normal NCAAF 3.00–4.99 are now Watch/£0. FHG >=70% and NCAAF-PRED1 are Watch/£0.\n"
        "CLV-First >0 is FILTER_ONLY and never changes stake automatically. Paddy Power remains excluded.\n"
        "Duplicate underlying selections across overlapping lanes are counted/staked once.\n"
        "No live betting authority is added by this export refresh.\n"
    )
    files["README.txt"] = readme.encode("utf-8")

    return _rebuild_zip(files), filename


def install(db, release_version: str) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_BUILD, _RELEASE_VERSION
    _RELEASE_VERSION = str(release_version)
    if _INSTALLED:
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
            "release_version": _RELEASE_VERSION,
        }

    _ORIGINAL_BUILD = base.build_challenge_export
    base.build_challenge_export = build_challenge_export_v3
    base.VERSION = VERSION
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "release_version": _RELEASE_VERSION,
        "route": base.ROUTE,
        "candidate_annotations": True,
        "portfolio_file": "challenge_portfolio.csv",
        "clv_first_filter_only": True,
        "provider_calls_added": 0,
        "selection_rules_changed": False,
        "staking_authority_changed": False,
        "betting_authority_changed": False,
    }


__all__ = ["VERSION", "PORTFOLIO_ROWS", "install", "build_challenge_export_v3"]
