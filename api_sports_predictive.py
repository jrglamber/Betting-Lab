from __future__ import annotations

"""API-Sports predictive shadow lane for NFL and EuroLeague.

Research-only. This script is designed to run as an isolated Railway cron service.
It never places bets and never alters the existing MSP1/MSP2 or XS1 rules.

It collects current-season game history from API-Sports, builds a deliberately
simple pre-market performance model (Elo + recency-weighted scoring margin),
maps forecasts to the Betting Lab's existing Odds-API multisport events where
possible, freezes the offered price seen at forecast time, and settles forecasts
from subsequent provider results.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import requests

from db import Database, utc_now_iso, sanitize_sensitive_text

MODEL_VERSION = "APISPORTS_PRED1_V1"
EDGE_THRESHOLD_PCT = 3.0
HOME_ADVANTAGE_ELO = 45.0
ELO_K = 20.0
REQUEST_TIMEOUT = 25


@dataclass(frozen=True)
class Lane:
    key: str
    model_name: str
    base_url: str
    league_search: str
    odds_sport_key: str


LANES: Tuple[Lane, ...] = (
    Lane(
        key="NFL",
        model_name="NFL-PRED1",
        base_url="https://v1.american-football.api-sports.io",
        league_search="NFL",
        odds_sport_key="americanfootball_nfl",
    ),
    Lane(
        key="EUROLEAGUE",
        model_name="EUROLEAGUE-PRED1",
        base_url="https://v1.basketball.api-sports.io",
        league_search="Euroleague",
        odds_sport_key="basketball_euroleague",
    ),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"\b(fc|bc|basket|basketball|club|the)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def _safe_float(value: Any) -> Optional[float]:
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return None


def _prob_to_odds(p: float) -> float:
    return 1.0 / max(0.01, min(0.99, p))


def _logistic_elo(diff: float) -> float:
    return 1.0 / (1.0 + 10.0 ** (-diff / 400.0))


class ApiSportsClient:
    def __init__(self, api_key: str):
        self.api_key = str(api_key or "").strip()
        self.calls: Dict[str, int] = {lane.key: 0 for lane in LANES}
        self.remaining: Dict[str, Optional[int]] = {lane.key: None for lane in LANES}

    def get(self, lane: Lane, endpoint: str, params: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        if not self.api_key:
            raise RuntimeError("API_FOOTBALL_KEY_NOT_SET")
        url = f"{lane.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        response = requests.get(
            url,
            params={k: v for k, v in params.items() if v not in (None, "")},
            timeout=REQUEST_TIMEOUT,
            headers={
                "x-apisports-key": self.api_key,
                "User-Agent": "Project-Exit-Plan-Betting-Lab/api-sports-pred1",
            },
        )
        self.calls[lane.key] += 1
        rem = response.headers.get("x-ratelimit-requests-remaining")
        try:
            self.remaining[lane.key] = int(rem) if rem is not None else None
        except Exception:
            pass
        response.raise_for_status()
        payload = response.json() if response.content else {}
        if not isinstance(payload, dict):
            raise RuntimeError(f"{lane.key} malformed response")
        errors = payload.get("errors")
        if errors:
            raise RuntimeError(f"{lane.key} API errors: {sanitize_sensitive_text(errors, (self.api_key,))}")
        data = payload.get("response") or []
        return data if isinstance(data, list) else [data]


def ensure_schema(db: Database) -> None:
    id_col = "BIGSERIAL PRIMARY KEY" if db.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    statements = (
        """
        CREATE TABLE IF NOT EXISTS api_sports_pred_leagues (
            lane_key TEXT PRIMARY KEY,
            provider_league_id INTEGER NOT NULL,
            league_name TEXT NOT NULL,
            season TEXT NOT NULL,
            discovered_at TEXT NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS api_sports_pred_games (
            id {id_col},
            lane_key TEXT NOT NULL,
            provider_game_id TEXT NOT NULL,
            provider_league_id INTEGER NOT NULL,
            season TEXT NOT NULL,
            commence_time TEXT NOT NULL,
            home_team_id TEXT,
            away_team_id TEXT,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            status TEXT NOT NULL,
            home_score REAL,
            away_score REAL,
            raw_updated_at TEXT,
            captured_at TEXT NOT NULL,
            UNIQUE(lane_key, provider_game_id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS api_sports_pred_forecasts (
            id {id_col},
            model_name TEXT NOT NULL,
            model_version TEXT NOT NULL,
            lane_key TEXT NOT NULL,
            provider_game_id TEXT NOT NULL,
            odds_event_id TEXT,
            generated_at TEXT NOT NULL,
            commence_time TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            home_probability REAL NOT NULL,
            away_probability REAL NOT NULL,
            home_fair_odds REAL NOT NULL,
            away_fair_odds REAL NOT NULL,
            expected_home_points REAL,
            expected_away_points REAL,
            expected_margin REAL,
            expected_total REAL,
            home_elo REAL,
            away_elo REAL,
            home_sample INTEGER NOT NULL,
            away_sample INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            home_score REAL,
            away_score REAL,
            winner TEXT,
            brier_score REAL,
            metadata_json TEXT,
            UNIQUE(model_name, provider_game_id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS api_sports_pred_shadow_bets (
            id {id_col},
            forecast_id INTEGER NOT NULL,
            model_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            odds_event_id TEXT NOT NULL,
            selection TEXT NOT NULL,
            bookmaker_key TEXT NOT NULL,
            offered_odds REAL NOT NULL,
            fair_probability REAL NOT NULL,
            fair_odds REAL NOT NULL,
            edge_pct REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            result TEXT,
            pnl_units REAL,
            UNIQUE(forecast_id, selection)
        )
        """,
    )
    for sql in statements:
        db.execute(sql)


def _league_season(item: Mapping[str, Any]) -> List[Tuple[int, str, str, bool]]:
    league_obj = item.get("league") if isinstance(item.get("league"), dict) else item
    league_id = _safe_int((league_obj or {}).get("id"))
    name = str((league_obj or {}).get("name") or "")
    seasons = item.get("seasons") or (league_obj or {}).get("seasons") or []
    out: List[Tuple[int, str, str, bool]] = []
    if league_id is None:
        return out
    for s in seasons:
        if isinstance(s, dict):
            season = str(s.get("season") or s.get("year") or "")
            current = bool(s.get("current"))
        else:
            season = str(s or "")
            current = False
        if season:
            out.append((league_id, name, season, current))
    return out


def discover_lane(db: Database, client: ApiSportsClient, lane: Lane) -> Tuple[int, str, str]:
    cached = db.fetchone(
        "SELECT * FROM api_sports_pred_leagues WHERE lane_key=?",
        (lane.key,),
    )
    # League/season discovery changes slowly. Reuse for 14 days, then refresh.
    if cached:
        at = _parse_dt(cached.get("discovered_at"))
        if at and (_now() - at).total_seconds() < 14 * 86400:
            return int(cached["provider_league_id"]), str(cached["league_name"]), str(cached["season"])

    rows = client.get(lane, "leagues", {"search": lane.league_search})
    candidates: List[Tuple[int, str, str, bool]] = []
    for item in rows:
        candidates.extend(_league_season(item))
    if not candidates:
        raise RuntimeError(f"{lane.key}: no league/season found for {lane.league_search}")
    current = [x for x in candidates if x[3]]
    chosen = (current or candidates)[-1]
    league_id, league_name, season, _ = chosen
    db.execute(
        """
        INSERT INTO api_sports_pred_leagues(lane_key,provider_league_id,league_name,season,discovered_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(lane_key) DO UPDATE SET provider_league_id=excluded.provider_league_id,
          league_name=excluded.league_name,season=excluded.season,discovered_at=excluded.discovered_at
        """,
        (lane.key, league_id, league_name, season, utc_now_iso()),
    )
    return league_id, league_name, season


def _extract_game(lane: Lane, item: Mapping[str, Any], league_id: int, season: str) -> Optional[Dict[str, Any]]:
    game_id = str(item.get("id") or "")
    date_value = item.get("date")
    if isinstance(date_value, dict):
        date_value = date_value.get("date") or date_value.get("start")
    commence = _parse_dt(date_value)
    teams = item.get("teams") or {}
    home = teams.get("home") if isinstance(teams, dict) else None
    away = teams.get("away") if isinstance(teams, dict) else None
    if not isinstance(home, dict) or not isinstance(away, dict):
        return None
    home_name = str(home.get("name") or "")
    away_name = str(away.get("name") or "")
    if not game_id or not commence or not home_name or not away_name:
        return None

    status_obj = item.get("status") or {}
    if isinstance(status_obj, dict):
        status = str(status_obj.get("short") or status_obj.get("long") or "")
    else:
        status = str(status_obj or "")

    scores = item.get("scores") or {}
    hscore = ascore = None
    if isinstance(scores, dict):
        h = scores.get("home")
        a = scores.get("away")
        if isinstance(h, dict):
            h = h.get("total") or h.get("points")
        if isinstance(a, dict):
            a = a.get("total") or a.get("points")
        hscore = _safe_float(h)
        ascore = _safe_float(a)

    # Some products expose scores as top-level nested team values.
    if hscore is None or ascore is None:
        for key in ("score", "points"):
            obj = item.get(key)
            if isinstance(obj, dict):
                hscore = hscore if hscore is not None else _safe_float(obj.get("home"))
                ascore = ascore if ascore is not None else _safe_float(obj.get("away"))

    completed = status.upper() in {"FT", "AET", "AP", "3", "FINISHED", "FINAL"}
    canonical_status = "FINISHED" if completed and hscore is not None and ascore is not None else "UPCOMING"
    if status.upper() in {"CANC", "POST", "ABD", "PST", "CANCELLED", "POSTPONED"}:
        canonical_status = "VOID"

    return {
        "lane_key": lane.key,
        "provider_game_id": game_id,
        "provider_league_id": league_id,
        "season": season,
        "commence_time": _iso(commence),
        "home_team_id": str(home.get("id") or ""),
        "away_team_id": str(away.get("id") or ""),
        "home_team": home_name,
        "away_team": away_name,
        "status": canonical_status,
        "home_score": hscore,
        "away_score": ascore,
        "raw_updated_at": str(item.get("updated") or item.get("timestamp") or ""),
    }


def sync_games(db: Database, client: ApiSportsClient, lane: Lane, league_id: int, season: str) -> int:
    rows = client.get(lane, "games", {"league": league_id, "season": season, "timezone": "UTC"})
    captured = utc_now_iso()
    count = 0
    for item in rows:
        game = _extract_game(lane, item, league_id, season)
        if not game:
            continue
        db.execute(
            """
            INSERT INTO api_sports_pred_games(
              lane_key,provider_game_id,provider_league_id,season,commence_time,
              home_team_id,away_team_id,home_team,away_team,status,home_score,away_score,
              raw_updated_at,captured_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(lane_key,provider_game_id) DO UPDATE SET
              commence_time=excluded.commence_time,home_team=excluded.home_team,away_team=excluded.away_team,
              status=excluded.status,home_score=excluded.home_score,away_score=excluded.away_score,
              raw_updated_at=excluded.raw_updated_at,captured_at=excluded.captured_at
            """,
            (
                game["lane_key"],game["provider_game_id"],game["provider_league_id"],game["season"],
                game["commence_time"],game["home_team_id"],game["away_team_id"],game["home_team"],
                game["away_team"],game["status"],game["home_score"],game["away_score"],
                game["raw_updated_at"],captured,
            ),
        )
        count += 1
    return count


def _build_ratings(games: Sequence[Mapping[str, Any]]) -> Tuple[Dict[str, float], Dict[str, List[Tuple[float, float]]]]:
    elo: Dict[str, float] = {}
    perf: Dict[str, List[Tuple[float, float]]] = {}
    for g in games:
        home = str(g["home_team"]); away = str(g["away_team"])
        hs = _safe_float(g.get("home_score")); aas = _safe_float(g.get("away_score"))
        if hs is None or aas is None:
            continue
        he = elo.get(home, 1500.0); ae = elo.get(away, 1500.0)
        expected = _logistic_elo((he + HOME_ADVANTAGE_ELO) - ae)
        actual = 1.0 if hs > aas else 0.0 if hs < aas else 0.5
        margin_mult = max(1.0, math.log(abs(hs - aas) + 1.0))
        delta = ELO_K * margin_mult * (actual - expected)
        elo[home] = he + delta; elo[away] = ae - delta
        perf.setdefault(home, []).append((hs, aas))
        perf.setdefault(away, []).append((aas, hs))
    return elo, perf


def _weighted_team_stats(perf: Mapping[str, List[Tuple[float, float]]], team: str) -> Tuple[Optional[float], Optional[float], int]:
    rows = list(perf.get(team) or [])[-10:]
    if not rows:
        return None, None, 0
    weights = list(range(1, len(rows) + 1))
    denom = float(sum(weights))
    pf = sum(w * r[0] for w, r in zip(weights, rows)) / denom
    pa = sum(w * r[1] for w, r in zip(weights, rows)) / denom
    return pf, pa, len(rows)


def _match_odds_event(db: Database, lane: Lane, game: Mapping[str, Any]) -> Optional[str]:
    commence = _parse_dt(game.get("commence_time"))
    if not commence:
        return None
    rows = db.fetchall(
        """
        SELECT event_id,home_team,away_team,commence_time
        FROM multisport_events
        WHERE sport_key=? AND status='UPCOMING'
        """,
        (lane.odds_sport_key,),
    )
    gh = _norm(game.get("home_team")); ga = _norm(game.get("away_team"))
    best: Optional[Tuple[float, str]] = None
    for r in rows:
        rh = _norm(r.get("home_team")); ra = _norm(r.get("away_team"))
        if gh != rh or ga != ra:
            continue
        dt = _parse_dt(r.get("commence_time"))
        if not dt:
            continue
        hours = abs((dt - commence).total_seconds()) / 3600.0
        if hours <= 12 and (best is None or hours < best[0]):
            best = (hours, str(r["event_id"]))
    return best[1] if best else None


def _latest_best_price(db: Database, event_id: str, selection: str) -> Optional[Tuple[str, float]]:
    rows = db.fetchall(
        """
        SELECT bookmaker_key,price,captured_at
        FROM multisport_odds_snapshots
        WHERE event_id=? AND market_key='h2h' AND selection=?
        ORDER BY captured_at DESC,id DESC
        """,
        (event_id, selection),
    )
    if not rows:
        return None
    latest_at = str(rows[0].get("captured_at") or "")
    wave = [r for r in rows if str(r.get("captured_at") or "") == latest_at]
    priced = [(str(r.get("bookmaker_key") or ""), _safe_float(r.get("price"))) for r in wave]
    priced = [(b, p) for b, p in priced if b and p is not None and p > 1.0]
    return max(priced, key=lambda x: x[1]) if priced else None


def make_forecasts(db: Database, lane: Lane) -> Dict[str, int]:
    finished = db.fetchall(
        """
        SELECT * FROM api_sports_pred_games
        WHERE lane_key=? AND status='FINISHED'
        ORDER BY commence_time ASC,provider_game_id ASC
        """,
        (lane.key,),
    )
    elo, perf = _build_ratings(finished)
    upcoming = db.fetchall(
        """
        SELECT * FROM api_sports_pred_games
        WHERE lane_key=? AND status='UPCOMING' AND commence_time>?
        ORDER BY commence_time ASC
        """,
        (lane.key, utc_now_iso()),
    )
    created = bets = 0
    for g in upcoming:
        if db.fetchone("SELECT id FROM api_sports_pred_forecasts WHERE model_name=? AND provider_game_id=?", (lane.model_name,g["provider_game_id"])):
            continue
        home = str(g["home_team"]); away = str(g["away_team"])
        he = elo.get(home,1500.0); ae = elo.get(away,1500.0)
        hpf,hpa,hn = _weighted_team_stats(perf,home)
        apf,apa,an = _weighted_team_stats(perf,away)
        if hn < 3 or an < 3:
            continue
        base_p = _logistic_elo((he + HOME_ADVANTAGE_ELO) - ae)
        home_exp = ((hpf or 0.0) + (apa or 0.0)) / 2.0
        away_exp = ((apf or 0.0) + (hpa or 0.0)) / 2.0
        margin = home_exp - away_exp
        # A small bounded performance adjustment. Elo remains the dominant term.
        adj = max(-0.08, min(0.08, margin / 100.0))
        hp = max(0.04, min(0.96, base_p + adj))
        ap = 1.0 - hp
        odds_event_id = _match_odds_event(db,lane,g)
        generated = utc_now_iso()
        metadata = {
            "method": "elo_plus_recent_scoring_margin",
            "elo_k": ELO_K,
            "home_advantage_elo": HOME_ADVANTAGE_ELO,
            "edge_threshold_pct": EDGE_THRESHOLD_PCT,
            "research_only": True,
        }
        db.execute(
            """
            INSERT INTO api_sports_pred_forecasts(
              model_name,model_version,lane_key,provider_game_id,odds_event_id,generated_at,
              commence_time,home_team,away_team,home_probability,away_probability,
              home_fair_odds,away_fair_odds,expected_home_points,expected_away_points,
              expected_margin,expected_total,home_elo,away_elo,home_sample,away_sample,status,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (lane.model_name,MODEL_VERSION,lane.key,g["provider_game_id"],odds_event_id,generated,
             g["commence_time"],home,away,hp,ap,_prob_to_odds(hp),_prob_to_odds(ap),
             home_exp,away_exp,margin,home_exp+away_exp,he,ae,hn,an,"OPEN",json.dumps(metadata,sort_keys=True)),
        )
        created += 1
        forecast = db.fetchone("SELECT * FROM api_sports_pred_forecasts WHERE model_name=? AND provider_game_id=?", (lane.model_name,g["provider_game_id"]))
        if not forecast or not odds_event_id:
            continue
        for selection,p in ((home,hp),(away,ap)):
            quote = _latest_best_price(db,odds_event_id,selection)
            if not quote:
                continue
            book, offered = quote
            edge = (p * offered - 1.0) * 100.0
            if edge < EDGE_THRESHOLD_PCT:
                continue
            db.execute(
                """
                INSERT INTO api_sports_pred_shadow_bets(
                  forecast_id,model_name,created_at,odds_event_id,selection,bookmaker_key,
                  offered_odds,fair_probability,fair_odds,edge_pct,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(forecast_id,selection) DO NOTHING
                """,
                (forecast["id"],lane.model_name,generated,odds_event_id,selection,book,offered,p,_prob_to_odds(p),edge,"OPEN"),
            )
            bets += 1
    return {"forecasts_created": created, "shadow_bets_created": bets}


def settle(db: Database, lane: Lane) -> Dict[str, int]:
    rows = db.fetchall(
        """
        SELECT f.*,g.home_score AS final_home,g.away_score AS final_away,g.status AS game_status
        FROM api_sports_pred_forecasts f
        JOIN api_sports_pred_games g ON g.lane_key=f.lane_key AND g.provider_game_id=f.provider_game_id
        WHERE f.model_name=? AND f.status='OPEN' AND g.status IN ('FINISHED','VOID')
        """,
        (lane.model_name,),
    )
    forecasts = bets = 0
    for f in rows:
        if str(f.get("game_status")) == "VOID":
            db.execute("UPDATE api_sports_pred_forecasts SET status='VOID' WHERE id=?", (f["id"],))
            db.execute("UPDATE api_sports_pred_shadow_bets SET status='VOID',result='VOID',pnl_units=0 WHERE forecast_id=? AND status='OPEN'", (f["id"],))
            forecasts += 1
            continue
        hs = _safe_float(f.get("final_home")); aas = _safe_float(f.get("final_away"))
        if hs is None or aas is None:
            continue
        winner = str(f["home_team"]) if hs > aas else str(f["away_team"]) if aas > hs else "DRAW"
        y_home = 1.0 if hs > aas else 0.0 if aas > hs else 0.5
        hp = float(f["home_probability"])
        brier = (hp - y_home) ** 2
        db.execute(
            "UPDATE api_sports_pred_forecasts SET status='SETTLED',home_score=?,away_score=?,winner=?,brier_score=? WHERE id=?",
            (hs,aas,winner,brier,f["id"]),
        )
        forecasts += 1
        open_bets = db.fetchall("SELECT * FROM api_sports_pred_shadow_bets WHERE forecast_id=? AND status='OPEN'", (f["id"],))
        for b in open_bets:
            won = str(b["selection"]) == winner
            result = "WIN" if won else "LOSS"
            pnl = float(b["offered_odds"]) - 1.0 if won else -1.0
            db.execute("UPDATE api_sports_pred_shadow_bets SET status='SETTLED',result=?,pnl_units=? WHERE id=?", (result,pnl,b["id"]))
            bets += 1
    return {"forecasts_settled": forecasts, "shadow_bets_settled": bets}


def scoreboard(db: Database, lane: Lane) -> Dict[str, Any]:
    f = db.fetchone(
        """
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN status='SETTLED' THEN 1 ELSE 0 END) AS settled,
               AVG(CASE WHEN status='SETTLED' THEN brier_score END) AS avg_brier
        FROM api_sports_pred_forecasts WHERE model_name=?
        """,
        (lane.model_name,),
    ) or {}
    b = db.fetchone(
        """
        SELECT COUNT(*) AS bets,
               SUM(CASE WHEN status='SETTLED' THEN 1 ELSE 0 END) AS settled_bets,
               COALESCE(SUM(CASE WHEN status='SETTLED' THEN pnl_units ELSE 0 END),0) AS pnl
        FROM api_sports_pred_shadow_bets WHERE model_name=?
        """,
        (lane.model_name,),
    ) or {}
    settled_bets = int(b.get("settled_bets") or 0)
    pnl = float(b.get("pnl") or 0.0)
    return {
        "model": lane.model_name,
        "forecasts": int(f.get("n") or 0),
        "settled_forecasts": int(f.get("settled") or 0),
        "avg_brier": _safe_float(f.get("avg_brier")),
        "shadow_bets": int(b.get("bets") or 0),
        "settled_shadow_bets": settled_bets,
        "pnl_units": pnl,
        "roi_pct": (pnl / settled_bets * 100.0) if settled_bets else None,
    }


def run() -> Dict[str, Any]:
    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    database_url = os.getenv("DATABASE_URL", "").strip()
    db_path = os.getenv("DB_PATH", "./betting_lab.sqlite")
    db = Database(database_url, db_path)
    ensure_schema(db)
    client = ApiSportsClient(api_key)
    summary: Dict[str, Any] = {"model_version": MODEL_VERSION, "lanes": {}}
    for lane in LANES:
        lane_result: Dict[str, Any] = {}
        try:
            league_id, league_name, season = discover_lane(db,client,lane)
            lane_result.update({"league_id":league_id,"league":league_name,"season":season})
            lane_result["games_synced"] = sync_games(db,client,lane,league_id,season)
            lane_result.update(make_forecasts(db,lane))
            lane_result.update(settle(db,lane))
            lane_result["scoreboard"] = scoreboard(db,lane)
            lane_result["api_calls"] = client.calls[lane.key]
            lane_result["remaining"] = client.remaining[lane.key]
            lane_result["ok"] = True
            try:
                db.record_collector_run(f"{lane.model_name}_MAINT", True, detail=json.dumps(lane_result,sort_keys=True))
            except Exception:
                pass
        except Exception as exc:
            lane_result.update({"ok":False,"error":sanitize_sensitive_text(f"{type(exc).__name__}: {exc}",(api_key,)),"api_calls":client.calls[lane.key],"remaining":client.remaining[lane.key]})
            try:
                db.record_collector_run(f"{lane.model_name}_MAINT", False, detail=json.dumps(lane_result,sort_keys=True))
            except Exception:
                pass
        summary["lanes"][lane.key] = lane_result
    print("API_SPORTS_PRED1 " + json.dumps(summary, sort_keys=True), flush=True)
    return summary


if __name__ == "__main__":
    result = run()
    raise SystemExit(0 if all(v.get("ok") for v in result["lanes"].values()) else 1)
