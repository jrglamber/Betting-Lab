from __future__ import annotations

"""Runtime entry point for API-Sports PRED1 with the verified NFL payload adapter."""

from typing import Any, Dict, Mapping, Optional

import api_sports_predictive as core
import api_sports_walkforward as walkforward


_original_extract_game = core._extract_game


def _extract_nfl(item: Mapping[str, Any], league_id: int, season: str) -> Optional[Dict[str, Any]]:
    game_obj = item.get("game") if isinstance(item.get("game"), dict) else {}
    game_id = str(game_obj.get("id") or item.get("id") or "")
    date_value = game_obj.get("date") or item.get("date")
    if isinstance(date_value, dict):
        ts = core._safe_int(date_value.get("timestamp"))
        if ts is not None:
            from datetime import datetime, timezone
            commence = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            raw_date = str(date_value.get("date") or "").strip()
            raw_time = str(date_value.get("time") or "00:00").strip()
            commence = core._parse_dt(f"{raw_date}T{raw_time}:00+00:00" if raw_date else "")
    else:
        commence = core._parse_dt(date_value)

    teams = item.get("teams") or {}
    home = teams.get("home") if isinstance(teams, dict) else None
    away = teams.get("away") if isinstance(teams, dict) else None
    if not isinstance(home, dict) or not isinstance(away, dict):
        return None
    home_name = str(home.get("name") or "")
    away_name = str(away.get("name") or "")
    if not game_id or not commence or not home_name or not away_name:
        return None

    status_obj = game_obj.get("status") or item.get("status") or {}
    status = str(status_obj.get("short") or status_obj.get("long") or "") if isinstance(status_obj, dict) else str(status_obj or "")
    scores = item.get("scores") or {}
    home_score = away_score = None
    if isinstance(scores, dict):
        h = scores.get("home"); a = scores.get("away")
        if isinstance(h, dict): h = h.get("total") if h.get("total") is not None else h.get("points")
        if isinstance(a, dict): a = a.get("total") if a.get("total") is not None else a.get("points")
        home_score = core._safe_float(h); away_score = core._safe_float(a)

    status_upper = status.upper()
    completed = status_upper in {"FT", "AET", "AP", "3", "FINISHED", "FINAL"}
    canonical_status = "FINISHED" if completed and home_score is not None and away_score is not None else "UPCOMING"
    if status_upper in {"CANC", "POST", "ABD", "PST", "CANCELLED", "POSTPONED"}:
        canonical_status = "VOID"

    return {
        "lane_key": "NFL", "provider_game_id": game_id, "provider_league_id": league_id,
        "season": season, "commence_time": core._iso(commence),
        "home_team_id": str(home.get("id") or ""), "away_team_id": str(away.get("id") or ""),
        "home_team": home_name, "away_team": away_name, "status": canonical_status,
        "home_score": home_score, "away_score": away_score,
        "raw_updated_at": str(item.get("updated") or item.get("timestamp") or game_obj.get("date", {}).get("timestamp") if isinstance(game_obj.get("date"), dict) else ""),
    }


def verified_extract_game(lane, item: Mapping[str, Any], league_id: int, season: str):
    return _extract_nfl(item, league_id, season) if lane.key == "NFL" else _original_extract_game(lane, item, league_id, season)


core._extract_game = verified_extract_game


if __name__ == "__main__":
    result = core.run()
    wf = walkforward.run()
    ok = all(v.get("ok") for v in result["lanes"].values()) and all(v.get("ok") for v in wf["lanes"].values())
    raise SystemExit(0 if ok else 1)
