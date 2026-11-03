from __future__ import annotations

"""Export-only £200→£2,000 evidence-review framework (3 Nov 2026).

Preserves the historical October frozen portfolio separately, while replacing its
misleading *manual challenge* stake suggestions. No model, provider, scheduler,
selection, alert, or execution path is modified.
"""

import csv
import io
import json
import math
import zipfile
from typing import Any, Dict, Mapping, Optional

import challenge_export_patch as base

VERSION = "CHALLENGE_EXPORT_V5_EVIDENCE_FRAMEWORK_2026_11_03"
PHASE = "200_TO_2000_2026_11_03"
REVIEWED_AT = "2026-11-03"
_INSTALLED = False
_ORIGINAL_BUILD = None
_RELEASE_VERSION = None

POLICY = {
    "version": VERSION,
    "phase_id": PHASE,
    "reviewed_at": REVIEWED_AT,
    "starting_bank_gbp": 200.0,
    "target_bank_gbp": 2000.0,
    "target_window_days": 30,
    "target_is_aspirational_not_a_staking_deadline": True,
    "balance_source": "user-reported; never inferred from the export or sportsbook",
    "candidate_universe": "All forward available markets/sports may be reviewed; no blanket lane approvals",
    "decision_method": "manual, per candidate; a no-bet decision is normal",
    "review_order": [
        "model probability and fair-price edge at the actually available bookmaker odds",
        "historical health (settled count, executable-price ROI, actual CLV and calibration) for the relevant lane and price band",
        "agreement or conflict across independently built models",
        "CLV-first predicted signal as supporting or opposing evidence, not a bet trigger or veto",
        "market/competition context including news, injuries, liquidity and timing",
        "same-event or correlated exposure and existing bets",
        "manual conviction and stake sizing only after all checks",
    ],
    "price_policy": "Export odds are indicative; a bet needs a current executable quote meeting its manual minimum price",
    "models": "PRED1, PRED2, PRED4, multisport, totals, BTTS and other lanes are evidence, not automatic approvals",
    "clv_first_role": "supporting/contradictory evidence only; predicted positive CLV alone never creates a bet",
    "fhg_role": "hit-rate and pricing context; multiple-leg stakes stay small until executable-price ROI is established",
    "weak_lanes": "poor parent ROI/CLV and small profitable pockets downgrade confidence, not silent exclusion or automatic approval",
    "stake_tiers_at_200_gbp_bank": [
        {"tier": "NORMAL", "stake_gbp": 5.0, "reference_bank_pct": 2.5, "requires": "plausible edge with material caveats"},
        {"tier": "STRONG", "stake_gbp": 10.0, "reference_bank_pct": 5.0, "requires": "clear live price value and broadly supportive evidence"},
        {"tier": "EXCEPTIONAL", "stake_gbp": 15.0, "reference_bank_pct": 7.5, "requires": "unusually compelling multi-source agreement and clear price value"},
    ],
    "staking": "All tier amounts are discussion guidance, not automatic or preapproved stakes; scale cautiously with actual bank",
    "rare_20_gbp_plus": "requires explicit manual assessment; never default",
    "portfolio_exposure_guide_pct": "usually 10–15% or less of confirmed current bank; correlated markets counted together",
    "duplicate_rule": "one exposure per identical underlying event/market/selection/line; no accidental double-staking",
    "bookmaker_excluded": ["Paddy Power"],
    "no_reload_or_chase": True,
    "automatic_stake_gbp": 0.0,
    "betting_authority": False,
    "provider_calls_added": 0,
    "legacy_policy_preserved": ["challenge_legacy_portfolio_20261027.csv", "challenge_legacy_portfolio_summary_20261027.json"],
}

SOURCE_ROLES = [
    ("MODEL_AND_PRICE", "Model fair value and executable price", "PRIMARY_EVIDENCE", "Read offered price as indicative until checked live"),
    ("LANE_HEALTH", "Settled ROI, actual CLV, calibration, sample size", "PRIMARY_EVIDENCE", "Check relevant sport, market and price band; not just headline ROI"),
    ("MODEL_AGREEMENT", "Cross-model agreement and conflicts", "SUPPORT_OR_WARNING", "Shared inputs can cause correlated model errors"),
    ("CLV_FIRST", "CLV-first forecast and history", "SUPPORT_OR_WARNING", "Positive forecast is neither sufficient nor a mandatory pass"),
    ("FHG", "First-half goals", "CONDITIONAL_REVIEW", "Watch executable-price profitability and limit multiple stakes"),
    ("H2H_TOTALS_BTTS", "Football match results, totals and BTTS", "CONDITIONAL_REVIEW", "All leagues reviewed on the same criteria"),
    ("MULTISPORT_TENNIS", "NFL, NCAAF, basketball and tennis", "CONDITIONAL_REVIEW", "Weak parent lanes and small samples require caution"),
    ("MULTIPLES", "Accas / HR systems", "SMALL_STAKE_RESEARCH", "High variance; no general automatic challenge approval"),
    ("MARKET_CONTEXT", "Fixture news, price moves and existing exposure", "SUPPORT_OR_WARNING", "Manually verify before placing"),
]


