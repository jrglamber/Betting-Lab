from __future__ import annotations

"""Rotate HR constituent lanes without resetting the active HR experiment.

The 31 Oct review changes constituents inside the existing V3 theoretical-queue
experiment. Historical candidates/cards/results stay immutable. Only current
queued candidates that no longer match the revised constituent definitions are
retired; still-eligible opportunities are then re-observed through the existing
V3 theoretical queue and keep the same settlement/price/placeability rules.

Changes agreed 31 Oct 2026:
- remove PRED1/PRED2 BTTS No 2.50-2.99;
- seasonally bench MLB 1.50-1.99;
- tighten EuroLeague core from 1.50-2.99 to 1.50-1.99;
- split NFL into 1.50-1.99 and 2.00-2.99 bands;
- leave football totals/BTTS, tennis and football H2H challenger lanes in place.
"""

import json
from copy import deepcopy
from typing import Any, Dict, Mapping

import hit_rate_multiples_lab as hr
from db import utc_now_iso

VERSION = "HR_LANE_ROTATION_2026_10_31_V3_CONTINUITY"
BASE_POLICY_VERSION = "HRM_2026-10-18_V3_THEORETICAL_QUEUE"
ACCIDENTAL_POLICY_VERSION = "HRM_2026-10-31_V4_ROTATED_LANES"
LEGACY_V1_POLICY_VERSION = "HRM_2026-10-12_V1"
POLICY_VERSION = BASE_POLICY_VERSION
_INSTALLED = False
_ORIGINAL_SOURCE_ROWS = None
_ORIGINAL_SCOREBOARD = None

REMOVE_LANES = {"PRED12_BTTS_NO_2_50_2_99", "MLB_H2H_1_50_1_99"}
EURO_OLD = "EUROLEAGUE_H2H_1_50_2_99"
EURO_NEW = "EUROLEAGUE_H2H_1_50_1_99"
NFL_OLD = "NFL_H2H_1_50_2_99"
NFL_LOW = "NFL_H2H_1_50_1_99"
NFL_HIGH = "NFL_H2H_2_00_2_99"


def _minimum(row: Mapping[str, Any]) -> float:
    try:
        return float(row.get("min_odds") or row.get("min_required_odds") or 0.0)
    except Exception:
        return 0.0


def _source_price(row: Mapping[str, Any]) -> float:
    try:
        return float(row.get("source_odds") or 0.0)
    except Exception:
        return 0.0


def _band(row: Mapping[str, Any], lane: str, lo: float, hi: float) -> dict[str, Any] | None:
    minimum = _minimum(row)
    if minimum <= 1.0 or minimum >= hi:
        return None
    out = dict(row)
    out["source_lane"] = lane
    out["band_min"] = float(lo)
    out["band_max"] = float(hi)
    source = _source_price(out)
    if source <= 1.0 or source < lo or source >= hi:
        out["source_odds"] = max(float(lo), minimum)
    return out


def _rotated_source_rows(db, now):
    # At install time this wraps the already-installed V3 theoretical queue.
    # Therefore pre-price model/reference candidates remain available exactly as
    # before; only constituent membership is changed here.
    rows = list(_ORIGINAL_SOURCE_ROWS(db, now))
    out: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        lane = str(row.get("source_lane") or "")
        if lane in REMOVE_LANES:
            continue
        if lane == EURO_OLD:
            source = _source_price(row)
            if 1.5 <= source < 2.0 or (source <= 1.0 and _minimum(row) < 2.0):
                changed = _band(row, EURO_NEW, 1.5, 2.0)
                if changed:
                    out.append(changed)
            continue
        if lane == NFL_OLD:
            source = _source_price(row)
            if 1.5 <= source < 2.0:
                changed = _band(row, NFL_LOW, 1.5, 2.0)
            elif 2.0 <= source < 3.0:
                changed = _band(row, NFL_HIGH, 2.0, 3.0)
            elif source <= 1.0 and _minimum(row) < 2.0:
                changed = _band(row, NFL_LOW, 1.5, 2.0)
            elif source <= 1.0 and _minimum(row) < 3.0:
                changed = _band(row, NFL_HIGH, 2.0, 3.0)
            else:
                changed = None
            if changed:
                out.append(changed)
            continue
        out.append(row)
    return sorted(out, key=lambda r: (
        str(r.get("source_created_at") or ""), str(r.get("experiment_key") or ""),
        str(r.get("source_lane") or ""), int(r.get("source_id") or 0),
    ))


