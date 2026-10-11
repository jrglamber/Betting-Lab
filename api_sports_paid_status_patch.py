from __future__ import annotations

"""Small observability helper for paid American-football PRED1.

Reads only shared research tables/collector metadata. It makes no provider calls
and has no execution authority. The startup log is deliberately limited to
non-sensitive season/count/quota metadata so subscription upgrades and price
linkage can be verified without exposing the API key.
"""

import json
from typing import Any, Dict

import web as web_module

VERSION = "APISPORTS_PAID_STATUS_V3"
_INSTALLED = False


def _safe_json(value: Any) -> Dict[str, Any]:
    try:
        obj = json.loads(str(value or "{}"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _lane_status(lane: str, model: str) -> Dict[str, Any]:
    db = web_module.db
    league = db.fetchone(
        "SELECT provider_league_id,league_name,season,discovered_at FROM api_sports_pred_leagues WHERE lane_key=?",
        (lane,),
    ) or {}
    latest = db.fetchone(
        "SELECT started_at,finished_at,ok,detail FROM collector_runs WHERE run_type=? ORDER BY id DESC LIMIT 1",
        (f"{model}_MAINT",),
    ) or {}
    detail = _safe_json(latest.get("detail"))
    counts = db.fetchall(
        """SELECT season,status,COUNT(*) AS n
           FROM api_sports_pred_games WHERE lane_key=?
           GROUP BY season,status ORDER BY season,status""",
        (lane,),
    )
    forecasts = db.fetchone(
        """SELECT COUNT(*) AS n,
                  SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS open_n,
                  SUM(CASE WHEN odds_event_id IS NOT NULL AND odds_event_id<>'' THEN 1 ELSE 0 END) AS mapped_n,
                  SUM(CASE WHEN status='OPEN' AND odds_event_id IS NOT NULL AND odds_event_id<>'' THEN 1 ELSE 0 END) AS open_mapped_n,
                  MAX(generated_at) AS latest_at
           FROM api_sports_pred_forecasts WHERE model_name=?""",
        (model,),
    ) or {}
    bets = db.fetchone(
        """SELECT COUNT(*) AS n,
                  SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS open_n,
                  MAX(created_at) AS latest_at
           FROM api_sports_pred_shadow_bets WHERE model_name=?""",
        (model,),
    ) or {}
    out = {
        "lane": lane,
        "model": model,
        "league": league.get("league_name") or detail.get("league"),
        "season": league.get("season") or detail.get("season"),
        "last_run_at": latest.get("started_at"),
        "last_run_ok": bool(latest.get("ok")) if latest else None,
        "games_synced_last_run": detail.get("games_synced"),
        "api_calls_last_run": detail.get("api_calls"),
        "api_remaining_last_run": detail.get("remaining"),
        "forecasts_created_last_run": detail.get("forecasts_created"),
        "shadow_bets_created_last_run": detail.get("shadow_bets_created"),
        "season_status_counts": [dict(x) for x in counts],
        "forecasts": int(forecasts.get("n") or 0),
        "open_forecasts": int(forecasts.get("open_n") or 0),
        "price_mapped_forecasts": int(forecasts.get("mapped_n") or 0),
        "open_price_mapped_forecasts": int(forecasts.get("open_mapped_n") or 0),
        "latest_forecast_at": forecasts.get("latest_at"),
        "shadow_bets": int(bets.get("n") or 0),
        "open_shadow_bets": int(bets.get("open_n") or 0),
        "latest_shadow_bet_at": bets.get("latest_at"),
        "error": detail.get("error") if latest and not bool(latest.get("ok")) else None,
    }
    if lane == "NCAAF" and int(forecasts.get("mapped_n") or 0) == 0:
        try:
            provider = db.fetchall(
                """SELECT home_team,away_team,commence_time
                   FROM api_sports_pred_forecasts
                   WHERE model_name=? AND status='OPEN'
                   ORDER BY commence_time LIMIT 8""",
                (model,),
            )
            odds = db.fetchall(
                """SELECT home_team,away_team,commence_time
                   FROM multisport_events
                   WHERE sport_key='americanfootball_ncaaf' AND status='UPCOMING'
                   ORDER BY commence_time LIMIT 12"""
            )
            out["mapping_samples"] = {
                "provider": [dict(x) for x in provider],
                "odds_api": [dict(x) for x in odds],
            }
        except Exception as exc:
            out["mapping_samples_error"] = f"{type(exc).__name__}: {exc}"
    return out


def snapshot() -> Dict[str, Any]:
    out: Dict[str, Any] = {"version": VERSION, "research_only": True, "lanes": {}}
    for lane, model in (("NFL", "NFL-PRED1"), ("NCAAF", "NCAAF-PRED1")):
        try:
            out["lanes"][lane] = _lane_status(lane, model)
        except Exception as exc:
            out["lanes"][lane] = {"lane": lane, "model": model, "error": f"{type(exc).__name__}: {exc}"}
    return out


def install() -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "status": snapshot()}
    status = snapshot()
    print("API_SPORTS_PAID_STATUS " + json.dumps(status, sort_keys=True, default=str), flush=True)
    _INSTALLED = True
    return {"installed": True, "already_installed": False, "status": status}
