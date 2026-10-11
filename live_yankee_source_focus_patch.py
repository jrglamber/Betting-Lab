from __future__ import annotations

"""Research-only Yankee source hardening and odds-pocket focus.

Fixes the forward-start blind spot by allowing still-open, still-upcoming source
selections that pre-date the Yankee feed itself.  Prices are always re-checked
live before a card can form, so this does not introduce hindsight.

The Yankee feed remains manual/shadow only.  Broad underlying collectors are not
narrowed; this patch only focuses the live-like Yankee candidate pool on the
currently most interesting descriptive odds pockets.
"""

from datetime import datetime
import json
from typing import Any, Dict, Mapping

import live_yankee_feed as feed
import live_yankee_consolidation_patch as consolidation
import web as web_module
from db import utc_now_iso

VERSION = "YANKEE_LIVE_FEED_V2_FOCUSED_BANDS"
SOURCES = (
    "PRED4",
    "FOOTBALL_4_TO_4_99",
    "NFL_2_TO_4_99",
    "NCAAF_3_TO_4_99",
    "BASKETBALL_1_50_TO_3_99",
    "BASEBALL_1_50_TO_1_99",
)

_INSTALLED = False
_ORIGINAL_SOURCE_ROWS = None
_ORIGINAL_VALID_QUOTES = None
_ORIGINAL_FRESH_QUOTE = None


def _in_band(source_lane: str, price: float) -> bool:
    lane = str(source_lane or "")
    p = float(price or 0.0)
    if lane == "FOOTBALL_4_TO_4_99":
        return 4.0 <= p < 5.0
    if lane == "NFL_2_TO_4_99":
        return 2.0 <= p < 5.0
    if lane == "NCAAF_3_TO_4_99":
        return 3.0 <= p < 5.0
    if lane == "BASKETBALL_1_50_TO_3_99":
        return 1.5 <= p < 4.0
    if lane == "BASEBALL_1_50_TO_1_99":
        return 1.5 <= p < 2.0
    return p > 1.0