def _policy() -> Dict[str, Any]:
    policy = deepcopy(getattr(hr, "POLICY", {}) or {})
    policy["HR1"] = {
        "label": "Strict high-hit pool — constituents rotated 31 Oct 2026",
        "pockets": [
            "Football BTTS 1.80-1.99", "Football totals 1.80-1.99",
            "Tennis H2H 1.80-1.99 (probation)", "EuroLeague H2H 1.50-1.99",
            "NFL H2H 1.50-1.99", "NFL H2H 2.00-2.99",
        ],
    }
    policy["HR2"] = {
        "label": "Expanded hit+value challenger — constituents rotated 31 Oct 2026",
        "pockets": [
            "HR1 pockets", "Football totals 2.00-2.99",
            "Football H2H 2.50-2.99 (probation)", "Tennis H2H 2.00-2.99 (probation)",
            "EuroLeague H2H 3.00-3.99",
        ],
    }
    return policy


def _ensure_rotation_audit(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS hit_rate_lane_rotations (
        rotation_version TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL,
        base_policy_version TEXT NOT NULL,
        details_json TEXT NOT NULL
    )""")


def _count(db, sql: str, params=()) -> int:
    try:
        row = db.fetchone(sql, params)
        return int((row or {}).get("n") or 0)
    except Exception:
        return 0


def _snapshot(db, policy_version: str) -> Dict[str, int]:
    return {
        "candidates": _count(db, "SELECT COUNT(*) AS n FROM hit_rate_candidates WHERE policy_version=?", (policy_version,)),
        "queued": _count(db, "SELECT COUNT(*) AS n FROM hit_rate_candidates WHERE policy_version=? AND status='QUEUED'", (policy_version,)),
        "consumed": _count(db, "SELECT COUNT(*) AS n FROM hit_rate_candidates WHERE policy_version=? AND status='CONSUMED'", (policy_version,)),
        "expired": _count(db, "SELECT COUNT(*) AS n FROM hit_rate_candidates WHERE policy_version=? AND status='EXPIRED'", (policy_version,)),
        "cards": _count(db, "SELECT COUNT(*) AS n FROM hit_rate_cards WHERE policy_version=?", (policy_version,)),
        "settled_cards": _count(db, "SELECT COUNT(*) AS n FROM hit_rate_cards WHERE policy_version=? AND settled_at IS NOT NULL", (policy_version,)),
    }


def _retire_rotation_boundary_queues(db) -> Dict[str, int]:
    """Refresh current queues only; never alter consumed candidates/cards/results."""
    stamp = utc_now_iso()
    retired: Dict[str, int] = {}

    # The card builder scans every policy_version with QUEUED rows, so no legacy
    # namespace may remain formable after V3 is restored as the active stream.
    retired["non_v3_queued"] = int(db.execute(
        "UPDATE hit_rate_candidates SET status='EXPIRED',expired_at=? "
        "WHERE policy_version<>? AND status='QUEUED'",
        (stamp, BASE_POLICY_VERSION),
    ) or 0)

    placeholders = ",".join("?" for _ in REMOVE_LANES)
    retired["removed_lane_queued"] = int(db.execute(
        f"UPDATE hit_rate_candidates SET status='EXPIRED',expired_at=? "
        f"WHERE policy_version=? AND status='QUEUED' AND source_lane IN ({placeholders})",
        (stamp, BASE_POLICY_VERSION, *sorted(REMOVE_LANES)),
    ) or 0)

    # Rebase the two changed broad bands completely. Any still-current eligible
    # opportunity is re-observed on the next V3 ingest under the narrower/split
    # source_lane key. This avoids old broad + new narrow duplicates.
    retired["euroleague_broad_queued"] = int(db.execute(
        "UPDATE hit_rate_candidates SET status='EXPIRED',expired_at=? "
        "WHERE policy_version=? AND status='QUEUED' AND source_lane=?",
        (stamp, BASE_POLICY_VERSION, EURO_OLD),
    ) or 0)
    retired["nfl_broad_queued"] = int(db.execute(
        "UPDATE hit_rate_candidates SET status='EXPIRED',expired_at=? "
        "WHERE policy_version=? AND status='QUEUED' AND source_lane=?",
        (stamp, BASE_POLICY_VERSION, NFL_OLD),
    ) or 0)
    return retired


def _record_rotation(db, details: Dict[str, Any]) -> None:
    _ensure_rotation_audit(db)
    db.execute("""INSERT INTO hit_rate_lane_rotations(
        rotation_version,applied_at,base_policy_version,details_json
    ) VALUES(?,?,?,?) ON CONFLICT(rotation_version) DO NOTHING""", (
        VERSION, utc_now_iso(), BASE_POLICY_VERSION, json.dumps(details, sort_keys=True),
    ))


def install(db) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_SOURCE_ROWS, _ORIGINAL_SCOREBOARD
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    _ORIGINAL_SOURCE_ROWS = hr._source_rows
    _ORIGINAL_SCOREBOARD = hr.scoreboard

    # Critical continuity rule: the lane rotation is not a new experiment. Keep
    # the V3 theoretical-queue namespace, keying, result resolver and price rules.
    hr.POLICY_VERSION = BASE_POLICY_VERSION
    hr.POLICY = _policy()
    hr._source_rows = _rotated_source_rows

    def scoreboard_with_rotation(db_obj):
        out = dict(_ORIGINAL_SCOREBOARD(db_obj))
        out["lane_rotation"] = {
            "version": VERSION,
            "policy_version": BASE_POLICY_VERSION,
            "continuous_experiment": True,
            "theoretical_queue_preserved": True,
            "queue_rebased_for_constituent_rotation": True,
            "removed": sorted(REMOVE_LANES),
            "tightened": {EURO_OLD: EURO_NEW},
            "split": {NFL_OLD: [NFL_LOW, NFL_HIGH]},
            "accidental_policy_retired": ACCIDENTAL_POLICY_VERSION,
            "historical_evidence_rewritten": False,
            "betting_authority": False,
        }
        return out

    hr.scoreboard = scoreboard_with_rotation

    before = {
        "base_v3": _snapshot(db, BASE_POLICY_VERSION),
        "accidental_v4": _snapshot(db, ACCIDENTAL_POLICY_VERSION),
        "legacy_v1": _snapshot(db, LEGACY_V1_POLICY_VERSION),
    }

    activation = hr._activate_policy(db)
    retired = _retire_rotation_boundary_queues(db)

    after = {
        "base_v3": _snapshot(db, BASE_POLICY_VERSION),
        "accidental_v4": _snapshot(db, ACCIDENTAL_POLICY_VERSION),
        "legacy_v1": _snapshot(db, LEGACY_V1_POLICY_VERSION),
    }
    audit = {
        "removed": sorted(REMOVE_LANES),
        "tightened": {EURO_OLD: EURO_NEW},
        "split": {NFL_OLD: [NFL_LOW, NFL_HIGH]},
        "before": before,
        "retired_queued": retired,
        "after": after,
        "historical_evidence_rewritten": False,
        "continuity_namespace": BASE_POLICY_VERSION,
    }
    _record_rotation(db, audit)

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "policy_version": BASE_POLICY_VERSION,
        "continuous_experiment": True,
        "theoretical_queue_preserved": True,
        "queue_rebased_for_constituent_rotation": True,
        "accidental_policy_version_retired": ACCIDENTAL_POLICY_VERSION,
        "removed": sorted(REMOVE_LANES),
        "euroleague": "1.50-1.99 core; 3.00-3.99 remains HR2 challenger",
        "nfl": ["1.50-1.99", "2.00-2.99"],
        "tennis_probation_retained": True,
        "football_h2h_probation_retained": True,
        "historical_evidence_rewritten": False,
        "provider_calls_added": 0,
        "betting_authority": False,
        "retired_queued": retired,
        "before": before,
        "after": after,
        "activation": dict(activation or {}),
    }


__all__ = ["VERSION", "POLICY_VERSION", "BASE_POLICY_VERSION", "install"]
