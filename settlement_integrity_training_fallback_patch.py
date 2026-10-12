from __future__ import annotations

"""High-confidence historical football fallback for settlement integrity.

The shared football result endpoint only has a short lookback.  PRED4/PXG1
already recovers many old misses from API-Football.  For the remaining football
fixtures, this layer uses the independent football-data training store that is
already populated with final scores.  It is deliberately conservative: same
sport, tight date window, strong two-team match, and no conflicting near-tie.
"""

from datetime import datetime, timedelta, timezone
import json
from typing import Any, Dict, Mapping, Optional

import settlement_integrity_v2_patch as v2
from db import utc_now_iso
from results import settle_event_signals

VERSION = "SETTLEMENT_INTEGRITY_V2_1"
APP_VERSION = "0.19.53"
WINDOW_HOURS = 36.0


def _find_training_result(db, event: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    kickoff = v2._parse(event.get("commence_time"))
    if kickoff is None:
        return None
    rows = db.fetchall(
        """
        SELECT * FROM football_predictive_training_matches
        WHERE sport_key=? AND home_goals IS NOT NULL AND away_goals IS NOT NULL
          AND played_at>=? AND played_at<=?
        ORDER BY played_at,id
        """,
        (
            event.get("sport_key"),
            (kickoff - timedelta(hours=WINDOW_HOURS)).isoformat(),
            (kickoff + timedelta(hours=WINDOW_HOURS)).isoformat(),
        ),
    )
    candidates = []
    for raw in rows:
        row = dict(raw)
        played = v2._parse(row.get("played_at"))
        if played is None:
            continue
        home = v2.v1._team_score(event.get("home_team"), row.get("home_team"))
        away = v2.v1._team_score(event.get("away_team"), row.get("away_team"))
        if home < 0.90 or away < 0.90 or home + away < 1.84:
            continue
        delta = abs((played - kickoff).total_seconds()) / 3600.0
        composite = home + away - min(delta, WINDOW_HOURS) * 0.001
        candidates.append((composite, home + away, -delta, row, home, away, delta))
    if not candidates:
        return None
    candidates.sort(reverse=True, key=lambda x: (x[0], x[1], x[2]))
    best = candidates[0]
    # A second near-identical candidate with a conflicting score is ambiguous.
    # Duplicate source rows agreeing on the score are harmless.
    if len(candidates) > 1 and best[0] - candidates[1][0] < 0.04:
        bscore = (int(best[3]["home_goals"]), int(best[3]["away_goals"]))
        sscore = (int(candidates[1][3]["home_goals"]), int(candidates[1][3]["away_goals"]))
        if bscore != sscore:
            return None
    out = dict(best[3])
    out["match_home"] = best[4]
    out["match_away"] = best[5]
    out["time_delta_hours"] = best[6]
    return out


def _store_training_result(db, event: Mapping[str, Any], match: Mapping[str, Any]) -> Dict[str, Any]:
    hs = int(match["home_goals"])
    aws = int(match["away_goals"])
    provenance = {
        "repair": VERSION,
        "training_match_id": match.get("id"),
        "training_source": match.get("source"),
        "source_ref": match.get("source_ref"),
        "training_played_at": match.get("played_at"),
        "home_match": round(float(match.get("match_home") or 0), 4),
        "away_match": round(float(match.get("match_away") or 0), 4),
        "time_delta_hours": round(float(match.get("time_delta_hours") or 0), 3),
    }
    db.execute(
        """
        INSERT INTO event_results(
            event_id,fetched_at,completed_at,home_score,away_score,source,raw_json,
            settlement_quality,settlement_provenance
        ) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(event_id) DO NOTHING
        """,
        (
            event["event_id"], utc_now_iso(), match.get("played_at"), hs, aws,
            "football_training_repair", json.dumps(provenance, separators=(",", ":")),
            "A", json.dumps(provenance, separators=(",", ":")),
        ),
    )
    db.execute("UPDATE events SET status='COMPLETED' WHERE event_id=?", (event["event_id"],))
    settle_event_signals(db, event["event_id"], home_score=hs, away_score=aws)
    return {
        "event_id": event.get("event_id"),
        "fixture": f"{event.get('home_team')} v {event.get('away_team')}",
        "score": f"{hs}-{aws}",
        "source": "football_training_repair",
        "time_delta_hours": provenance["time_delta_hours"],
    }


def repair_all_football_missing_results(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    # First retain the stronger API-Football/PXG1 recovery already proven live.
    first = _ORIGINAL_REPAIR(db, now)
    second_repairs = []
    unresolved = []
    for event in v2._all_missing_football_results(db, now):
        match = _find_training_result(db, event)
        if match is None:
            unresolved.append({
                "event_id": event.get("event_id"),
                "fixture": f"{event.get('home_team')} v {event.get('away_team')}",
                "commence_time": event.get("commence_time"),
                "sport_key": event.get("sport_key"),
            })
            continue
        second_repairs.append(_store_training_result(db, event, match))
    repairs = list(first.get("repairs") or []) + second_repairs
    checked = int(first.get("checked") or 0)
    # checked means unique football obligations examined at the beginning of this pass.
    return {
        "checked": checked,
        "repaired": len(repairs),
        "repaired_pxg": int(first.get("repaired") or 0),
        "repaired_training": len(second_repairs),
        "unresolved": len(unresolved),
        "repairs": repairs,
        "unresolved_examples": unresolved[:30],
    }


_ORIGINAL_REPAIR = v2.repair_all_football_missing_results


def install() -> Dict[str, Any]:
    # V2's worker guard resolves this module-global function at run time, so patch
    # it before installing the guard; no second worker wrapper/thread is created.
    v2.repair_all_football_missing_results = repair_all_football_missing_results
    v2.VERSION = VERSION
    result = dict(v2.install())
    try:
        import web as web_module
        web_module.VERSION = APP_VERSION
    except Exception:
        pass
    result["version"] = VERSION
    result["dashboard_version"] = APP_VERSION
    result["training_fallback"] = True
    return result
