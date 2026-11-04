from __future__ import annotations

"""Read-only operations / decision-hygiene audit for the Betting Lab Challenge ZIP.

No API traffic, settlement writes, forecast revisions, auto approvals or bets.
Keeps research coverage, forecast reliability and executable edge separate.
"""
import csv
import io
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

VERSION = "CHALLENGE_OPERATIONS_AUDIT_V1_2026_11_04"
FHG_QUALIFY = 0.70
XCOMP_MIN_FORWARD_SETTLED_FOR_EVALUATION = 100
XCOMP_MIN_PER_COMPETITION = 20


def _utc(value: Any) -> Optional[datetime]:
    try:
        value = str(value or "").strip()
        if not value:
            return None
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except Exception:
        return None


def _number(value: Any) -> Optional[float]:
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except Exception:
        return None


def _rows(db, query: str, params=()):
    try:
        return [dict(r) for r in db.fetchall(query, params)]
    except Exception:
        return []


def _csv(records: list[Mapping[str, Any]]) -> bytes:
    if not records:
        return b""
    fields = list(dict.fromkeys(str(k) for r in records for k in r))
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fields, extrasaction="ignore")
    writer.writeheader()
    for record in records:
        writer.writerow({key: "" if record.get(key) is None else record.get(key) for key in fields})
    return stream.getvalue().encode()


def _fhg_diagnostics(db, now: datetime):
    from first_half_goal_espn_settlement_patch import LEAGUE_SLUGS
    from predictive_football import _normalize_text
    overdue = _rows(
        db,
        """SELECT c.*,e.status AS event_status,r.completed_at AS result_completed_at,
                  r.home_score AS ft_home,r.away_score AS ft_away
           FROM first_half_goal_candidates c
           LEFT JOIN events e ON e.event_id=c.event_id
           LEFT JOIN event_results r ON r.event_id=c.event_id
           WHERE c.status='OPEN' AND c.commence_time<?
           ORDER BY c.commence_time,c.id LIMIT 3000""",
        ((now - timedelta(hours=2)).isoformat(),),
    )
    # Only an exact historical home/away match on the same local calendar date
    # can identify a ready canonical result. Never infer a half-time score from FT.
    by_league = defaultdict(lambda: {
        "overdue_open": 0, "qualifying_overdue": 0,
        "ft_known_no_ht": 0, "canonical_ht_match": 0, "no_espn_fallback": 0,
        "age_gt_7d": 0
    })
    cases = []
    for c in overdue:
        ko = _utc(c.get("commence_time"))
        if ko is None:
            continue
        sport = str(c.get("sport_key") or "")
        league = str(c.get("league") or sport)
        age_hours = max(0.0, (now - ko).total_seconds() / 3600)
        p = _number(c.get("estimated_probability"))
        qualifying = p is not None and p >= FHG_QUALIFY
        group = by_league[(sport, league)]
        group["overdue_open"] += 1
        group["qualifying_overdue"] += int(qualifying)
        group["ft_known_no_ht"] += int(c.get("ft_home") is not None and c.get("ft_away") is not None)
        group["no_espn_fallback"] += int(sport not in LEAGUE_SLUGS)
        group["age_gt_7d"] += int(age_hours >= 168)
        canonical = False
        # All candidate timestamps are UTC; stored historical dates are UTC midnight.
        # Match local football calendar day, which is the existing FHG canonical convention.
        try:
            from zoneinfo import ZoneInfo
            day = ko.astimezone(ZoneInfo("Europe/London")).date().isoformat()
            h = _rows(
                db,
                """SELECT ht_home,ht_away FROM first_half_goal_history
                   WHERE sport_key=? AND played_at>=? AND played_at<?
                     AND home_key=? AND away_key=? LIMIT 2""",
                (sport, day + "T00:00:00+00:00",
                 (datetime.fromisoformat(day).replace(tzinfo=timezone.utc) + timedelta(days=1)).isoformat(),
                 str(c.get("home_key") or ""), str(c.get("away_key") or "")),
            )
            canonical = len(h) == 1
        except Exception:
            canonical = False
        group["canonical_ht_match"] += int(canonical)
        status = str(c.get("event_status") or "").upper()
        if status in ("POSTPONED", "CANCELLED", "CANCELED", "ABANDONED", "VOID"):
            reason = "TERMINAL_EVENT_REQUIRES_LIFECYCLE_REVIEW"
        elif canonical:
            reason = "CANONICAL_HALFTIME_EXISTS_PENDING_SETTLER"
        elif sport not in LEAGUE_SLUGS:
            reason = "NO_ESPN_FALLBACK_FOR_LEAGUE"
        elif c.get("ft_home") is not None and c.get("ft_away") is not None:
            reason = "FULLTIME_KNOWN_HALFTIME_MISSING"
        elif age_hours >= 168:
            reason = "AGED_SCORE_SOURCE_GAP"
        else:
            reason = "AWAITING_VERIFIED_HALF_TIME_SCORE"
        cases.append({
            "candidate_id": c.get("id"), "event_id": c.get("event_id"),
            "sport_key": sport, "league": league,
            "home_team": c.get("home_team"), "away_team": c.get("away_team"),
            "kickoff": c.get("commence_time"),
            "age_hours": round(age_hours, 1), "qualifying": qualifying,
            "estimated_probability": p,
            "event_status": status, "fulltime_recorded": c.get("ft_home") is not None,
            "canonical_halftime_present": canonical,
            "espn_fallback_supported": sport in LEAGUE_SLUGS,
            "triage_reason": reason,
            "action": "VERIFY_THEN_SETTLE_OR_VOID_ONLY; NEVER_ASSUME_FHG_HIT",
        })
    per_league = []
    for (sport, league), stats in sorted(by_league.items()):
        per_league.append({"sport_key": sport, "league": league, **stats})
    qualifying = sum(int(r["qualifying"]) for r in cases)
    return cases, per_league, {
        "overdue_open": len(cases), "qualifying_overdue": qualifying,
        "age_gt_7d": sum(r["age_hours"] >= 168 for r in cases),
        "canonical_ready_for_reconciliation": sum(r["canonical_halftime_present"] for r in cases),
        "full_time_known_without_verified_half": sum(r["fulltime_recorded"] for r in cases),
        "unsupported_espn_league": sum(not r["espn_fallback_supported"] for r in cases),
        "triage_reason_counts": dict(Counter(r["triage_reason"] for r in cases)),
        "settlement_policy": "Use only confirmed half-time scores from canonical CSV or verified ESPN period 1; never assume losses, hits or full-time proxies.",
        "provider_calls_added": 0,
    }


