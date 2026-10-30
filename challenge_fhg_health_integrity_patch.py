from __future__ import annotations

"""Integrity fixes for FHG1 challenge-export health and fixture lifecycle.

The original lane-health implementation used ``value or ''`` when reading
``fhg_hit``. A genuine loss is numeric 0, so those rows were excluded. This
module also installs the postponed-fixture guard so stale future FHG candidates
cannot leak into Challenge Export.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import challenge_export_patch as challenge
import first_half_goal_lab as fhg
from fhg_postponed_guard_patch import install as install_fhg_postponed_guard

VERSION = "CHALLENGE_FHG_HEALTH_INTEGRITY_V2_POSTPONED_GUARD"
_INSTALLED = False


def _hit_value(row: Mapping[str, Any]):
    value = row.get("fhg_hit")
    if value is None or value == "":
        return None
    try:
        parsed = int(float(value))
    except Exception:
        return None
    return parsed if parsed in (0, 1) else None


def _probability(row: Mapping[str, Any]):
    try:
        value = float(row.get("estimated_probability"))
    except Exception:
        return None
    return value if 0.0 <= value <= 1.0 else None


def _fhg_health_integrity(db) -> list[dict[str, Any]]:
    rows = challenge._safe_rows(db, "first_half_goal_candidates")
    settled = [
        r for r in rows
        if str(r.get("status") or "").upper() == "SETTLED" and _hit_value(r) is not None
    ]

    now = datetime.now(timezone.utc)
    overdue_cutoff = now - timedelta(hours=2)
    overdue_open = []
    for row in rows:
        if str(row.get("status") or "").upper() != "OPEN":
            continue
        ko = challenge._parse(row.get("commence_time"))
        if ko is not None and ko <= overdue_cutoff:
            overdue_open.append(row)

    threshold = float(getattr(fhg, "THRESHOLD", 0.70))
    qualifying = [
        r for r in settled
        if (_probability(r) is not None and _probability(r) >= threshold)
    ]
    qualifying_overdue = [
        r for r in overdue_open
        if (_probability(r) is not None and _probability(r) >= threshold)
    ]

    out: list[dict[str, Any]] = []

    def record(
        scope: str,
        key: str,
        group: list[dict[str, Any]],
        pending_group: list[dict[str, Any]] | None = None,
        *,
        league: str = "",
        tier: str = "",
        evidence_kind: str = "HIT_RATE_ONLY",
    ):
        if not group:
            return
        pending_group = pending_group or []
        n = len(group)
        hits = sum(_hit_value(r) == 1 for r in group)
        losses = n - hits
        grade, maturity = challenge._evidence_grade(n)
        dates = [
            dt for dt in (challenge._parse(r.get("settled_at")) for r in group)
            if dt is not None
        ]
        probs = [p for p in (_probability(r) for r in group) if p is not None]
        avg_prob = sum(probs) / len(probs) if probs else None
        actual = hits / n if n else None
        due_total = n + len(pending_group)
        coverage = (100.0 * n / due_total) if due_total else None
        out.append({
            "source": "FHG1",
            "source_table": "first_half_goal_candidates",
            "scope": scope,
            "scope_key": key,
            "sport_key": "",
            "league": league,
            "market_key": "fhg_over_0.5",
            "odds_band": "",
            "tier": tier,
            "evidence_kind": evidence_kind,
            "settled_n": n,
            "wins": hits,
            "losses": losses,
            "pushes": 0,
            "voids": 0,
            "hit_rate_pct": (100.0 * actual) if actual is not None else None,
            "avg_model_probability_pct": (100.0 * avg_prob) if avg_prob is not None else None,
            "calibration_gap_pct_points": (
                100.0 * (actual - avg_prob)
                if actual is not None and avg_prob is not None else None
            ),
            "overdue_open_n": len(pending_group),
            "settlement_coverage_pct": coverage,
            "net_pnl_units": None,
            "net_roi_pct": None,
            "avg_offered_odds": None,
            "avg_edge_pct": None,
            "clv_samples": 0,
            "avg_clv_pct": None,
            "beat_close_pct": None,
            "evidence_grade": grade,
            "maturity": maturity,
            "grade_basis": "sample_size_only",
            "first_settled_at": min(dates).isoformat() if dates else None,
            "last_settled_at": max(dates).isoformat() if dates else None,
        })

    record(
        "QUALIFYING",
        f"P>={threshold:.2f}",
        qualifying,
        qualifying_overdue,
        evidence_kind="FORWARD_HIT_RATE_AT_SELECTION_THRESHOLD",
    )

    record(
        "ALL_SETTLED",
        "ALL_MODEL_CANDIDATES",
        settled,
        overdue_open,
        evidence_kind="DIAGNOSTIC_ALL_CANDIDATES",
    )

    tier_defs = (
        ("A", 0.80, 1.01),
        ("B", 0.75, 0.80),
        ("C", threshold, 0.75),
    )
    for tier, low, high in tier_defs:
        group = [
            r for r in settled
            if (_probability(r) is not None and low <= _probability(r) < high)
        ]
        pending = [
            r for r in overdue_open
            if (_probability(r) is not None and low <= _probability(r) < high)
        ]
        if group:
            record(
                "TIER",
                tier,
                group,
                pending,
                tier=tier,
                evidence_kind="FORWARD_HIT_RATE_BY_TIER",
            )

    leagues = sorted({
        str(r.get("league") or "").strip()
        for r in qualifying
        if str(r.get("league") or "").strip()
    })
    for league in leagues:
        group = [r for r in qualifying if str(r.get("league") or "").strip() == league]
        if len(group) < 5:
            continue
        pending = [
            r for r in qualifying_overdue
            if str(r.get("league") or "").strip() == league
        ]
        record(
            "LEAGUE",
            league,
            group,
            pending,
            league=league,
            evidence_kind="FORWARD_HIT_RATE_AT_SELECTION_THRESHOLD",
        )

    return out


def install() -> dict[str, Any]:
    global _INSTALLED

    # Install this on every call, including idempotent/reload paths, so the
    # export/lifecycle guard cannot be skipped merely because health was already
    # patched in the process.
    try:
        postponed_guard = install_fhg_postponed_guard()
    except Exception as exc:
        postponed_guard = {
            "installed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    if _INSTALLED or getattr(challenge, "_fhg_health_integrity_patch", False):
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
            "postponed_guard": postponed_guard,
        }

    challenge._fhg_health = _fhg_health_integrity
    challenge._fhg_health_integrity_patch = True
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "bug_fixed": "numeric_zero_fhg_losses_were_excluded",
        "adds": [
            "threshold-qualified hit rate",
            "calibration",
            "settlement coverage",
            "tier splits",
            "future postponed/rescheduled fixture guard",
        ],
        "postponed_guard": postponed_guard,
        "provider_calls_added": 0,
        "betting_authority_changed": False,
    }
