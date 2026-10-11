from __future__ import annotations

"""Paid API-Sports access upgrade for American football PRED1.

Adds an independent NCAAF lane and forces stale historical NFL/NCAAF cached
seasons to be rediscovered so a subscription upgrade is picked up immediately.
NFL and NCAA use API-Sports' stable competition IDs (NFL=1, NCAA=2), avoiding
fragile fuzzy league-name discovery. No execution authority is added; all
forecasts and bets remain shadow research.
"""

from datetime import datetime, timezone
import re

import api_sports_predictive as core
from db import utc_now_iso

VERSION = "APISPORTS_AMFOOT_PAID_V2"
NFL_LEAGUE_ID = 1
NCAAF_LEAGUE_ID = 2


def _season_year(value) -> int | None:
    years = [int(x) for x in re.findall(r"20\d{2}", str(value or ""))]
    return max(years) if years else None


def install() -> dict:
    if not any(l.key == "NCAAF" for l in core.LANES):
        core.LANES = tuple(core.LANES) + (
            core.Lane(
                key="NCAAF",
                model_name="NCAAF-PRED1",
                base_url="https://v1.american-football.api-sports.io",
                league_search="NCAA",
                odds_sport_key="americanfootball_ncaaf",
            ),
        )

    original_matches = core._league_matches
    original_rows = core._league_rows
    original_discover = core.discover_lane

    def league_matches(lane, name: str) -> bool:
        if lane.key == "NCAAF":
            n = core._norm(name)
            return (
                n in {"ncaa", "ncaaf", "ncaafootball", "collegefootball"}
                or ("ncaa" in n and "football" in n)
                or "collegefootball" in n
            )
        return original_matches(lane, name)

    def league_rows(client, lane):
        if lane.key == "NFL":
            return client.get(lane, "leagues", {"id": NFL_LEAGUE_ID})
        if lane.key == "NCAAF":
            return client.get(lane, "leagues", {"id": NCAAF_LEAGUE_ID})
        return original_rows(client, lane)

    def discover_lane(db, client, lane):
        if lane.key not in {"NFL", "NCAAF"}:
            return original_discover(db, client, lane)

        expected_id = NFL_LEAGUE_ID if lane.key == "NFL" else NCAAF_LEAGUE_ID
        cached = db.fetchone(
            "SELECT * FROM api_sports_pred_leagues WHERE lane_key=?",
            (lane.key,),
        ) or {}
        cached_year = _season_year(cached.get("season"))
        current_year = datetime.now(timezone.utc).year
        cached_id = int(cached.get("provider_league_id") or 0)

        # A paid-plan upgrade must not remain pinned to the free-plan historical
        # season, and NCAA must never inherit an accidental fuzzy league match.
        if cached and cached_id == expected_id and cached_year is not None and cached_year >= current_year:
            return (
                cached_id,
                str(cached.get("league_name") or ("NFL" if lane.key == "NFL" else "NCAA")),
                str(cached.get("season") or current_year),
                None,
                [],
            )
        if cached:
            db.execute("DELETE FROM api_sports_pred_leagues WHERE lane_key=?", (lane.key,))

        rows = league_rows(client, lane)
        candidates = []
        for item in rows:
            for candidate in core._league_season(item):
                if int(candidate[0]) == expected_id:
                    candidates.append(candidate)
        if not candidates:
            raise RuntimeError(f"{lane.key}: exact API-Sports league id {expected_id} returned no seasons")

        league_id, league_name, season, game_rows, attempts = core._probe_accessible_season(client, lane, candidates)
        db.execute(
            """INSERT INTO api_sports_pred_leagues(lane_key,provider_league_id,league_name,season,discovered_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(lane_key) DO UPDATE SET
                 provider_league_id=excluded.provider_league_id,
                 league_name=excluded.league_name,
                 season=excluded.season,
                 discovered_at=excluded.discovered_at""",
            (lane.key, league_id, league_name, season, utc_now_iso()),
        )
        return league_id, league_name, season, game_rows, attempts

    core._league_matches = league_matches
    core._league_rows = league_rows
    core.discover_lane = discover_lane

    return {
        "installed": True,
        "version": VERSION,
        "lanes": [lane.key for lane in core.LANES],
        "exact_league_ids": {"NFL": NFL_LEAGUE_ID, "NCAAF": NCAAF_LEAGUE_ID},
        "historical_cache_reprobe": ["NFL", "NCAAF"],
        "research_only": True,
    }