def _market_key(selection: str, market: str, home: str, away: str) -> Optional[str]:
    s = selection.strip().lower()
    m = market.strip().lower()
    if m == "h2h":
        if s == str(home or "").lower():
            return "HOME"
        if s == str(away or "").lower():
            return "AWAY"
        if s in ("draw", "tie"):
            return "DRAW"
    if m == "btts":
        return "YES" if s == "yes" else "NO" if s == "no" else None
    if m == "totals":
        return "OVER" if s == "over" else "UNDER" if s == "under" else None
    return None


def _xcomp_market_benchmark(db, now: datetime):
    rows = _rows(
        db,
        """SELECT c.*,p.created_at AS frozen_at,p.home_sample,p.away_sample
           FROM football_cross_comp_candidates c
           JOIN football_cross_comp_predictions p ON p.id=c.prediction_id
           WHERE p.commence_time>=? AND p.commence_time<=?
           ORDER BY p.commence_time,p.event_id,c.id""",
        ((now - timedelta(hours=24)).isoformat(), (now + timedelta(hours=96)).isoformat()),
    )
    result = []
    for c in rows:
        market = str(c.get("market_key") or "")
        desired = _market_key(
            str(c.get("selection") or ""), market,
            str(c.get("home_team") or ""), str(c.get("away_team") or ""),
        )
        point = _number(c.get("point"))
        freeze = _utc(c.get("frozen_at"))
        kickoff = _utc(c.get("commence_time"))
        quotes = _rows(
            db,
            """SELECT captured_at,selection,point,fair_probability,num_books
               FROM consensus_snapshots
               WHERE event_id=? AND market_key=? AND captured_at>=? AND captured_at<?
               ORDER BY captured_at""",
            (str(c.get("event_id") or ""), market,
             freeze.isoformat() if freeze else "9999-01-01T00:00:00+00:00",
             kickoff.isoformat() if kickoff else "0001-01-01T00:00:00+00:00"),
        )
        matched = []
        for q in quotes:
            if int(q.get("num_books") or 0) < 2:
                continue
            q_point = _number(q.get("point"))
            if market == "totals" and (point is None or q_point is None or abs(point-q_point)>0.01):
                continue
            if _market_key(str(q.get("selection") or ""), market, str(c.get("home_team") or ""), str(c.get("away_team") or "")) != desired:
                continue
            probability = _number(q.get("fair_probability"))
            if probability is not None and 0 < probability < 1:
                matched.append(q)
        earliest = matched[0] if matched else None
        latest = matched[-1] if matched else None
        prob = _number(c.get("fair_probability"))
        first_p = _number(earliest.get("fair_probability")) if earliest else None
        last_p = _number(latest.get("fair_probability")) if latest else None
        close_dt = _utc(latest.get("captured_at")) if latest else None
        closing_age_min = (kickoff-close_dt).total_seconds()/60 if kickoff and close_dt else None
        result.append({
            "event_id": c.get("event_id"), "competition": c.get("league"),
            "kickoff": c.get("commence_time"), "frozen_at": c.get("frozen_at"),
            "home_team": c.get("home_team"), "away_team": c.get("away_team"),
            "market": market, "selection": c.get("selection"), "point": c.get("point"),
            "xcomp_probability": prob,
            "first_postfreeze_consensus_probability": first_p,
            "last_prekickoff_consensus_probability": last_p,
            "model_minus_first_consensus_pp": round(100*(prob-first_p), 2) if prob is not None and first_p is not None else None,
            "model_minus_late_consensus_pp": round(100*(prob-last_p), 2) if prob is not None and last_p is not None else None,
            "earliest_quote_at": earliest.get("captured_at") if earliest else None,
            "latest_quote_at": latest.get("captured_at") if latest else None,
            "closing_quote_age_min": round(closing_age_min,1) if closing_age_min is not None else None,
            "benchmark_quality": (
                "NO_MATCHING_POSTFREEZE_CONSENSUS" if not matched
                else "STALE_PREKICKOFF_CONSENSUS" if closing_age_min is None or closing_age_min > 120
                else "RESEARCH_COMPARISON_NOT_EXECUTABLE_PRICE"
            ),
            "recorded_quote_count": len(matched),
            "actual_clv": None,
            "readiness": "RESEARCH_ONLY_NO_STAKING",
        })
    preds = _rows(
        db,
        """SELECT sport_key,status,brier_score,home_sample,away_sample
           FROM football_cross_comp_predictions"""
    )
    settled = [p for p in preds if str(p.get("status") or "").upper() == "SETTLED" and _number(p.get("brier_score")) is not None]
    by_competition = Counter(str(p.get("sport_key") or "") for p in settled)
    fresh_quotes = sum(r["first_postfreeze_consensus_probability"] is not None for r in result)
    return result, {
        "all_time_forecasts": len(preds),
        "settled_with_brier": len(settled),
        "mean_brier": (sum(float(p["brier_score"]) for p in settled) / len(settled)) if settled else None,
        "settled_by_competition": dict(by_competition),
        "recent_candidate_markets_checked": len(result),
        "postfreeze_consensus_comparisons": fresh_quotes,
        "recent_missing_consensus": len(result) - fresh_quotes,
        "minimum_forward_settled_for_research_evaluation": XCOMP_MIN_FORWARD_SETTLED_FOR_EVALUATION,
        "minimum_per_competition_for_research_evaluation": XCOMP_MIN_PER_COMPETITION,
        "ready_for_staking": False,
        "reason": "No calibrated cross-league strength adjustment, insufficient prospective validation, and no verified executable-price CLV.",
        "provider_calls_added": 0,
    }


