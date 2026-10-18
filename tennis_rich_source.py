from __future__ import annotations

"""Cached RapidAPI rich-tennis source for TPRED3 research.

This module has no betting authority. It persists provider IDs, historical
results, surfaces, rankings, raw match statistics and a request ledger so the
predictive layer can work from Postgres rather than repeatedly polling the API.
"""

from datetime import date, datetime, timedelta, timezone
import json
import os
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import requests

VERSION = "TPRED_RICH_SOURCE_V1"
HOST = "tennis-api-atp-wta-itf.p.rapidapi.com"
BASE_URL = f"https://{HOST}"
DAILY_REQUEST_CAP = int(os.getenv("TPRED_RICH_DAILY_CAP", "2500"))
PER_CYCLE_CAP = int(os.getenv("TPRED_RICH_PER_CYCLE_CAP", "150"))
RESULT_CHUNK_DAYS = int(os.getenv("TPRED_RICH_RESULT_CHUNK_DAYS", "21"))
BACKFILL_DAYS = int(os.getenv("TPRED_RICH_BACKFILL_DAYS", "365"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _norm_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return re.sub(r"\s+", " ", text)


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return None


def _player_obj(row: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = row.get(key)
    return value if isinstance(value, Mapping) else {}


def _tour_obj(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("tournament")
    return value if isinstance(value, Mapping) else {}


def _surface(row: Mapping[str, Any]) -> Optional[str]:
    tournament = _tour_obj(row)
    court = tournament.get("court")
    if isinstance(court, Mapping):
        value = court.get("name") or court.get("court")
        if value:
            return str(value)
    if isinstance(court, str) and court:
        return court
    court_id = _as_int(tournament.get("courtId") or row.get("courtId"))
    return {1: "Hard", 2: "Clay", 3: "I.hard", 4: "Carpet", 5: "Grass"}.get(court_id)


def install_schema(db) -> Dict[str, Any]:
    id_col = "BIGSERIAL PRIMARY KEY" if getattr(db, "is_postgres", False) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ddl = (
        f"""
        CREATE TABLE IF NOT EXISTS tennis_rich_api_ledger (
            id {id_col},
            requested_at TEXT NOT NULL,
            purpose TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            status_code INTEGER,
            ok INTEGER NOT NULL DEFAULT 0,
            provider_remaining INTEGER,
            error TEXT
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS tennis_rich_players (
            id {id_col},
            tour TEXT NOT NULL,
            provider_player_id INTEGER NOT NULL,
            player_name TEXT NOT NULL,
            player_name_norm TEXT NOT NULL,
            country TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(tour,provider_player_id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS tennis_rich_rankings (
            id {id_col},
            ranking_date TEXT NOT NULL,
            tour TEXT NOT NULL,
            provider_player_id INTEGER NOT NULL,
            player_name TEXT NOT NULL,
            ranking INTEGER,
            ranking_points REAL,
            fetched_at TEXT NOT NULL,
            UNIQUE(ranking_date,tour,provider_player_id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS tennis_rich_matches (
            id {id_col},
            tour TEXT NOT NULL,
            provider_match_id INTEGER NOT NULL,
            match_date TEXT,
            player1_id INTEGER,
            player1_name TEXT,
            player2_id INTEGER,
            player2_name TEXT,
            winner_id INTEGER,
            tournament_id INTEGER,
            tournament_name TEXT,
            surface TEXT,
            tour_rank INTEGER,
            round_id INTEGER,
            result TEXT,
            result_type TEXT,
            stats_json TEXT,
            stats_fetched_at TEXT,
            discovered_at TEXT NOT NULL,
            UNIQUE(tour,provider_match_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tennis_rich_sync_state (
            singleton_id INTEGER PRIMARY KEY,
            history_start TEXT,
            history_cursor TEXT,
            history_end TEXT,
            last_rankings_at TEXT,
            last_current_results_at TEXT,
            last_maintenance_at TEXT,
            source_version TEXT NOT NULL
        )
        """,
    )
    for sql in ddl:
        db.execute(sql)
    db.execute(
        """
        INSERT INTO tennis_rich_sync_state(singleton_id,source_version)
        VALUES(1,?)
        ON CONFLICT(singleton_id) DO UPDATE SET source_version=excluded.source_version
        """,
        (VERSION,),
    )
    return {"installed": True, "version": VERSION, "betting_authority": False}


def _used_today(db, now: Optional[datetime] = None) -> int:
    now = now or _now()
    prefix = now.date().isoformat() + "%"
    row = db.fetchone("SELECT COUNT(*) AS n FROM tennis_rich_api_ledger WHERE requested_at LIKE ?", (prefix,)) or {}
    return int(row.get("n") or 0)


class RichClient:
    def __init__(self, db, max_requests: int):
        self.db = db
        self.key = os.getenv("TENNIS_RAPIDAPI_KEY", "").strip()
        used = _used_today(db)
        allowance = max(0, DAILY_REQUEST_CAP - used)
        self.max_requests = max(0, min(int(max_requests), allowance))
        self.requests = 0
        self.errors = 0

    @property
    def available(self) -> bool:
        return bool(self.key) and self.requests < self.max_requests

    def get(self, path: str, *, params: Optional[Mapping[str, Any]] = None, purpose: str) -> Optional[Any]:
        if not self.available:
            return None
        endpoint = path if path.startswith("/") else "/" + path
        status = None
        ok = False
        remaining = None
        error = None
        payload = None
        try:
            response = requests.get(
                BASE_URL + endpoint,
                headers={"X-RapidAPI-Key": self.key, "X-RapidAPI-Host": HOST},
                params=dict(params or {}),
                timeout=18,
            )
            self.requests += 1
            status = int(response.status_code)
            remaining = _as_int(response.headers.get("x-ratelimit-requests-remaining"))
            ok = 200 <= status < 300
            if ok:
                payload = response.json()
            else:
                error = f"HTTP {status}"
                self.errors += 1
        except Exception as exc:
            self.requests += 1
            self.errors += 1
            error = f"{type(exc).__name__}: {exc}"
        self.db.execute(
            """
            INSERT INTO tennis_rich_api_ledger(
                requested_at,purpose,endpoint,status_code,ok,provider_remaining,error
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (_iso(_now()), purpose, endpoint, status, int(ok), remaining, error),
        )
        return payload


def _upsert_player(db, tour: str, player: Mapping[str, Any], seen_at: str) -> None:
    pid = _as_int(player.get("id"))
    name = str(player.get("name") or "").strip()
    if pid is None or not name:
        return
    db.execute(
        """
        INSERT INTO tennis_rich_players(
            tour,provider_player_id,player_name,player_name_norm,country,first_seen_at,last_seen_at
        ) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(tour,provider_player_id) DO UPDATE SET
            player_name=excluded.player_name,
            player_name_norm=excluded.player_name_norm,
            country=COALESCE(excluded.country,tennis_rich_players.country),
            last_seen_at=excluded.last_seen_at
        """,
        (tour, pid, name, _norm_name(name), player.get("countryAcr") or player.get("country"), seen_at, seen_at),
    )


def _store_result_row(db, tour: str, row: Mapping[str, Any]) -> bool:
    match_id = _as_int(row.get("matchId") or row.get("id"))
    if match_id is None:
        return False
    seen_at = _iso(_now())
    p1 = _player_obj(row, "player1")
    p2 = _player_obj(row, "player2")
    _upsert_player(db, tour, p1, seen_at)
    _upsert_player(db, tour, p2, seen_at)
    tournament = _tour_obj(row)
    p1id = _as_int(row.get("player1Id") or p1.get("id"))
    p2id = _as_int(row.get("player2Id") or p2.get("id"))
    winner = _as_int(row.get("match_winner")) or p1id
    changed = db.execute(
        """
        INSERT INTO tennis_rich_matches(
            tour,provider_match_id,match_date,player1_id,player1_name,player2_id,player2_name,
            winner_id,tournament_id,tournament_name,surface,tour_rank,round_id,result,result_type,
            discovered_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(tour,provider_match_id) DO UPDATE SET
            match_date=excluded.match_date,
            player1_id=excluded.player1_id,player1_name=excluded.player1_name,
            player2_id=excluded.player2_id,player2_name=excluded.player2_name,
            winner_id=excluded.winner_id,tournament_id=excluded.tournament_id,
            tournament_name=excluded.tournament_name,surface=COALESCE(excluded.surface,tennis_rich_matches.surface),
            tour_rank=excluded.tour_rank,round_id=excluded.round_id,result=excluded.result,
            result_type=excluded.result_type
        """,
        (
            tour, match_id, row.get("date") or row.get("startTime"), p1id, p1.get("name"), p2id,
            p2.get("name"), winner, _as_int(row.get("tournamentId") or tournament.get("id")),
            tournament.get("name"), _surface(row), _as_int(tournament.get("rankId") or row.get("rank")),
            _as_int(row.get("roundId")), row.get("result"), row.get("result_type"), seen_at,
        ),
    )
    return bool(changed)


def sync_results_range(db, client: RichClient, tour: str, start: date, end: date) -> Dict[str, int]:
    page = 1
    rows_seen = 0
    stored = 0
    while client.available:
        payload = client.get(
            f"/tennis/v2/{tour}/results/{start.isoformat()}/{end.isoformat()}",
            params={"pageNo": page, "pageSize": 500, "filter": "PlayerGroup:singles"},
            purpose=f"results_backfill_{tour}",
        )
        if not isinstance(payload, Mapping):
            break
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            break
        rows_seen += len(data)
        for row in data:
            if isinstance(row, Mapping) and _store_result_row(db, tour, row):
                stored += 1
        if not bool(payload.get("hasNextPage")):
            break
        page += 1
    return {"rows_seen": rows_seen, "stored": stored}


def _ranking_rows(payload: Any) -> List[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        data = payload.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, Mapping)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, Mapping)]
    return []


def sync_current_rankings(db, client: RichClient, tour: str, now: Optional[datetime] = None) -> Dict[str, int]:
    now = now or _now()
    ranking_date = now.date().isoformat()
    page = 1
    seen = 0
    stored = 0
    while client.available:
        payload = client.get(
            f"/tennis/v2/{tour}/ranking/singles",
            params={"pageNo": page, "pageSize": 500},
            purpose=f"current_rankings_{tour}",
        )
        rows = _ranking_rows(payload)
        if not rows:
            break
        for row in rows:
            player = row.get("player") if isinstance(row.get("player"), Mapping) else row
            pid = _as_int(player.get("id") or row.get("playerId"))
            name = str(player.get("name") or row.get("playerName") or row.get("name") or "").strip()
            if pid is None or not name:
                continue
            seen += 1
            _upsert_player(db, tour, {"id": pid, "name": name, "countryAcr": player.get("countryAcr")}, _iso(now))
            rank = _as_int(row.get("rank") or row.get("ranking") or row.get("position"))
            points = _as_float(row.get("points") or row.get("rankingPoints") or row.get("score"))
            changed = db.execute(
                """
                INSERT INTO tennis_rich_rankings(
                    ranking_date,tour,provider_player_id,player_name,ranking,ranking_points,fetched_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(ranking_date,tour,provider_player_id) DO UPDATE SET
                    player_name=excluded.player_name,ranking=excluded.ranking,
                    ranking_points=excluded.ranking_points,fetched_at=excluded.fetched_at
                """,
                (ranking_date, tour, pid, name, rank, points, _iso(now)),
            )
            stored += int(bool(changed))
        has_next = bool(payload.get("hasNextPage")) if isinstance(payload, Mapping) else False
        if not has_next:
            break
        page += 1
    return {"seen": seen, "stored": stored}


def sync_missing_match_stats(db, client: RichClient, limit: int = 100) -> Dict[str, int]:
    rows = db.fetchall(
        """
        SELECT tour,provider_match_id FROM tennis_rich_matches
        WHERE stats_json IS NULL AND COALESCE(result_type,'completed') NOT IN ('walkover','w/o')
        ORDER BY match_date DESC,provider_match_id DESC LIMIT ?
        """,
        (int(limit),),
    )
    fetched = 0
    nonempty = 0
    for row in rows:
        if not client.available:
            break
        tour = str(row.get("tour") or "").lower()
        mid = _as_int(row.get("provider_match_id"))
        if tour not in {"atp", "wta"} or mid is None:
            continue
        payload = client.get(
            f"/tennis/v2/matches/{mid}/stats",
            params={"type": tour, "source": "game"},
            purpose=f"match_stats_{tour}",
        )
        fetched += 1
        if payload is None:
            continue
        compact = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        has_data = bool(payload) and payload != {"data": {}}
        nonempty += int(has_data)
        db.execute(
            """
            UPDATE tennis_rich_matches SET stats_json=?,stats_fetched_at=?
            WHERE tour=? AND provider_match_id=?
            """,
            (compact, _iso(_now()), tour, mid),
        )
    return {"fetched": fetched, "nonempty": nonempty}


def _initial_window(db, now: datetime) -> Tuple[date, date]:
    state = db.fetchone("SELECT * FROM tennis_rich_sync_state WHERE singleton_id=1") or {}
    end = now.date()
    cursor_raw = state.get("history_cursor")
    if cursor_raw:
        try:
            return date.fromisoformat(str(cursor_raw)), end
        except Exception:
            pass
    start = end - timedelta(days=BACKFILL_DAYS)
    db.execute(
        """
        UPDATE tennis_rich_sync_state SET history_start=?,history_cursor=?,history_end=?
        WHERE singleton_id=1
        """,
        (start.isoformat(), start.isoformat(), end.isoformat()),
    )
    return start, end


def maintenance(db, now: Optional[datetime] = None, max_requests: int = PER_CYCLE_CAP) -> Dict[str, Any]:
    install_schema(db)
    now = now or _now()
    client = RichClient(db, max_requests=max_requests)
    if not client.key:
        return {"ok": False, "version": VERSION, "reason": "TENNIS_RAPIDAPI_KEY_missing", "requests": 0, "betting_authority": False}
    if client.max_requests <= 0:
        return {"ok": True, "version": VERSION, "reason": "daily_cap_reached", "requests": 0, "betting_authority": False}

    state = db.fetchone("SELECT * FROM tennis_rich_sync_state WHERE singleton_id=1") or {}
    rankings = {}
    last_rank = str(state.get("last_rankings_at") or "")
    if not last_rank or last_rank[:10] != now.date().isoformat():
        for tour in ("atp", "wta"):
            rankings[tour] = sync_current_rankings(db, client, tour, now)
            if not client.available:
                break
        if rankings:
            db.execute("UPDATE tennis_rich_sync_state SET last_rankings_at=? WHERE singleton_id=1", (_iso(now),))

    cursor, end = _initial_window(db, now)
    backfill = []
    while client.available and cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=RESULT_CHUNK_DAYS - 1))
        item: Dict[str, Any] = {"start": cursor.isoformat(), "end": chunk_end.isoformat(), "tours": {}}
        for tour in ("atp", "wta"):
            if not client.available:
                break
            item["tours"][tour] = sync_results_range(db, client, tour, cursor, chunk_end)
        backfill.append(item)
        cursor = chunk_end + timedelta(days=1)
        db.execute("UPDATE tennis_rich_sync_state SET history_cursor=? WHERE singleton_id=1", (cursor.isoformat(),))
        if len(backfill) >= 2:
            break

    stats_budget = max(0, min(100, client.max_requests - client.requests))
    stats = sync_missing_match_stats(db, client, limit=stats_budget) if stats_budget else {"fetched": 0, "nonempty": 0}
    db.execute("UPDATE tennis_rich_sync_state SET last_maintenance_at=? WHERE singleton_id=1", (_iso(now),))

    counts = {
        "players": int((db.fetchone("SELECT COUNT(*) AS n FROM tennis_rich_players") or {}).get("n") or 0),
        "rankings": int((db.fetchone("SELECT COUNT(*) AS n FROM tennis_rich_rankings") or {}).get("n") or 0),
        "matches": int((db.fetchone("SELECT COUNT(*) AS n FROM tennis_rich_matches") or {}).get("n") or 0),
        "matches_with_stats": int((db.fetchone("SELECT COUNT(*) AS n FROM tennis_rich_matches WHERE stats_json IS NOT NULL") or {}).get("n") or 0),
    }
    return {
        "ok": True,
        "version": VERSION,
        "requests": client.requests,
        "errors": client.errors,
        "daily_used": _used_today(db),
        "daily_cap": DAILY_REQUEST_CAP,
        "rankings": rankings,
        "backfill_chunks": backfill,
        "stats": stats,
        "counts": counts,
        "betting_authority": False,
    }


def status(db) -> Dict[str, Any]:
    install_schema(db)
    state = db.fetchone("SELECT * FROM tennis_rich_sync_state WHERE singleton_id=1") or {}
    return {
        "version": VERSION,
        "key_configured": bool(os.getenv("TENNIS_RAPIDAPI_KEY", "").strip()),
        "daily_used": _used_today(db),
        "daily_cap": DAILY_REQUEST_CAP,
        "per_cycle_cap": PER_CYCLE_CAP,
        "players": int((db.fetchone("SELECT COUNT(*) AS n FROM tennis_rich_players") or {}).get("n") or 0),
        "rankings": int((db.fetchone("SELECT COUNT(*) AS n FROM tennis_rich_rankings") or {}).get("n") or 0),
        "matches": int((db.fetchone("SELECT COUNT(*) AS n FROM tennis_rich_matches") or {}).get("n") or 0),
        "matches_with_stats": int((db.fetchone("SELECT COUNT(*) AS n FROM tennis_rich_matches WHERE stats_json IS NOT NULL") or {}).get("n") or 0),
        "history_cursor": state.get("history_cursor"),
        "history_end": state.get("history_end"),
        "last_maintenance_at": state.get("last_maintenance_at"),
        "betting_authority": False,
    }