def _source_rows(db, start_iso: str, now: datetime):
    # Intentionally ignore the Yankee-feed start timestamp.  A selection is
    # eligible if it is still OPEN, its event is still UPCOMING/actionable and
    # we can obtain a fresh current manual-book price.  This seeds the feed with
    # valid opportunities that existed just before the feature was switched on.
    rows = [dict(x) for x in _ORIGINAL_SOURCE_ROWS(db, "1970-01-01T00:00:00+00:00", now)]
    focused = []
    for row in rows:
        lane = str(row.get("source_lane") or "")
        price = float(row.get("source_odds") or 0.0)
        if lane == "NFL":
            if not (2.0 <= price < 5.0):
                continue
            row["source_lane"] = "NFL_2_TO_4_99"
        elif lane == "NCAAF_3_TO_4_99":
            if not (3.0 <= price < 5.0):
                continue
        # PRED4 remains model-driven rather than price-band-driven.
        focused.append(row)

    # Core football 4.00-4.99.  All existing execution shadows continue to be
    # collected elsewhere; this is only a focused Yankee source lane.
    try:
        core = db.fetchall(
            """SELECT b.id AS source_id,b.created_at AS source_created_at,b.event_id,
                      b.market_key,b.selection,b.point,b.min_odds,b.offered_odds AS source_odds,
                      b.bookmaker_key AS source_bookmaker_key,b.bookmaker_title AS source_bookmaker_title,
                      b.edge_pct,e.sport_key,e.league,e.commence_time,e.home_team,e.away_team
               FROM execution_shadow_bets b
               JOIN events e ON e.event_id=b.event_id
               WHERE b.status='OPEN' AND e.status='UPCOMING'
                 AND b.offered_odds>=4.0 AND b.offered_odds<5.0
               ORDER BY b.created_at,b.id"""
        )
    except Exception:
        core = []
    for raw in core:
        row = dict(raw)
        if not feed._actionable(row.get("commence_time"), now):
            continue
        if float(row.get("min_odds") or 0.0) <= 1.0:
            continue
        row.update(source_lane="FOOTBALL_4_TO_4_99", source_table="execution_shadow_bets")
        focused.append(row)

    # The two smaller multisport pockets stay explicitly exploratory.  They are
    # included because this feed is shadow-only; broad collection is unchanged.
    try:
        multi = db.fetchall(
            """SELECT b.id AS source_id,b.created_at AS source_created_at,b.event_id,
                      'h2h' AS market_key,b.selection,NULL AS point,b.min_odds,
                      b.offered_odds AS source_odds,b.bookmaker_key AS source_bookmaker_key,
                      b.bookmaker_title AS source_bookmaker_title,b.edge_pct,
                      e.sport_key,e.league_title AS league,e.commence_time,e.home_team,e.away_team
               FROM multisport_execution_bets b
               JOIN multisport_events e ON e.event_id=b.event_id
               WHERE b.status='OPEN' AND e.status='UPCOMING'
               ORDER BY b.created_at,b.id"""
        )
    except Exception:
        multi = []
    for raw in multi:
        row = dict(raw)
        if not feed._actionable(row.get("commence_time"), now):
            continue
        price = float(row.get("source_odds") or 0.0)
        key = str(row.get("sport_key") or "").lower()
        lane = None
        if key.startswith("basketball_") and 1.5 <= price < 4.0:
            lane = "BASKETBALL_1_50_TO_3_99"
        elif key.startswith("baseball_") and 1.5 <= price < 2.0:
            lane = "BASEBALL_1_50_TO_1_99"
        if lane is None or float(row.get("min_odds") or 0.0) <= 1.0:
            continue
        row.update(source_lane=lane, source_table="multisport_execution_bets")
        focused.append(row)

    # Dedupe identical selection/event/market/point rows while preserving the
    # earliest source ordering used by the original queue.
    seen = set()
    out = []
    for row in sorted(focused, key=lambda r: (str(r.get("source_created_at") or ""), str(r.get("source_lane") or ""), int(r.get("source_id") or 0))):
        key = feed._candidate_key(row.get("event_id"), row.get("market_key"), row.get("selection"), row.get("point"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _valid_quotes(db, candidate: Mapping[str, Any], now: datetime):
    quotes = dict(_ORIGINAL_VALID_QUOTES(db, candidate, now))
    return {
        book: row for book, row in quotes.items()
        if _in_band(str(candidate.get("source_lane") or ""), float(row.get("price") or 0.0))
    }


def _fresh_quote_for_book(db, candidate: Mapping[str, Any], bookmaker_key: str, now: datetime):
    row = _ORIGINAL_FRESH_QUOTE(db, candidate, bookmaker_key, now)
    if row is None:
        return None
    return row if _in_band(str(candidate.get("source_lane") or ""), float(row.get("price") or 0.0)) else None


def install() -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_SOURCE_ROWS, _ORIGINAL_VALID_QUOTES, _ORIGINAL_FRESH_QUOTE
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    _ORIGINAL_SOURCE_ROWS = feed._source_rows
    _ORIGINAL_VALID_QUOTES = feed._valid_quotes
    _ORIGINAL_FRESH_QUOTE = consolidation._fresh_quote_for_book

    feed._source_rows = _source_rows
    feed._valid_quotes = _valid_quotes
    consolidation._fresh_quote_for_book = _fresh_quote_for_book
    feed.FROZEN_SOURCES = SOURCES
    feed.ALGORITHM_VERSION = VERSION

    policy = {
        "sources": list(SOURCES),
        "pred4": "all current PRED4 selections",
        "football": "core football execution shadows at 4.00-4.99",
        "nfl": "Multi-Sport NFL H2H at 2.00-4.99",
        "ncaaf": "Multi-Sport NCAAF H2H at 3.00-4.99",
        "basketball": "Multi-Sport basketball H2H at 1.50-3.99 (exploratory)",
        "baseball": "Multi-Sport baseball H2H at 1.50-1.99 (exploratory)",
        "seed_policy": "all still-open actionable selections, including those created before Yankee feed start",
        "dedupe": "event+market+selection+point",
        "one_leg_per_event_per_card": True,
        "shadow_only": True,
    }
    try:
        feed._ensure_schema(web_module.db)
        web_module.db.execute(
            "UPDATE live_yankee_state SET algorithm_version=?,source_policy_json=? WHERE singleton_id=1",
            (VERSION, json.dumps(policy, sort_keys=True)),
        )
    except Exception:
        pass

    web_module.VERSION = "0.19.42"
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "sources": list(SOURCES),
        "prestart_seed_fix": True,
        "current_price_band_guard": True,
        "dashboard_version": web_module.VERSION,
    }
