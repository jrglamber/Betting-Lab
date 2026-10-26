from __future__ import annotations

"""Research-only tertiary FHG1 settlement fallback via ESPN public JSON feeds.

football-data.co.uk remains canonical and the existing free-settlement wrapper
remains first authority. This patch is only reached for already-frozen candidates
that are still OPEN several hours after kickoff. It uses ESPN's public scoreboard
plus per-competitor period linescores, then relies on the existing conservative
team/kickoff matcher. Canonical football-data rows later reconcile every result.

No forecasts, thresholds, prices, staking, alerts or betting authority change.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional

import requests

import first_half_goal_lab as fhg
import first_half_goal_settlement_patch as legacy
from db import sanitize_sensitive_text

VERSION = "FHG1_ESPN_SETTLEMENT_V1"
MIN_AGE_MINUTES = 180
REFRESH_MINUTES = 60
SOURCE_STATE_PREFIX = "FHG1:ESPN_SETTLEMENT_V1"
SITE_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard"
CORE_LINES_URL = (
    "https://sports.core.api.espn.com/v2/sports/soccer/leagues/{league}/events/{event}/"
    "competitions/{competition}/competitors/{competitor}/linescores"
)
LEAGUE_SLUGS = {
    "soccer_epl": "eng.1",
    "soccer_efl_champ": "eng.2",
    "soccer_england_league1": "eng.3",
    "soccer_england_league2": "eng.4",
    "soccer_spl": "sco.1",
}

_INSTALLED = False
_ORIGINAL_SETTLER = None


def _overdue(db, now: datetime):
    cutoff = now - timedelta(minutes=MIN_AGE_MINUTES)
    try:
        return db.fetchall(
            """SELECT * FROM first_half_goal_candidates
               WHERE status='OPEN' AND commence_time<?
               ORDER BY commence_time,id""",
            (cutoff.isoformat(),),
        )
    except Exception:
        return []


def _int_score(value: Any) -> Optional[int]:
    if isinstance(value, Mapping):
        for key in ("value", "score", "displayValue"):
            if key in value:
                return _int_score(value.get(key))
        return None
    try:
        return int(float(value))
    except Exception:
        return None


def _period_one(payload: Any) -> Optional[int]:
    if isinstance(payload, Mapping):
        items = payload.get("items")
        if isinstance(items, list):
            payload = items
        elif isinstance(payload.get("linescores"), list):
            payload = payload.get("linescores")
        else:
            # Some site responses expose one line-score object directly.
            period = payload.get("period") or payload.get("periodNumber")
            if str(period) == "1":
                return _int_score(payload)
            return None
    if not isinstance(payload, list):
        return None
    first_fallback = None
    for idx, raw in enumerate(payload):
        if not isinstance(raw, Mapping):
            continue
        value = _int_score(raw)
        if idx == 0 and value is not None:
            first_fallback = value
        period = raw.get("period") or raw.get("periodNumber") or raw.get("number")
        if str(period) == "1" and value is not None:
            return value
    return first_fallback


def _site_fixtures(payload: Any, league: str) -> list[dict[str, Any]]:
    events = payload.get("events") if isinstance(payload, Mapping) else []
    if not isinstance(events, list):
        return []
    out = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        status = event.get("status") if isinstance(event.get("status"), Mapping) else {}
        stype = status.get("type") if isinstance(status.get("type"), Mapping) else {}
        completed = bool(stype.get("completed")) or str(stype.get("state") or "").lower() == "post"
        if not completed:
            continue
        comps = event.get("competitions") if isinstance(event.get("competitions"), list) else []
        if not comps or not isinstance(comps[0], Mapping):
            continue
        comp = comps[0]
        competitors = comp.get("competitors") if isinstance(comp.get("competitors"), list) else []
        home = next((x for x in competitors if isinstance(x, Mapping) and str(x.get("homeAway") or "").lower() == "home"), None)
        away = next((x for x in competitors if isinstance(x, Mapping) and str(x.get("homeAway") or "").lower() == "away"), None)
        if not home or not away:
            continue

        def team_name(row):
            team = row.get("team") if isinstance(row.get("team"), Mapping) else {}
            return str(team.get("displayName") or team.get("shortDisplayName") or team.get("name") or "").strip()

        home_name, away_name = team_name(home), team_name(away)
        if not home_name or not away_name:
            continue
        kickoff = fhg._dt(event.get("date") or comp.get("date"))
        out.append({
            "home": home_name,
            "away": away_name,
            "home_key": fhg._normalize_text(home_name),
            "away_key": fhg._normalize_text(away_name),
            "kickoff": kickoff,
            "league": league,
            "event_id": str(event.get("id") or ""),
            "competition_id": str(comp.get("id") or event.get("id") or ""),
            "home_competitor_id": str(home.get("id") or ""),
            "away_competitor_id": str(away.get("id") or ""),
            "home_embedded_lines": home.get("linescores"),
            "away_embedded_lines": away.get("linescores"),
        })
    return out


def _core_period_one(session: requests.Session, fixture: Mapping[str, Any], side: str) -> Optional[int]:
    embedded = _period_one(fixture.get(f"{side}_embedded_lines"))
    if embedded is not None:
        return embedded
    competitor = str(fixture.get(f"{side}_competitor_id") or "")
    event = str(fixture.get("event_id") or "")
    competition = str(fixture.get("competition_id") or event)
    league = str(fixture.get("league") or "")
    if not competitor or not event or not competition or not league:
        return None
    response = session.get(
        CORE_LINES_URL.format(
            league=league,
            event=event,
            competition=competition,
            competitor=competitor,
        ),
        params={"limit": 20},
        timeout=20,
        headers={"User-Agent": "Project-Exit-Plan-Betting-Lab/FHG1-settlement"},
    )
    response.raise_for_status()
    return _period_one(response.json() if response.content else {})


def _settle_from_espn(db, now: datetime) -> Dict[str, Any]:
    rows = _overdue(db, now)
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        sport = str(row.get("sport_key") or "")
        league = LEAGUE_SLUGS.get(sport)
        ko = fhg._dt(row.get("commence_time"))
        if not league or ko is None:
            continue
        day = ko.astimezone(fhg.LONDON).date().isoformat()
        grouped.setdefault((sport, day), []).append(row)

    out = {"open_overdue": len(rows), "groups": len(grouped), "attempted": 0, "ok": 0, "failed": 0,
           "settled": 0, "exact": 0, "alias": 0, "unmatched": 0, "no_halftime": 0}
    if not grouped:
        return out

    session = requests.Session()
    headers = {"User-Agent": "Project-Exit-Plan-Betting-Lab/FHG1-settlement"}
    score_cache: dict[tuple[str, str], tuple[Optional[int], Optional[int]]] = {}
    for (sport, day), candidates in grouped.items():
        league = LEAGUE_SLUGS[sport]
        key = f"{SOURCE_STATE_PREFIX}:{sport}:{day}"
        if not fhg._source_due(db, key, now, REFRESH_MINUTES / 60.0):
            continue
        out["attempted"] += 1
        try:
            response = session.get(
                SITE_URL.format(league=league),
                params={"dates": day.replace("-", "")},
                timeout=20,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json() if response.content else {}
            fixtures = _site_fixtures(payload, league)
            raw_events = payload.get("events") if isinstance(payload, Mapping) else []
            fhg._source_state(db, key, sport, day, now, True, len(raw_events) if isinstance(raw_events, list) else 0)

            for candidate in candidates:
                fixture, mode = legacy._match_fixture(candidate, fixtures)
                if fixture is None:
                    out["unmatched"] += 1
                    continue
                cache_key = (str(fixture.get("league")), str(fixture.get("event_id")))
                if cache_key not in score_cache:
                    try:
                        score_cache[cache_key] = (
                            _core_period_one(session, fixture, "home"),
                            _core_period_one(session, fixture, "away"),
                        )
                    except Exception:
                        score_cache[cache_key] = (None, None)
                hh, ha = score_cache[cache_key]
                if hh is None or ha is None:
                    out["no_halftime"] += 1
                    continue
                hit = 1 if int(hh) + int(ha) > 0 else 0
                changed = db.execute(
                    """UPDATE first_half_goal_candidates
                       SET status='SETTLED',ht_home=?,ht_away=?,fhg_hit=?,settled_at=?
                       WHERE id=? AND status='OPEN'""",
                    (int(hh), int(ha), hit, now.isoformat(), int(candidate["id"])),
                )
                if int(changed or 0) <= 0:
                    continue
                out["settled"] += 1
                out["exact" if mode == "exact" else "alias"] += 1
            out["ok"] += 1
        except Exception as exc:
            safe = sanitize_sensitive_text(str(exc))
            fhg._source_state(db, key, sport, day, now, False, error=safe)
            out["failed"] += 1
            print(f"FHG1_ESPN_SETTLEMENT_ERROR sport={sport} day={day} error={safe[:300]}", flush=True)

    if out["settled"] or out["failed"]:
        print(f"FHG1_ESPN_SETTLEMENT {out}", flush=True)
    return out


def install(db) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_SETTLER
    if _INSTALLED or getattr(fhg, "_espn_settlement_patch", False):
        return {"installed": True, "already_installed": True, "version": VERSION}
    _ORIGINAL_SETTLER = fhg.settle_candidates

    def settle_candidates_with_espn(db_obj, now=None):
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        settled = int(_ORIGINAL_SETTLER(db_obj, now) or 0)
        extra = _settle_from_espn(db_obj, now)
        return settled + int(extra.get("settled") or 0)

    fhg.settle_candidates = settle_candidates_with_espn
    fhg._espn_settlement_patch = True
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "minimum_age_minutes": MIN_AGE_MINUTES,
        "refresh_minutes": REFRESH_MINUTES,
        "leagues": dict(LEAGUE_SLUGS),
        "canonical_reconciliation": "football-data.co.uk",
        "betting_authority": False,
    }
