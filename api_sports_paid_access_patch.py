from __future__ import annotations

"""Paid API-Sports access upgrade for American football PRED1.

Adds an independent NCAAF lane and forces stale historical NFL/NCAAF cached
seasons to be rediscovered so a subscription upgrade is picked up immediately.
No execution authority is added; all forecasts and bets remain shadow research.
"""

from datetime import datetime, timezone
import re

import api_sports_predictive as core

VERSION = "APISPORTS_AMFOOT_PAID_V1"


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
        if lane.key in {"NFL", "NCAAF"}:
            return client.get(lane, "leagues", {})
        return original_rows(client, lane)

    def discover_lane(db, client, lane):
        # The old free-plan mapping was cached for 14 days. Re-probe American
        # football whenever that cache is older than the current season so a
        # paid-plan upgrade becomes effective immediately.
        if lane.key in {"NFL", "NCAAF"}:
            cached = db.fetchone(
                "SELECT season FROM api_sports_pred_leagues WHERE lane_key=?",
                (lane.key,),
            ) or {}
            cached_year = _season_year(cached.get("season"))
            current_year = datetime.now(timezone.utc).year
            if cached_year is not None and cached_year < current_year:
                db.execute("DELETE FROM api_sports_pred_leagues WHERE lane_key=?", (lane.key,))
        return original_discover(db, client, lane)

    core._league_matches = league_matches
    core._league_rows = league_rows
    core.discover_lane = discover_lane

    return {
        "installed": True,
        "version": VERSION,
        "lanes": [lane.key for lane in core.LANES],
        "historical_cache_reprobe": ["NFL", "NCAAF"],
        "research_only": True,
    }
