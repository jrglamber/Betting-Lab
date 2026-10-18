from __future__ import annotations

"""Timely, research-only FHG1 settlement fallback via API-Football.

football-data.co.uk remains the canonical historical/training source. This patch
only settles already-frozen FHG1 candidates when the canonical CSV has not yet
published the result. It does not create candidates, alter probabilities, or
change betting authority.
"""

from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import os
from typing import Any, Dict, List, Mapping, Optional, Tuple

import requests

import first_half_goal_lab as fhg
from db import sanitize_sensitive_text

VERSION = "FHG1_API_FOOTBALL_SETTLEMENT_V2"
BASE_URL = "https://v3.football.api-sports.io/fixtures"
MIN_AGE_MINUTES = 60
POLL_MINUTES = 30
REQUEST_TIMEOUT = 20

# Stable API-Football competition IDs for the FHG1 league universe.
API_LEAGUES = {
    "soccer_epl": 39,
    "soccer_efl_champ": 40,
    "soccer_england_league1": 41,
    "soccer_england_league2": 42,
    "soccer_spl": 179,
    "soccer_scotland_championship": 180,
    "soccer_scotland_league_one": 181,
    "soccer_scotland_league_two": 182,
}

_INSTALLED = False
_DROP_TOKENS = {"fc", "afc", "cf", "sc"}


def _season_start(day) -> int:
    return day.year if day.month >= 7 else day.year - 1


def _due(db, key: str, now: datetime) -> bool:
    row = db.fetchone(
        "SELECT last_attempt_at FROM first_half_goal_source_state WHERE source_key=?",
        (key,),
    )
    last = fhg._dt((row or {}).get("last_attempt_at"))
    return not last or (now - last).total_seconds() >= POLL_MINUTES * 60


def _record_state(
    db,
    key: str,
    sport: str,
    season: str,
    now: datetime,
    ok: bool,
    rows_seen: int = 0,
    error: str = "",
) -> None:
    db.execute(
        """
        INSERT INTO first_half_goal_source_state(
            source_key,sport_key,season_code,last_attempt_at,last_success_at,rows_seen,last_error
        ) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(source_key) DO UPDATE SET
            sport_key=excluded.sport_key,
            season_code=excluded.season_code,
            last_attempt_at=excluded.last_attempt_at,
            last_success_at=CASE WHEN excluded.last_success_at IS NOT NULL
                THEN excluded.last_success_at ELSE first_half_goal_source_state.last_success_at END,
            rows_seen=excluded.rows_seen,
            last_error=excluded.last_error
        """,
        (
            key,
            sport,
            season,
            now.isoformat(),
            now.isoformat() if ok else None,
            int(rows_seen),
            None if ok else str(error)[:800],
        ),
    )


def _team_key(value: str) -> str:
    """Conservative settlement-only team key; forecasting is untouched."""
    norm = fhg._normalize_text(str(value or ""))
    tokens = [t for t in norm.split() if t not in _DROP_TOKENS]
    return " ".join(tokens) or norm


def _team_similarity(left: str, right: str) -> float:
    a, b = _team_key(left), _team_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Common provider difference: one source appends City/Town/United/etc.
    # Require a meaningful core string before rewarding containment.
    if min(len(a), len(b)) >= 5 and (a in b or b in a):
        return 0.92
    return SequenceMatcher(None, a, b).ratio()


