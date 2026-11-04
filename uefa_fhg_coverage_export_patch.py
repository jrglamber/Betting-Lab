from __future__ import annotations

"""UEFA Champions/Europa/Conference coverage and FHG research-only bridge.

Uses existing Odds-API event rows and existing *domestic* FHG1 half-time history.
No new provider calls, extra odds polling, FHG1 calibration claims, staking, or
betting execution. This proxy is NOT the validated domestic FHG1 model.
"""

import csv
import io
import json
import math
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, Mapping, Optional

import challenge_export_patch as base
from predictive_football import _normalize_text

VERSION = "UEFA_FHG_RESEARCH_EXPORT_V1_2026_11_04"
MODEL = "UEFA_FHG_DOMESTIC_SPLIT_PROXY_V1"
COMPETITIONS = (
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
)
LOOKBACK_DAYS = 730
MIN_HOME_SPLIT = 5
MIN_AWAY_SPLIT = 5
MIN_QUALIFIED = 0.70
PRIOR_MATCHES = 8.0
CROSS_COMP_SHRINK = 0.75
_INSTALLED = False
_ORIGINAL_BUILD = None
_RELEASE_VERSION = None


def _read_csv(data: bytes) -> list[dict[str, str]]:
    if not data:
        return []
    return [dict(r) for r in csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace")))]


def _csv(rows: list[Mapping[str, Any]]) -> bytes:
    if not rows:
        return b""
    fields = list(dict.fromkeys(str(k) for r in rows for k in r))
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: "" if r.get(k) is None else r[k] for k in fields})
    return buf.getvalue().encode("utf-8")