def _read_csv(payload: bytes) -> list[dict[str, str]]:
    if not payload:
        return []
    return [dict(x) for x in csv.DictReader(io.StringIO(payload.decode("utf-8-sig", errors="replace")))]


def _write_csv(rows: list[Mapping[str, Any]]) -> bytes:
    if not rows:
        return b""
    fields = list(dict.fromkeys(k for row in rows for k in row.keys()))
    dst = io.StringIO(newline="")
    writer = csv.DictWriter(dst, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows({k: "" if r.get(k) is None else r.get(k) for k in fields} for r in rows)
    return dst.getvalue().encode("utf-8")


def _json(data: bytes) -> dict[str, Any]:
    try:
        result = json.loads(data.decode("utf-8"))
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _num(v: Any) -> Optional[float]:
    try:
        val = float(v)
        return val if math.isfinite(val) else None
    except (TypeError, ValueError):
        return None


def _clv_support(row: Mapping[str, Any]) -> str:
    value = _num(row.get("clv_first_predicted_clv_pct"))
    if value is None:
        return "NO_OBSERVATION"
    if value > 0:
        return "POSITIVE_SUPPORT_NOT_TRIGGER"
    if value < 0:
        return "NEGATIVE_CAUTION_NOT_VETO"
    return "NEUTRAL"


def _annotate(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    # Preserve original 27-Oct allocation as historical audit fields, not live advice.
    for field in ("challenge_lane", "challenge_status", "challenge_stake_gbp", "challenge_note", "challenge_reviewed_at"):
        item["legacy_" + field] = item.get(field, "")
    item["challenge_lane"] = "EVIDENCE_DRIVEN_MANUAL_REVIEW"
    item["challenge_status"] = "MANUAL_REVIEW_REQUIRED"
    item["challenge_stake_gbp"] = 0.0
    item["challenge_note"] = "No blanket approval: verify live price, lane health, model agreement, CLV support and existing exposure."
    item["challenge_reviewed_at"] = REVIEWED_AT
    item["challenge_phase"] = PHASE
    item["clv_first_evidence_role"] = _clv_support(item)
    offered = _num(item.get("offered_odds"))
    min_odds = _num(item.get("min_odds"))
    item["export_price_vs_minimum"] = (
        "ABOVE_RECORDED_MIN_NOT_LIVE" if offered is not None and min_odds is not None and offered >= min_odds
        else "BELOW_RECORDED_MIN_NOT_LIVE" if offered is not None and min_odds is not None
        else "INSUFFICIENT_RECORDED_PRICE"
    )
    item["manual_current_price_required"] = True
    return item


def _portfolio_rows() -> list[dict[str, Any]]:
    return [
        {
            "lane_key": key, "label": name, "status": role, "stake_gbp": 0.0,
            "rule": note, "rationale": "Decision support only; no fixed lane allocation or automatic stake.",
            "reviewed_at": REVIEWED_AT, "frozen_boundary": "", "duplicate_rule": POLICY["duplicate_rule"],
            "paddy_power_excluded": True, "betting_authority": False,
        }
        for key, name, role, note in SOURCE_ROLES
    ]


def build_challenge_export_v5(db, settings, app_version: str):
    release_version = str(_RELEASE_VERSION or app_version)
    raw_zip, filename = _ORIGINAL_BUILD(db, settings, release_version)
    with zipfile.ZipFile(io.BytesIO(raw_zip), "r") as archive:
        files = {name: archive.read(name) for name in archive.namelist()}

    # Keep the frozen 27-Oct portfolio in named audit files; do not silently rewrite history.
    if "challenge_portfolio.csv" in files:
        files["challenge_legacy_portfolio_20261027.csv"] = files["challenge_portfolio.csv"]
    if "challenge_portfolio_summary.json" in files:
        files["challenge_legacy_portfolio_summary_20261027.json"] = files["challenge_portfolio_summary.json"]
    files["challenge_candidates.csv"] = _write_csv([_annotate(r) for r in _read_csv(files.get("challenge_candidates.csv", b""))])
    files["challenge_clv_first_shortlist.csv"] = _write_csv([_annotate(r) for r in _read_csv(files.get("challenge_clv_first_shortlist.csv", b""))])
    files["challenge_portfolio.csv"] = _write_csv(_portfolio_rows())
    files["challenge_portfolio_summary.json"] = json.dumps({
        "version": VERSION,
        "purpose": "£200→£2,000 human-reviewed challenge evidence, not automatic staking",
        "reviewed_at": REVIEWED_AT,
        "phase_id": PHASE,
        "active_staking_lanes": [],
        "policy_file": "challenge_selection_framework.json",
        "historical_portfolio_file": "challenge_legacy_portfolio_20261027.csv",
        "clv_first_role": POLICY["clv_first_role"],
        "automatic_stake_gbp": 0.0,
        "betting_authority": False,
    }, indent=2, ensure_ascii=False).encode("utf-8")
    files["challenge_selection_framework.json"] = json.dumps(POLICY, indent=2, ensure_ascii=False).encode("utf-8")

    summary = _json(files.get("challenge_summary.json", b""))
    summary.update({
        "export_version": VERSION,
        "portfolio_version": VERSION,
        "portfolio_reviewed_at": REVIEWED_AT,
        "challenge_phase": PHASE,
        "manual_review_required": True,
        "automatic_stake_gbp": 0.0,
        "challenge_framework_file": "challenge_selection_framework.json",
        "challenge_portfolio_file": "challenge_portfolio.csv",
        "legacy_policy_file": "challenge_legacy_portfolio_20261027.csv",
        "clv_first_role": "SUPPORT_OR_WARNING_NOT_STANDALONE",
        "betting_authority": False,
    })
    files["challenge_summary.json"] = json.dumps(summary, indent=2, ensure_ascii=False).encode("utf-8")

    manifest = _json(files.get("manifest.json", b""))
    manifest.update({
        "app_version": release_version,
        "export_version": VERSION,
        "phase_id": PHASE,
        "portfolio_reviewed_at": REVIEWED_AT,
    })
    manifest_files = list(manifest.get("files") or [])
    for name in files:
        if name not in manifest_files:
            manifest_files.append(name)
    manifest["files"] = manifest_files
    files["manifest.json"] = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")

    files["README.txt"] = (
        "BETTING LAB — £200→£2,000 CHALLENGE (3 NOV 2026)\n\n"
        "Current policy is MANUAL EVIDENCE REVIEW, not a static list of pre-approved betting lanes.\n"
        "The 30-day target is aspirational. Never chase to meet its pace.\n"
        "challenge_selection_framework.json = full policy, selection tests, stake tiers and safety rules.\n"
        "challenge_candidates.csv = forward candidates; challenge_stake_gbp is ALWAYS £0; bookmaker prices must be verified live.\n"
        "challenge_model_support.csv = model agreement/conflict evidence.\n"
        "challenge_lane_health.csv = settled results, actual CLV, sample-size and calibration evidence.\n"
        "challenge_clv_first_shortlist.csv = CLV-first research-only positive shortlist; never a standalone betting instruction.\n"
        "challenge_clv_first_summary.json = CLV-first overall forward evidence.\n"
        "challenge_portfolio.csv and challenge_portfolio_summary.json = CURRENT source role/status guidance.\n"
        "challenge_legacy_portfolio_20261027.csv and its summary = historical frozen October policy, not current staking guidance.\n"
        "Legacy per-candidate allocation values are retained only as legacy_challenge_* audit columns.\n"
        "Baseline £200-bank tier discussion: normal £5, strong £10, exceptional £15. All require manual authorisation.\n"
        "No provider calls, automatic stakes, new signals, betting authority or execution changes.\n"
    ).encode("utf-8")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return out.getvalue(), filename


def install(release_version: str) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_BUILD, _RELEASE_VERSION
    _RELEASE_VERSION = str(release_version)
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}
    _ORIGINAL_BUILD = base.build_challenge_export
    base.build_challenge_export = build_challenge_export_v5
    base.VERSION = VERSION
    _INSTALLED = True
    return {
        "installed": True, "version": VERSION, "route": base.ROUTE,
        "provider_calls_added": 0, "automatic_staking": False, "betting_authority": False,
    }


__all__ = ["VERSION", "POLICY", "install", "build_challenge_export_v5"]