def _api_fixture_rows(rows: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in rows:
        teams = item.get("teams") if isinstance(item, Mapping) else None
        score = item.get("score") if isinstance(item, Mapping) else None
        fixture = item.get("fixture") if isinstance(item, Mapping) else None
        if not isinstance(teams, Mapping) or not isinstance(score, Mapping):
            continue
        home = teams.get("home")
        away = teams.get("away")
        half = score.get("halftime")
        if not isinstance(home, Mapping) or not isinstance(away, Mapping) or not isinstance(half, Mapping):
            continue
        try:
            hh = int(half.get("home"))
            ha = int(half.get("away"))
        except (TypeError, ValueError):
            continue
        home_name = str(home.get("name") or "").strip()
        away_name = str(away.get("name") or "").strip()
        if not home_name or not away_name:
            continue
        kickoff = None
        if isinstance(fixture, Mapping):
            kickoff = fhg._dt(fixture.get("date"))
        out.append(
            {
                "home": home_name,
                "away": away_name,
                "home_key": fhg._normalize_text(home_name),
                "away_key": fhg._normalize_text(away_name),
                "kickoff": kickoff,
                "ht_home": hh,
                "ht_away": ha,
            }
        )
    return out


def _match_fixture(candidate: Mapping[str, Any], fixtures: List[Mapping[str, Any]]) -> Tuple[Optional[Mapping[str, Any]], str]:
    # Exact normalized provider-name match remains the first and safest path.
    hk = str(candidate.get("home_key") or "")
    ak = str(candidate.get("away_key") or "")
    exact = [r for r in fixtures if r.get("home_key") == hk and r.get("away_key") == ak]
    if len(exact) == 1:
        return exact[0], "exact"

    cko = fhg._dt(candidate.get("commence_time"))
    ranked: List[Tuple[float, Mapping[str, Any], float, float, float]] = []
    for row in fixtures:
        rko = row.get("kickoff")
        if cko and rko:
            delta_minutes = abs((rko - cko).total_seconds()) / 60.0
            if delta_minutes > 180:
                continue
        else:
            delta_minutes = 999.0
        hs = _team_similarity(str(candidate.get("home_team") or hk), str(row.get("home") or ""))
        aas = _team_similarity(str(candidate.get("away_team") or ak), str(row.get("away") or ""))
        if hs < 0.72 or aas < 0.72:
            continue
        score = (hs + aas) / 2.0
        if delta_minutes <= 30:
            score += 0.05
        elif delta_minutes <= 90:
            score += 0.02
        ranked.append((score, row, hs, aas, delta_minutes))

    if not ranked:
        return None, "unmatched"
    ranked.sort(key=lambda x: x[0], reverse=True)
    best = ranked[0]
    # Require strong two-team agreement and enough separation from an alternate
    # fixture so a fuzzy alias can never silently settle an ambiguous match.
    if best[0] < 0.82 or (len(ranked) > 1 and best[0] - ranked[1][0] < 0.08):
        return None, "ambiguous"
    return best[1], "alias"


def _settle_from_api(db, now: datetime) -> int:
    api_key = str(os.getenv("API_FOOTBALL_KEY") or "").strip()
    if not api_key:
        print("FHG1_API_FOOTBALL_DIAG status=no_api_key", flush=True)
        return 0

    cutoff = now - timedelta(minutes=MIN_AGE_MINUTES)
    open_rows = db.fetchall(
        """
        SELECT * FROM first_half_goal_candidates
        WHERE status='OPEN' AND commence_time<?
        ORDER BY commence_time,id
        """,
        (cutoff.isoformat(),),
    )
    if not open_rows:
        return 0

    grouped: Dict[tuple[str, str], List[Mapping[str, Any]]] = {}
    unsupported = 0
    for row in open_rows:
        sport = str(row.get("sport_key") or "")
        if sport not in API_LEAGUES:
            unsupported += 1
            continue
        ko = fhg._dt(row.get("commence_time"))
        if not ko:
            continue
        day = ko.astimezone(fhg.LONDON).date().isoformat()
        grouped.setdefault((sport, day), []).append(row)

    settled = 0
    headers = {
        "x-apisports-key": api_key,
        "User-Agent": "Project-Exit-Plan-Betting-Lab/FHG1-settlement",
    }
    for (sport, day_text), candidates in grouped.items():
        key = f"FHG1:API_FOOTBALL_SETTLEMENT:{sport}:{day_text}"
        if not _due(db, key, now):
            continue
        league_id = API_LEAGUES[sport]
        day = datetime.fromisoformat(day_text).date()
        season = str(_season_start(day))
        try:
            response = requests.get(
                BASE_URL,
                params={
                    "league": league_id,
                    "season": season,
                    "date": day_text,
                    "timezone": "Europe/London",
                },
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json() if response.content else {}
            errors = payload.get("errors") if isinstance(payload, dict) else None
            if errors:
                raise RuntimeError(f"API-Football errors: {sanitize_sensitive_text(errors, (api_key,))}")
            rows = payload.get("response") if isinstance(payload, dict) else []
            rows = rows if isinstance(rows, list) else []
            fixtures = _api_fixture_rows(rows)
            _record_state(db, key, sport, season, now, True, len(rows))

            matched_exact = 0
            matched_alias = 0
            unmatched: List[str] = []
            for candidate in candidates:
                fixture, mode = _match_fixture(candidate, fixtures)
                if fixture is None:
                    if len(unmatched) < 8:
                        unmatched.append(
                            f"{candidate.get('home_team')} v {candidate.get('away_team')}[{mode}]"
                        )
                    continue
                hh = int(fixture["ht_home"])
                ha = int(fixture["ht_away"])
                hit = 1 if hh + ha > 0 else 0
                db.execute(
                    """
                    UPDATE first_half_goal_candidates
                    SET status='SETTLED',ht_home=?,ht_away=?,fhg_hit=?,settled_at=?
                    WHERE id=? AND status='OPEN'
                    """,
                    (hh, ha, hit, now.isoformat(), candidate["id"]),
                )
                settled += 1
                if mode == "exact":
                    matched_exact += 1
                else:
                    matched_alias += 1

            print(
                "FHG1_API_FOOTBALL_DIAG "
                f"sport={sport} day={day_text} candidates={len(candidates)} "
                f"api_rows={len(rows)} usable={len(fixtures)} exact={matched_exact} "
                f"alias={matched_alias} unmatched={len(candidates)-matched_exact-matched_alias} "
                f"examples={unmatched}",
                flush=True,
            )
        except Exception as exc:
            safe = sanitize_sensitive_text(str(exc), (api_key,))
            _record_state(db, key, sport, season, now, False, 0, safe)
            print(f"FHG1_API_FOOTBALL_ERROR sport={sport} day={day_text} error={safe[:300]}", flush=True)

    if unsupported:
        print(f"FHG1_API_FOOTBALL_DIAG unsupported_open={unsupported}", flush=True)
    if settled:
        print(f"FHG1_API_FOOTBALL_SETTLED count={settled}", flush=True)
    return settled


def install(db) -> Dict[str, Any]:
    global _INSTALLED
    fhg.ensure_schema(db)
    if _INSTALLED or getattr(fhg, "_api_football_settlement_patch", False):
        return {"installed": True, "already_installed": True, "version": VERSION}

    original = fhg.settle_candidates

    def settle_candidates_with_fallback(db, now=None):
        now = now or datetime.now(timezone.utc)
        primary = int(original(db, now) or 0)
        fallback = int(_settle_from_api(db, now) or 0)
        return primary + fallback

    fhg.settle_candidates = settle_candidates_with_fallback
    fhg._api_football_settlement_patch = True
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "poll_minutes": POLL_MINUTES,
        "minimum_age_minutes": MIN_AGE_MINUTES,
        "matching": "exact_then_conservative_alias_plus_kickoff",
        "training_source_unchanged": True,
        "betting_authority": False,
    }
