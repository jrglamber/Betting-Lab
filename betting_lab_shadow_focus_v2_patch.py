from __future__ import annotations

"""Final shadow-only tightening for the 11 Oct 2026 Betting Lab review.

- Restrict the exploratory basketball Yankee pocket to EuroLeague 1.50-3.99.
- Keep the focused football/NFL/NCAAF/baseball/PRED4 lanes unchanged.
- Backfill the already-stored NFL/EuroLeague historical games through the
  strict chronological PRED1 walk-forward immediately at web startup, so the
  research dashboard does not wait for the next six-hour API-Sports cron.

No live bet placement, staking or underlying collector authority is changed.
"""

import json
from typing import Any, Dict

import api_sports_predictive as api_pred
import api_sports_walkforward as walkforward
import live_yankee_feed as feed
import live_yankee_source_focus_patch as focus
import web as web_module

VERSION = "YANKEE_LIVE_FEED_V2_1_FOCUSED_BANDS"
SOURCES = (
    "PRED4",
    "FOOTBALL_4_TO_4_99",
    "NFL_2_TO_4_99",
    "NCAAF_3_TO_4_99",
    "EUROLEAGUE_1_50_TO_3_99",
    "BASEBALL_1_50_TO_1_99",
)

_INSTALLED = False
_ORIGINAL_SOURCE_ROWS = None
_ORIGINAL_IN_BAND = None


def _source_rows(db, start_iso, now):
    rows = [dict(x) for x in _ORIGINAL_SOURCE_ROWS(db, start_iso, now)]
    out = []
    for row in rows:
        lane = str(row.get("source_lane") or "")
        if lane == "BASKETBALL_1_50_TO_3_99":
            if str(row.get("sport_key") or "").lower() != "basketball_euroleague":
                continue
            row["source_lane"] = "EUROLEAGUE_1_50_TO_3_99"
        out.append(row)
    return out


def _in_band(source_lane: str, price: float) -> bool:
    if str(source_lane or "") == "EUROLEAGUE_1_50_TO_3_99":
        p = float(price or 0.0)
        return 1.5 <= p < 4.0
    return bool(_ORIGINAL_IN_BAND(source_lane, price))


def _backfill_walkforward() -> Dict[str, Any]:
    db = web_module.db
    result: Dict[str, Any] = {"version": walkforward.VERSION, "lanes": {}}
    try:
        walkforward.ensure_schema(db)
        for lane in api_pred.LANES:
            try:
                result["lanes"][lane.key] = {"ok": True, **walkforward.run_lane(db, lane)}
            except Exception as exc:
                result["lanes"][lane.key] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    print("API_SPORTS_WALKFORWARD_STARTUP " + json.dumps(result, sort_keys=True), flush=True)
    return result


def install() -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_SOURCE_ROWS, _ORIGINAL_IN_BAND
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    _ORIGINAL_SOURCE_ROWS = feed._source_rows
    _ORIGINAL_IN_BAND = focus._in_band
    feed._source_rows = _source_rows
    focus._in_band = _in_band
    feed.FROZEN_SOURCES = SOURCES
    feed.ALGORITHM_VERSION = VERSION

    # Clean up any queued generic-basketball rows left by the immediately prior
    # shadow configuration. EuroLeague rows are retained and relabelled.
    try:
        web_module.db.execute(
            "UPDATE live_yankee_candidates SET source_lane='EUROLEAGUE_1_50_TO_3_99' "
            "WHERE status='QUEUED' AND source_lane='BASKETBALL_1_50_TO_3_99' "
            "AND lower(sport_key)='basketball_euroleague'"
        )
        web_module.db.execute(
            "UPDATE live_yankee_candidates SET status='EXPIRED',expired_at=CURRENT_TIMESTAMP "
            "WHERE status='QUEUED' AND source_lane='BASKETBALL_1_50_TO_3_99'"
        )
        state = {
            "sources": list(SOURCES),
            "pred4": "all current PRED4 selections",
            "football": "core football execution shadows at 4.00-4.99",
            "nfl": "Multi-Sport NFL H2H at 2.00-4.99",
            "ncaaf": "Multi-Sport NCAAF H2H at 3.00-4.99",
            "euroleague": "Multi-Sport EuroLeague H2H at 1.50-3.99 (exploratory)",
            "baseball": "Multi-Sport MLB H2H at 1.50-1.99 (exploratory)",
            "seed_policy": "all still-open actionable selections, including selections created before Yankee-feed start; fresh prices required",
            "dedupe": "event+market+selection+point",
            "one_leg_per_event_per_card": True,
            "shadow_only": True,
        }
        web_module.db.execute(
            "UPDATE live_yankee_state SET algorithm_version=?,source_policy_json=? WHERE singleton_id=1",
            (VERSION, json.dumps(state, sort_keys=True)),
        )
    except Exception:
        pass

    walkforward_result = _backfill_walkforward()
    web_module.VERSION = "0.19.45"
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "sources": list(SOURCES),
        "walkforward": walkforward_result,
        "dashboard_version": web_module.VERSION,
    }
