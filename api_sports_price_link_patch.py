from __future__ import annotations

"""Conservative API-Sports -> Odds-API event/price linkage for PRED1.

The provider often names an NCAA team as the school only (e.g. Middle
Tennessee) while Odds-API includes the mascot (Middle Tennessee Blue Raiders).
For NCAAF only, match both home and away names by exact normalized equality or
prefix equivalence, inside the existing 12-hour kickoff guardrail.  Existing
OPEN forecasts are revisited each maintenance run so a forecast that existed
before its Odds-API event/price became available is not stranded forever.

Research-only: this creates/updates shadow links and shadow bets only.
"""

from typing import Any, Dict, Mapping, Optional

import api_sports_predictive as core
from db import utc_now_iso

VERSION = "APISPORTS_PRICE_LINK_V1"
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
    # Mascot/suffix expansion only.  Requiring the shorter normalized school
    # name to be a substantial prefix avoids loose fuzzy matching.
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= 5 and longer.startswith(shorter)


def _match_event(db, lane, game: Mapping[str, Any]) -> Optional[str]:
    if lane.key != "NCAAF":
        return _ORIGINAL_MATCH(db, lane, game)
    commence = core._parse_dt(game.get("commence_time"))
    if not commence:
        return None
    rows = db.fetchall(
        """SELECT event_id,home_team,away_team,commence_time
           FROM multisport_events
           WHERE sport_key=? AND status='UPCOMING'""",
        (lane.odds_sport_key,),
    )
    best = None
    for row in rows:
        dt = core._parse_dt(row.get("commence_time"))
        if not dt:
            continue
        hours = abs((dt - commence).total_seconds()) / 3600.0
        if hours > 12:
            continue
        if not _school_match(game.get("home_team"), row.get("home_team")):
            continue
        if not _school_match(game.get("away_team"), row.get("away_team")):
            continue
        if best is None or hours < best[0]:
            best = (hours, str(row.get("event_id") or ""))
    return best[1] if best else None


def _actual_selections(db, event_id: str, forecast: Mapping[str, Any]):
    event = db.fetchone(
        "SELECT home_team,away_team FROM multisport_events WHERE event_id=?",
        (event_id,),
    ) or {}
    home_sel = str(event.get("home_team") or forecast.get("home_team") or "")
    away_sel = str(event.get("away_team") or forecast.get("away_team") or "")
    return (
        (home_sel, float(forecast.get("home_probability") or 0.0)),
        (away_sel, float(forecast.get("away_probability") or 0.0)),
    )


def _backfill(db, lane) -> Dict[str, int]:
    linked = bets = 0
    forecasts = db.fetchall(
        """SELECT * FROM api_sports_pred_forecasts
           WHERE model_name=? AND status='OPEN'
           ORDER BY commence_time,id""",
        (lane.model_name,),
    )
    for raw in forecasts:
        f = dict(raw)
        event_id = str(f.get("odds_event_id") or "")
        if not event_id:
            event_id = _match_event(db, lane, f) or ""
            if event_id:
                db.execute(
                    "UPDATE api_sports_pred_forecasts SET odds_event_id=? WHERE id=? AND (odds_event_id IS NULL OR odds_event_id='')",
                    (event_id, int(f["id"])),
                )
                linked += 1
        if not event_id:
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
    return {"price_links_backfilled": linked, "shadow_bets_backfilled": bets}


def _make_forecasts(db, lane):
    out = dict(_ORIGINAL_MAKE(db, lane))
    backfill = _backfill(db, lane)
    out.update(backfill)
    out["shadow_bets_created"] = int(out.get("shadow_bets_created") or 0) + int(backfill["shadow_bets_backfilled"])
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
        "ncaaf_match": "same kickoff window + exact/prefix home and away school names",
        "revisit_open_forecasts": True,
        "research_only": True,
    }
