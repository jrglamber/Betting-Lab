from __future__ import annotations

"""Rotate active HR constituent lanes without rewriting historical evidence.

New candidates use a fresh policy version. Existing candidates/cards/results stay
immutable. Changes agreed 31 Oct 2026:
- remove PRED1/PRED2 BTTS No 2.50-2.99;
- seasonally bench MLB 1.50-1.99;
- tighten EuroLeague core from 1.50-2.99 to 1.50-1.99;
- split NFL into 1.50-1.99 and 2.00-2.99 bands;
- leave football totals/BTTS, tennis and football H2H challenger lanes in place.
"""

import hashlib
from copy import deepcopy
from typing import Any, Dict, Mapping

import hit_rate_multiples_lab as hr

VERSION = "HR_LANE_ROTATION_2026_10_31_V1"
POLICY_VERSION = "HRM_2026-10-31_V4_ROTATED_LANES"
_INSTALLED = False
_ORIGINAL_SOURCE_ROWS = None
_ORIGINAL_KEY = None
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
    try:
        source = float(out.get("source_odds") or 0.0)
    except Exception:
        source = 0.0
    if source <= 1.0 or source < lo or source >= hi:
        out["source_odds"] = max(float(lo), minimum)
    return out


def _rotated_source_rows(db, now):
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


def _key(policy: str, exp: str, system: str, row: Mapping[str, Any]) -> str:
    if policy != POLICY_VERSION:
        return _ORIGINAL_KEY(policy, exp, system, row)
    point = row.get("point")
    try:
        point_key = "" if point is None else f"{float(point):.8f}".rstrip("0").rstrip(".")
    except Exception:
        point_key = str(point or "")
    raw = "|".join([
        policy, exp, system, str(row.get("event_id") or "").lower(),
        str(row.get("market_key") or "").lower(), str(row.get("selection") or "").lower(),
        point_key, str(row.get("source_lane") or "").lower(),
        f"{float(row.get('band_min') or 0):.4f}", f"{float(row.get('band_max') or 0):.4f}",
    ])
    return hashlib.sha256(raw.encode()).hexdigest()


def _policy() -> Dict[str, Any]:
    policy = deepcopy(getattr(hr, "POLICY", {}) or {})
    policy["HR1"] = {
        "label": "Strict high-hit pool — rotated 31 Oct 2026",
        "pockets": [
            "Football BTTS 1.80-1.99", "Football totals 1.80-1.99",
            "Tennis H2H 1.80-1.99 (probation)", "EuroLeague H2H 1.50-1.99",
            "NFL H2H 1.50-1.99", "NFL H2H 2.00-2.99",
        ],
    }
    policy["HR2"] = {
        "label": "Expanded hit+value challenger — rotated 31 Oct 2026",
        "pockets": [
            "HR1 pockets", "Football totals 2.00-2.99",
            "Football H2H 2.50-2.99 (probation)", "Tennis H2H 2.00-2.99 (probation)",
            "EuroLeague H2H 3.00-3.99",
        ],
    }
    return policy


def install(db) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_SOURCE_ROWS, _ORIGINAL_KEY, _ORIGINAL_SCOREBOARD
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    _ORIGINAL_SOURCE_ROWS = hr._source_rows
    _ORIGINAL_KEY = hr._key
    _ORIGINAL_SCOREBOARD = hr.scoreboard

    hr.POLICY_VERSION = POLICY_VERSION
    hr.POLICY = _policy()
    hr._source_rows = _rotated_source_rows
    hr._key = _key

    def scoreboard_with_rotation(db_obj):
        out = dict(_ORIGINAL_SCOREBOARD(db_obj))
        out["lane_rotation"] = {
            "version": VERSION,
            "policy_version": POLICY_VERSION,
            "removed": sorted(REMOVE_LANES),
            "tightened": {EURO_OLD: EURO_NEW},
            "split": {NFL_OLD: [NFL_LOW, NFL_HIGH]},
            "historical_evidence_rewritten": False,
            "betting_authority": False,
        }
        return out

    hr.scoreboard = scoreboard_with_rotation
    activation = hr._activate_policy(db)
    _INSTALLED = True
    return {
        "installed": True, "already_installed": False, "version": VERSION,
        "policy_version": POLICY_VERSION, "removed": sorted(REMOVE_LANES),
        "euroleague": "1.50-1.99 core; 3.00-3.99 remains HR2 challenger",
        "nfl": ["1.50-1.99", "2.00-2.99"],
        "tennis_probation_retained": True, "football_h2h_probation_retained": True,
        "historical_evidence_rewritten": False, "provider_calls_added": 0,
        "betting_authority": False, "activation": dict(activation or {}),
    }


__all__ = ["VERSION", "POLICY_VERSION", "install"]
