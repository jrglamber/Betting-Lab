from __future__ import annotations

"""Challenge ZIP finalisation: XCOMP risk gate and record-count integrity.

No model inference, selection, provider calls, settlement, orders or alerts.
XCOMP outputs are unvalidated cross-league forecasts: never actionable by themselves.
"""

import csv
import io
import json
import math
from collections import Counter
from typing import Any

VERSION = "XCOMP_EXPORT_RESEARCH_SAFETY_V1"


def read_csv(blob: bytes):
    return list(csv.DictReader(io.StringIO(blob.decode("utf-8-sig", errors="replace")))) if blob else []


def write_csv(rows):
    if not rows:
        return b""
    fields = list(dict.fromkeys(str(k) for r in rows for k in r))
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode()


def number(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def signature(row):
    p = number(row.get("point"))
    return (
        str(row.get("event_id") or "").strip(),
        str(row.get("market_key") or "").strip().lower(),
        str(row.get("selection") or "").strip().lower(),
        round(p, 4) if p is not None else None,
    )


def enhance_export(files: dict[str, bytes]) -> dict[str, bytes]:
    candidates = read_csv(files.get("challenge_candidates.csv", b""))
    support = read_csv(files.get("challenge_model_support.csv", b""))
    xcomp_models = {
        str(r.get("event_id") or ""): r for r in support
        if str(r.get("model") or "") == "XCOMP1"
    }
    core = {signature(r): r for r in candidates if r.get("source") == "CORE"}
    audit = []
    for r in candidates:
        if r.get("source") != "XCOMP1_RESEARCH":
            continue
        r.update({
            "challenge_status": "RESEARCH_ONLY",
            "challenge_lane": "XCOMP_UNVALIDATED_CROSS_LEAGUE",
            "challenge_stake_gbp": "0.0",
            "challenge_note": "Unvalidated relative domestic league strength; no validated executable odds/ROI/CLV",
            "xcomp_research_gate": "BLOCKED_PENDING_CALIBRATION_AND_PRICE",
            "manual_current_price_required": "True",
            "min_odds": "",
        })
        model = xcomp_models.get(str(r.get("event_id") or ""), {})
        h = number(model.get("home_sample"))
        a = number(model.get("away_sample"))
        p = number(r.get("fair_probability"))
        reference = core.get(signature(r), {})
        q = number(reference.get("fair_probability"))
        diff = (p - q) * 100 if p is not None and q is not None else None
        flags = ["CROSS_LEAGUE_UNCALIBRATED", "NO_EXECUTABLE_PRICE"]
        if h is None or a is None:
            flags.append("UNKNOWN_SAMPLE")
        elif min(h, a) < 10:
            flags.append("THIN_TEAM_SAMPLE")
        if diff is not None and abs(diff) >= 15:
            flags.append("SEVERE_DIFFERENCE_VS_CORE")
        elif diff is not None and abs(diff) >= 8:
            flags.append("MATERIAL_DIFFERENCE_VS_CORE")
        r["xcomp_quality_flags"] = "|".join(flags)
        audit.append({
            "event_id": r.get("event_id"), "kickoff": r.get("commence_time"),
            "competition": r.get("league"), "home_team": r.get("home_team"),
            "away_team": r.get("away_team"), "market": r.get("market_key"),
            "selection": r.get("selection"), "point": r.get("point"),
            "xcomp_probability": p, "core_reference_probability": q,
            "difference_vs_core_pp": round(diff, 3) if diff is not None else None,
            "home_sample": h, "away_sample": a,
            "quality_flags": "|".join(flags),
            "status": "RESEARCH_ONLY_NO_STAKING", "provider_calls_added": 0,
        })
    appearances = Counter(signature(r) for r in candidates)
    by_source = {}
    for r in candidates:
        source = str(r.get("source") or "")
        by_source.setdefault(source, []).append(r)
    source_metrics = {
        name: {
            "rows": len(rows),
            "distinct_selections": len({signature(r) for r in rows}),
            "repeat_appearances": len(rows) - len({signature(r) for r in rows}),
        }
        for name, rows in sorted(by_source.items())
    }
    alternative_outcomes = {}
    for r in candidates:
        event, market, selection, point = signature(r)
        alternative_outcomes.setdefault((event, market, point), set()).add(selection)
    integrity = {
        "raw_candidate_rows": len(candidates),
        "distinct_economic_selections": len(appearances),
        "repeat_appearances": sum(max(0, n - 1) for n in appearances.values()),
        "event_market_lines_with_alternative_outcomes": sum(len(outcomes) > 1 for outcomes in alternative_outcomes.values()),
        "source_breakdown": source_metrics,
        "note": "Alternative outcomes and repeated quote/source rows are not independent bets; raw evidence is retained.",
    }
    summary = json.loads(files.get("challenge_summary.json", b"{}"))
    manifest = json.loads(files.get("manifest.json", b"{}"))
    summary.update({
        "candidate_rows": len(candidates),
        "model_support_rows": len(support),
        "candidate_sources": dict(sorted(Counter(str(r.get("source") or "") for r in candidates).items())),
        "model_sources": dict(sorted(Counter(str(r.get("model") or "") for r in support).items())),
        "xcomp_quality_audit_file": "xcomp_quality_audit.csv",
        "xcomp_candidates_research_only": len(audit),
        "challenge_market_integrity_file": "challenge_market_integrity.json",
        "market_integrity": integrity,
        "xcomp_safety_version": VERSION,
    })
    files["challenge_candidates.csv"] = write_csv(candidates)
    files["challenge_summary.json"] = json.dumps(summary, ensure_ascii=False, indent=2).encode()
    files["xcomp_quality_audit.csv"] = write_csv(audit)
    files["challenge_market_integrity.json"] = json.dumps(integrity, indent=2).encode()
    if isinstance(manifest.get("row_counts"), dict):
        manifest["row_counts"]["challenge_candidates"] = len(candidates)
        manifest["row_counts"]["challenge_model_support"] = len(support)
    for name in ("xcomp_quality_audit.csv", "challenge_market_integrity.json"):
        if name not in manifest.get("files", []):
            manifest.setdefault("files", []).append(name)
    manifest["xcomp_safety_version"] = VERSION
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    files["README.txt"] = files.get("README.txt", b"") + (
        "\nXCOMP risk audit (4 Nov): XCOMP remains RESEARCH_ONLY, with no price,\n"
        "validated cross-league calibration or staking authority. xcomp_quality_audit.csv\n"
        "flags thin history and disagreements with same-event/market CORE estimates.\n"
        "challenge_market_integrity.json shows duplicates and alternative outcomes;\n"
        "final summary row totals now match the exported CSVs. Zero provider calls.\n"
    ).encode()
    return files
