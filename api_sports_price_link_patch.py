from __future__ import annotations

"""Conservative API-Sports -> Odds-API event/price linkage for PRED1.

NCAAF needs stricter identity matching than timestamp matching because the
API-Sports feed sometimes supplies placeholder 04:00 UTC kickoff times while
the Odds API later carries the real scheduled kickoff many hours later.

V2 therefore:
- keeps exact/prefix-equivalent home AND away school-name matching;
- keeps the original 12-hour match window as the first choice;
- permits a wider <=24-hour NCAAF fallback only when exactly one upcoming
  Odds-API fixture matches both teams;
- refuses to freeze a shadow price after the actual Odds-API kickoff;
- revisits existing OPEN forecasts on every maintenance run.

Research-only: this creates/updates shadow links and shadow bets only.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple

import api_sports_predictive as core
from db import utc_now_iso

VERSION = "APISPORTS_PRICE_LINK_V2_NCAAF_KICKOFF_DRIFT"
STRICT_WINDOW_HOURS = 12.0
NCAAF_FALLBACK_WINDOW_HOURS = 24.0
_INSTALLED = False
_ORIGINAL_MATCH = None
_ORIGINAL_MAKE = None


def _school_match(provider_name: Any, odds_name: Any) -> bool:
    a = core._norm(provider_name)
    b = core._norm(odds_name)
    if not a or not b:
        return False
    if a == b:
        return True
    # Mascot/suffix expansion only. Requiring the shorter normalized school
    # name to be a substantial prefix avoids loose fuzzy matching.
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= 5 and longer.startswith(shorter)


def _ncaaf_match_detail(db, lane, game: Mapping[str, Any]) -> Tuple[Optional[str], str]:
    commence = core._parse_dt(game.get("commence_time"))
    if not commence:
        return None, "invalid_provider_kickoff"

    rows = db.fetchall(
        """SELECT event_id,home_team,away_team,commence_time
           FROM multisport_events
           WHERE sport_key=? AND status='UPCOMING'""",
        (lane.odds_sport_key,),
    )

    named = []
    for row in rows:
        if not _school_match(game.get("home_team"), row.get("home_team")):
            continue
        if not _school_match(game.get("away_team"), row.get("away_team")):
            continue
        dt = core._parse_dt(row.get("commence_time"))
        if not dt:
            continue
        hours = abs((dt - commence).total_seconds()) / 3600.0
        named.append((hours, dt, str(row.get("event_id") or "")))

    if not named:
        return None, "no_team_match"

    named.sort(key=lambda item: (item[0], item[2]))
    strict = [item for item in named if item[0] <= STRICT_WINDOW_HOURS]
    if strict:
        return strict[0][2], "strict"

    # API-Sports sometimes emits a generic 04:00 UTC timestamp for NCAAF
    # Saturday fixtures. A unique two-team match within one day is safe enough
    # to repair that timestamp drift without opening fuzzy identity matching.
    fallback = [item for item in named if item[0] <= NCAAF_FALLBACK_WINDOW_HOURS]
    if len(fallback) == 1:
        return fallback[0][2], "unique_24h_team_match"

    return None, "ambiguous_or_outside_24h"


def _match_event(db, lane, game: Mapping[str, Any]) -> Optional[str]:
    if lane.key != "NCAAF":
        return _ORIGINAL_MATCH(db, lane, game)
    event_id, _ = _ncaaf_match_detail(db, lane, game)
    return event_id


def _actual_event(db, event_id: str) -> Mapping[str, Any]:
    return db.fetchone(
        """SELECT event_id,home_team,away_team,commence_time,status
           FROM multisport_events WHERE event_id=?""",
        (event_id,),
    ) or {}


def _actual_selections(db, event_id: str, forecast: Mapping[str, Any]):
    event = _actual_event(db, event_id)
    home_sel = str(event.get("home_team") or forecast.get("home_team") or "")
    away_sel = str(event.get("away_team") or forecast.get("away_team") or "")
    return (
        (home_sel, float(forecast.get("home_probability") or 0.0)),
        (away_sel, float(forecast.get("away_probability") or 0.0)),
    )


def _event_is_future(db, event_id: str) -> bool:
    event = _actual_event(db, event_id)
    dt = core._parse_dt(event.get("commence_time"))
    if not dt:
        return False
    return dt > datetime.now(timezone.utc)


def _backfill(db, lane) -> Dict[str, int]:
    linked = bets = strict_links = drift_links = skipped_started = 0
    forecasts = db.fetchall(
        """SELECT * FROM api_sports_pred_forecasts
           WHERE model_name=? AND status='OPEN'
           ORDER BY commence_time,id""",
        (lane.model_name,),
    )
    for raw in forecasts:
        f = dict(raw)
        event_id = str(f.get("odds_event_id") or "")
        mode = "existing"

        if not event_id:
            if lane.key == "NCAAF":
                event_id, mode = _ncaaf_match_detail(db, lane, f)
                event_id = event_id or ""
            else:
                event_id = _match_event(db, lane, f) or ""

            if event_id:
                db.execute(
                    """UPDATE api_sports_pred_forecasts
                       SET odds_event_id=?
                       WHERE id=? AND (odds_event_id IS NULL OR odds_event_id='')""",
                    (event_id, int(f["id"])),
                )
                linked += 1
                strict_links += int(mode == "strict")
                drift_links += int(mode == "unique_24h_team_match")

        if not event_id:
            continue

        # Never freeze a price after the real Odds-API kickoff. This is
        # especially important when the provider forecast carried a stale
        # placeholder kickoff.
        if not _event_is_future(db, event_id):
            skipped_started += 1
            continue

        for selection, probability in _actual_selections(db, event_id, f):
            if not selection or probability <= 0.0:
                continue
            quote = core._latest_best_price(db, event_id, selection)
            if not quote:
                continue
            book, offered = quote
            edge = (probability * offered - 1.0) * 100.0
            if edge < core.EDGE_THRESHOLD_PCT:
                continue
            changed = db.execute(
                """INSERT INTO api_sports_pred_shadow_bets(
                       forecast_id,model_name,created_at,odds_event_id,selection,bookmaker_key,
                       offered_odds,fair_probability,fair_odds,edge_pct,status
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(forecast_id,selection) DO NOTHING""",
                (
                    int(f["id"]), lane.model_name, utc_now_iso(), event_id, selection,
                    book, float(offered), probability, core._prob_to_odds(probability), edge, "OPEN",
                ),
            )
            bets += int(changed > 0)

    return {
        "price_links_backfilled": linked,
        "ncaaf_strict_links_backfilled": strict_links,
        "ncaaf_kickoff_drift_links_backfilled": drift_links,
        "shadow_bets_backfilled": bets,
        "post_kickoff_price_freezes_blocked": skipped_started,
    }


def _make_forecasts(db, lane):
    out = dict(_ORIGINAL_MAKE(db, lane))
    backfill = _backfill(db, lane)
    out.update(backfill)
    out["shadow_bets_created"] = int(out.get("shadow_bets_created") or 0) + int(
        backfill["shadow_bets_backfilled"]
    )
    return out


def install() -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_MATCH, _ORIGINAL_MAKE
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}
    _ORIGINAL_MATCH = core._match_odds_event
    _ORIGINAL_MAKE = core.make_forecasts
    core._match_odds_event = _match_event
    core.make_forecasts = _make_forecasts
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "ncaaf_match": (
            "exact/prefix home+away; <=12h strict, otherwise one unique <=24h "
            "two-team match for provider kickoff drift"
        ),
        "revisit_open_forecasts": True,
        "post_kickoff_price_freeze_block": True,
        "research_only": True,
    }