def _parse(value: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except Exception:
        return None


def _rows(db, sql: str, params=()):
    try:
        return [dict(r) for r in db.fetchall(sql, params)]
    except Exception:
        return []


def _count(db, table: str, key: str) -> int:
    try:
        if table == "execution_shadow_bets":
            row = db.fetchone(
                "SELECT COUNT(*) AS n FROM execution_shadow_bets b JOIN events e ON e.event_id=b.event_id WHERE e.sport_key=?",
                (key,),
            )
        else:
            row = db.fetchone(f"SELECT COUNT(*) AS n FROM {table} WHERE sport_key=?", (key,))
        return int((row or {}).get("n") or 0)
    except Exception:
        return 0


def _resolve_team(name: str, known_keys: set[str]) -> tuple[Optional[str], str]:
    normalized = _normalize_text(name)
    if normalized in known_keys:
        return normalized, "EXACT"
    if not normalized or len(normalized) < 5:
        return None, "UNMAPPED"
    # Restrict fuzzy matches to high-confidence, clearly unambiguous names.
    nearest = sorted(
        ((SequenceMatcher(None, normalized, k).ratio(), k) for k in known_keys),
        reverse=True,
    )[:2]
    if nearest and nearest[0][0] >= 0.91 and (
        len(nearest) == 1 or nearest[0][0] - nearest[1][0] >= 0.06
    ):
        return nearest[0][1], "FUZZY_HIGH_CONFIDENCE"
    return None, "UNMAPPED"


def _split(db, key: str, home: bool, kickoff: datetime) -> tuple[int, int]:
    col = "home_key" if home else "away_key"
    row = db.fetchone(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(fhg_hit),0) AS hits
            FROM first_half_goal_history
            WHERE {col}=? AND played_at<? AND played_at>=?""",
        (key, kickoff.isoformat(), (kickoff - timedelta(days=LOOKBACK_DAYS)).isoformat()),
    )
    return int((row or {}).get("n") or 0), int((row or {}).get("hits") or 0)


def _reference_rate(db, now: datetime) -> tuple[float, int]:
    try:
        row = db.fetchone(
            "SELECT COUNT(*) AS n, COALESCE(SUM(fhg_hit),0) AS hits FROM first_half_goal_history WHERE played_at<? AND played_at>=?",
            (now.isoformat(), (now - timedelta(days=LOOKBACK_DAYS)).isoformat()),
        )
        n = int((row or {}).get("n") or 0)
        return (max(0.55, min(0.85, int(row["hits"]) / n)), n) if n >= 100 else (0.70, n)
    except Exception:
        return 0.70, 0


def _build_research(db, now: datetime, horizon_hours: int):
    end = now + timedelta(hours=horizon_hours)
    raw_events = _rows(
        db,
        "SELECT event_id,sport_key,league,commence_time,home_team,away_team,status FROM events WHERE sport_key IN (?,?,?)",
        COMPETITIONS,
    )
    future = [
        e for e in raw_events
        if str(e.get("status") or "").upper() == "UPCOMING"
        and (kickoff := _parse(e.get("commence_time"))) is not None
        and now < kickoff <= end
    ]
    future.sort(key=lambda x: (str(x.get("commence_time") or ""), str(x.get("event_id") or "")))
    half_history_names = _rows(
        db,
        "SELECT DISTINCT home_key AS team_key FROM first_half_goal_history UNION SELECT DISTINCT away_key AS team_key FROM first_half_goal_history",
    )
    known = {str(r.get("team_key") or "") for r in half_history_names if r.get("team_key")}
    prior, prior_n = _reference_rate(db, now)
    decisions: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    support: list[dict[str, Any]] = []

    for e in future:
        kickoff = _parse(e["commence_time"])
        home_key, home_method = _resolve_team(str(e.get("home_team") or ""), known)
        away_key, away_method = _resolve_team(str(e.get("away_team") or ""), known)
        hn = hh = an = ah = 0
        if home_key:
            try:
                hn, hh = _split(db, home_key, True, kickoff)
            except Exception:
                pass
        if away_key:
            try:
                an, ah = _split(db, away_key, False, kickoff)
            except Exception:
                pass
        probability = None
        reason = "ELIGIBLE"
        if not home_key or not away_key:
            reason = "UNMAPPED_DOMESTIC_TEAM"
        elif hn < MIN_HOME_SPLIT or an < MIN_AWAY_SPLIT:
            reason = "INSUFFICIENT_DOMESTIC_HALF_HISTORY"
        elif prior_n < 100:
            reason = "INSUFFICIENT_BASELINE_HISTORY"
        else:
            hp = (hh + PRIOR_MATCHES * prior) / (hn + PRIOR_MATCHES)
            ap = (ah + PRIOR_MATCHES * prior) / (an + PRIOR_MATCHES)
            # Cross-competition styles are not calibrated. Shrink to broad prior.
            probability = max(0.01, min(0.99, prior + CROSS_COMP_SHRINK * ((hp + ap) / 2.0 - prior)))
            if probability < MIN_QUALIFIED:
                reason = "BELOW_RESEARCH_THRESHOLD"
        entry = {
            "event_id": e.get("event_id"), "commence_time": e.get("commence_time"),
            "sport_key": e.get("sport_key"), "league": e.get("league"),
            "home_team": e.get("home_team"), "away_team": e.get("away_team"),
            "home_mapping": home_method, "away_mapping": away_method,
            "home_split_n": hn, "home_split_hits": hh,
            "away_split_n": an, "away_split_hits": ah,
            "reference_half_goal_rate": round(prior, 6),
            "estimated_probability": round(probability, 6) if probability is not None else None,
            "research_status": reason,
            "model_version": MODEL, "calibration": "UNVALIDATED_CROSS_COMP_PROXY",
            "price_source": "NONE", "betting_authority": False,
        }
        decisions.append(entry)
        if probability is None:
            continue
        support.append({
            "model": MODEL, "source_table": "uefa_fhg_research_export",
            "event_id": e.get("event_id"), "commence_time": e.get("commence_time"),
            "sport_key": e.get("sport_key"), "league": e.get("league"),
            "home_team": e.get("home_team"), "away_team": e.get("away_team"),
            "estimated_probability": round(probability, 6), "evidence_mode": "RESEARCH_UNVALIDATED",
            "status": "OPEN", "model_version": MODEL,
        })
        if reason != "ELIGIBLE":
            continue
        candidates.append({
            "source": "UEFA_FHG_RESEARCH", "source_table": "uefa_fhg_research_export",
            "source_id": e.get("event_id"), "event_id": e.get("event_id"),
            "commence_time": e.get("commence_time"), "sport_key": e.get("sport_key"),
            "league": e.get("league"), "home_team": e.get("home_team"),
            "away_team": e.get("away_team"), "market_key": "fhg_over_0_5",
            "selection": "Over", "point": 0.5, "bookmaker_key": None, "bookmaker_title": None,
            "offered_odds": None, "odds_band": None, "fair_probability": round(probability, 6),
            "fair_odds": None, "edge_pct": None, "min_odds": None, "status": "OPEN",
            "notes": "UEFA domestic-half-time proxy, not calibrated or priced; manual research only.",
            "challenge_lane": "UEFA_FHG_UNVALIDATED", "challenge_status": "RESEARCH_ONLY",
            "challenge_stake_gbp": 0.0, "challenge_note": "Do not stake from this proxy alone.",
            "challenge_phase": "200_TO_2000_2026_11_03", "manual_current_price_required": True,
            "clv_first_evidence_role": "NO_OBSERVATION", "export_price_vs_minimum": "UNPRICED",
        })
    return future, decisions, candidates, support, prior_n


def build_uefa_coverage_export(db, settings, app_version: str):
    raw, filename = _ORIGINAL_BUILD(db, settings, str(_RELEASE_VERSION or app_version))
    with zipfile.ZipFile(io.BytesIO(raw), "r") as z:
        files = {name: z.read(name) for name in z.namelist()}
    now = datetime.now(timezone.utc)
    horizon = int(getattr(base, "HORIZON_HOURS", 48))
    events, evaluations, candidates, support, prior_n = _build_research(db, now, horizon)
    existing = _read_csv(files.get("challenge_candidates.csv", b""))
    keys = {(str(r.get("event_id") or ""), str(r.get("market_key") or ""), str(r.get("source") or "")) for r in existing}
    for r in candidates:
        key = (str(r["event_id"]), str(r["market_key"]), str(r["source"]))
        if key not in keys:
            existing.append(r)
            keys.add(key)
    files["challenge_candidates.csv"] = _csv(existing)
    files["challenge_model_support.csv"] = _csv(_read_csv(files.get("challenge_model_support.csv", b"")) + support)
    files["uefa_fhg_research.csv"] = _csv(evaluations)

    inventory = []
    for sport in COMPETITIONS:
        fixtures = [e for e in events if e.get("sport_key") == sport]
        items = [e for e in evaluations if e.get("sport_key") == sport]
        inventory.append({
            "sport_key": sport, "upcoming_48h": len(fixtures),
            "core_event_records": _count(db, "execution_shadow_bets", sport),
            "existing_fhg1_candidates": _count(db, "first_half_goal_candidates", sport),
            "existing_xcomp_predictions": _count(db, "football_cross_comp_predictions", sport),
            "uefa_fhg_proxy_evaluated": len(items),
            "uefa_fhg_proxy_unpriced_candidates": sum(e["research_status"] == "ELIGIBLE" for e in items),
            "unmapped_or_insufficient": sum(e["research_status"] in ("UNMAPPED_DOMESTIC_TEAM", "INSUFFICIENT_DOMESTIC_HALF_HISTORY") for e in items),
            "provider_calls_added": 0,
        })
    files["uefa_coverage_diagnostics.csv"] = _csv(inventory)
    summary = json.loads(files.get("challenge_summary.json", b"{}").decode("utf-8"))
    summary.update({
        "uefa_coverage_version": VERSION,
        "uefa_competitions": list(COMPETITIONS),
        "uefa_fhg_model": MODEL,
        "uefa_fhg_evidence": "UNVALIDATED_RESEARCH_ONLY",
        "uefa_fhg_proxy_candidate_count": len(candidates),
        "uefa_fhg_domestic_history_count": prior_n,
        "uefa_fhg_research_file": "uefa_fhg_research.csv",
        "uefa_coverage_diagnostics_file": "uefa_coverage_diagnostics.csv",
        "uefa_additional_provider_calls": 0,
    })
    files["challenge_summary.json"] = json.dumps(summary, indent=2, ensure_ascii=False).encode()
    manifest = json.loads(files.get("manifest.json", b"{}").decode("utf-8"))
    for path in ("uefa_fhg_research.csv", "uefa_coverage_diagnostics.csv"):
        if path not in manifest.get("files", []):
            manifest.setdefault("files", []).append(path)
    manifest["uefa_coverage_version"] = VERSION
    files["manifest.json"] = json.dumps(manifest, indent=2, ensure_ascii=False).encode()
    files["README.txt"] = files.get("README.txt", b"") + (
        "\nUEFA COVERAGE (4 NOV 2026): Champions, Europa and Conference League remain in normal odds/core research.\n"
        "uefa_coverage_diagnostics.csv shows each competition's observed coverage.\n"
        "uefa_fhg_research.csv screens UEFA first-half goals from stored domestic HT histories;\n"
        "missing/unmapped fixtures carry explicit reasons. Valid proxy rows join challenge_candidates.csv\n"
        "and challenge_model_support.csv as RESEARCH_ONLY with zero stake, no live quote and no validated ROI.\n"
        "Not FHG1, not eligible for FHG1 acca formation, no added paid API calls or betting authority.\n"
    ).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name, value in files.items():
            z.writestr(name, value)
    return out.getvalue(), filename


def install(release_version: str) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_BUILD, _RELEASE_VERSION
    _RELEASE_VERSION = str(release_version)
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}
    _ORIGINAL_BUILD = base.build_challenge_export
    base.build_challenge_export = build_uefa_coverage_export
    base.VERSION = VERSION
    _INSTALLED = True
    return {
        "installed": True, "version": VERSION,
        "uefa_competitions": COMPETITIONS, "provider_calls_added": 0,
        "selection_changes": False, "betting_authority": False,
    }