DECISION_HIERARCHY = {
    "version": VERSION,
    "purpose": "Restore consistent evidence weighting in the £200-to-£2,000 manual challenge, not relax price discipline.",
    "roles": {
        "primary": [
            "Current executable bookmaker odds compared with conservative evidence-adjusted fair price",
            "Forward lane/market/odds-band ROI and CLV at actual captured prices, not raw success stories",
            "Reliable independently validated model forecast for the exact same selection and market",
        ],
        "supporting_not_automatic_veto": [
            "CLV-first prediction: useful warning/support, not automatic veto",
            "PRED1 and PRED2: strongly related models, not two independent votes",
            "PRED4: alternative model, check relevant market/odds-band sample before giving authority",
            "FHG2 and UEFA FHG proxy: shadow-only until forward calibration/settlement improves",
        ],
        "research_only_never_standalone_bet": [
            "XCOMP cross-league outputs until league-strength calibration and prospective pricing validation",
            "CORE broad execution signals without verified positive relevant lane edge",
            "Any duplicate or opposing same-event/market selection presented across rows",
        ],
    },
    "contradiction_policy": (
        "A disagreement is a finding to explain, not automatically a veto. Models sharing "
        "training data do not count as independent corroboration. Weight a conflict by "
        "model relevance, forward ROI/CLV, sample, independent data and exact market; "
        "unproven models cannot overrule established evidence merely by disagreeing."
    ),
    "decision_states": [
        "PRICE_CHECK", "SMALL_PILOT", "WATCH", "PASS_POOR_PRICE",
        "PASS_WEAK_EVIDENCE", "RESEARCH_ONLY",
    ],
    "stake_authority": "manual only; no automatic bets or stake increases",
    "profit_claim": "A winning recent run is not proof of persistent edge; also don't blanket-reject merely due research-model noise.",
}


def enhance_ops_export(db, files: dict[str, bytes]) -> dict[str, bytes]:
    now = datetime.now(timezone.utc)
    overdue, per_league, fhg_summary = _fhg_diagnostics(db, now)
    benchmark, xcomp_summary = _xcomp_market_benchmark(db, now)
    files["fhg_overdue_audit.csv"] = _csv(overdue)
    files["fhg_settlement_by_league.csv"] = _csv(per_league)
    files["fhg_settlement_audit.json"] = json.dumps(fhg_summary, indent=2).encode()
    files["xcomp_consensus_benchmark.csv"] = _csv(benchmark)
    files["xcomp_validation_status.json"] = json.dumps(xcomp_summary, indent=2).encode()
    files["challenge_decision_hierarchy.json"] = json.dumps(DECISION_HIERARCHY, indent=2).encode()

    summary = json.loads(files.get("challenge_summary.json", b"{}"))
    summary["ops_audit_version"] = VERSION
    summary["fhg_settlement_triage"] = fhg_summary
    summary["xcomp_validation"] = xcomp_summary
    summary["decision_hierarchy"] = "challenge_decision_hierarchy.json"
    files["challenge_summary.json"] = json.dumps(summary, indent=2, ensure_ascii=False).encode()
    manifest = json.loads(files.get("manifest.json", b"{}"))
    for path in (
        "fhg_overdue_audit.csv", "fhg_settlement_by_league.csv",
        "fhg_settlement_audit.json", "xcomp_consensus_benchmark.csv",
        "xcomp_validation_status.json", "challenge_decision_hierarchy.json",
    ):
        if path not in manifest.get("files", []):
            manifest.setdefault("files", []).append(path)
    manifest["ops_audit_version"] = VERSION
    files["manifest.json"] = json.dumps(manifest, indent=2).encode()
    files["README.txt"] = files.get("README.txt", b"") + (
        "\nOPERATIONS AUDIT (Nov 2026): FHG overdue scores split by league and reason;\n"
        "XCOMP market benchmarks use only pre-kickoff consensus already stored AFTER\n"
        "prediction freeze. A consensus is NOT a bookmaker execution price, CLV,\n"
        "calibration proof or staking authority. Stable decision hierarchy does not\n"
        "alter the underlying models, their prices or selection/settlement logic.\n"
    ).encode()
    return files
