from __future__ import annotations

"""Chronological rich context for TPRED3.

Adds two inputs the base rich collector intentionally does not infer:
- dated official rankings for historical matches (prevents current-rank leakage)
- upcoming fixture surface/tournament context for frozen forward forecasts

Research only. No odds, selections, staking or execution authority live here.
"""

from datetime import date, datetime, timedelta, timezone
import json
from typing import Any, Dict, List, Mapping, Optional

from tennis_rich_source import RichClient, _as_float, _as_int, _iso, _norm_name, _now, _surface, _tour_obj, _upsert_player

VERSION = "TPRED_RICH_CONTEXT_V1"
RANKING_WEEKS_BACK = 156  # three years
RANKING_WEEKS_PER_CYCLE = 6
FIXTURE_HORIZON_DAYS = 8


def install_schema(db) -> Dict[str, Any]:
    id_col = "BIGSERIAL PRIMARY KEY" if getattr(db, "is_postgres", False) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ddl = (
        f"""
        CREATE TABLE IF NOT EXISTS tennis_rich_fixtures (
            id {id_col},
            tour TEXT NOT NULL,
            provider_match_id INTEGER NOT NULL,
            start_time TEXT,
            player1_id INTEGER,
            player1_name TEXT,
            player2_id INTEGER,
            player2_name TEXT,
            tournament_id INTEGER,
            tournament_name TEXT,
            surface TEXT,
            tour_rank INTEGER,
            round_id INTEGER,
            discovered_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(tour,provider_match_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tennis_rich_context_state (
            singleton_id INTEGER PRIMARY KEY,
            ranking_cursor TEXT,
            ranking_end TEXT,
            last_fixture_sync_at TEXT,
            last_maintenance_at TEXT,
            version TEXT NOT NULL
        )
        """,
    )
    for sql in ddl:
        db.execute(sql)
    db.execute(
        """
        INSERT INTO tennis_rich_context_state(singleton_id,version)
        VALUES(1,?)
        ON CONFLICT(singleton_id) DO UPDATE SET version=excluded.version
        """,
        (VERSION,),
    )
    return {"installed": True, "version": VERSION, "betting_authority": False}


def _extract_rows(payload: Any) -> List[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        data = payload.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, Mapping)]
        if isinstance(data, Mapping):
            for key in ("rankings", "rows", "items", "fixtures", "matches"):
                value = data.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, Mapping)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, Mapping)]
    return []


def _ranking_cursor(db, now: datetime) -> date:
    state = db.fetchone("SELECT * FROM tennis_rich_context_state WHERE singleton_id=1") or {}
    raw = state.get("ranking_cursor")
    if raw:
        try:
            return date.fromisoformat(str(raw))
        except Exception:
            pass
    # Mondays make ranking snapshots comparable and match official list cadence.
    today = now.date()
    latest_monday = today - timedelta(days=today.weekday())
    start = latest_monday - timedelta(weeks=RANKING_WEEKS_BACK)
    db.execute(
        "UPDATE tennis_rich_context_state SET ranking_cursor=?,ranking_end=? WHERE singleton_id=1",
        (start.isoformat(), latest_monday.isoformat()),
    )
    return start


def sync_historical_rankings(db, client: RichClient, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or _now()
    state = db.fetchone("SELECT * FROM tennis_rich_context_state WHERE singleton_id=1") or {}
    cursor = _ranking_cursor(db, now)
    end_raw = state.get("ranking_end")
    if not end_raw:
        state = db.fetchone("SELECT * FROM tennis_rich_context_state WHERE singleton_id=1") or {}
        end_raw = state.get("ranking_end")
    end = date.fromisoformat(str(end_raw)) if end_raw else now.date()
    snapshots = 0
    rows_stored = 0

    while client.available and cursor <= end and snapshots < RANKING_WEEKS_PER_CYCLE:
        date_param = cursor.strftime("%d.%m.%Y")
        for tour in ("atp", "wta"):
            if not client.available:
                break
            page = 1
            while client.available:
                payload = client.get(
                    f"/tennis/v2/ranking/{tour}",
                    params={"date": date_param, "group": "singles", "page": page, "limit": 500},
                    purpose=f"historical_rankings_{tour}",
                )
                rows = _extract_rows(payload)
                if not rows:
                    break
                for row in rows:
                    player = row.get("player") if isinstance(row.get("player"), Mapping) else row
                    pid = _as_int(player.get("id") or row.get("playerId") or row.get("id"))
                    name = str(player.get("name") or row.get("playerName") or row.get("name") or "").strip()
                    if pid is None or not name:
                        continue
                    rank = _as_int(row.get("rank") or row.get("ranking") or row.get("position"))
                    points = _as_float(row.get("points") or row.get("rankingPoints") or row.get("score"))
                    seen = _iso(now)
                    _upsert_player(db, tour, {"id": pid, "name": name, "countryAcr": player.get("countryAcr")}, seen)
                    changed = db.execute(
                        """
                        INSERT INTO tennis_rich_rankings(
                            ranking_date,tour,provider_player_id,player_name,ranking,ranking_points,fetched_at
                        ) VALUES(?,?,?,?,?,?,?)
                        ON CONFLICT(ranking_date,tour,provider_player_id) DO UPDATE SET
                            player_name=excluded.player_name,ranking=excluded.ranking,
                            ranking_points=excluded.ranking_points,fetched_at=excluded.fetched_at
                        """,
                        (cursor.isoformat(), tour, pid, name, rank, points, seen),
                    )
                    rows_stored += int(bool(changed))
                has_next = bool(payload.get("hasNextPage")) if isinstance(payload, Mapping) else False
                if not has_next:
                    break
                page += 1
        snapshots += 1
        cursor = cursor + timedelta(days=7)
        db.execute("UPDATE tennis_rich_context_state SET ranking_cursor=? WHERE singleton_id=1", (cursor.isoformat(),))

    return {"snapshots": snapshots, "rows_stored": rows_stored, "next_cursor": cursor.isoformat(), "end": end.isoformat()}


def _store_fixture(db, tour: str, row: Mapping[str, Any], now: datetime) -> bool:
    mid = _as_int(row.get("matchId") or row.get("id"))
    if mid is None:
        return False
    p1 = row.get("player1") if isinstance(row.get("player1"), Mapping) else {}
    p2 = row.get("player2") if isinstance(row.get("player2"), Mapping) else {}
    p1id = _as_int(row.get("player1Id") or p1.get("id"))
    p2id = _as_int(row.get("player2Id") or p2.get("id"))
    seen = _iso(now)
    _upsert_player(db, tour, {"id": p1id, "name": p1.get("name"), "countryAcr": p1.get("countryAcr")}, seen)
    _upsert_player(db, tour, {"id": p2id, "name": p2.get("name"), "countryAcr": p2.get("countryAcr")}, seen)
    tournament = _tour_obj(row)
    changed = db.execute(
        """
        INSERT INTO tennis_rich_fixtures(
            tour,provider_match_id,start_time,player1_id,player1_name,player2_id,player2_name,
            tournament_id,tournament_name,surface,tour_rank,round_id,discovered_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(tour,provider_match_id) DO UPDATE SET
            start_time=excluded.start_time,player1_id=excluded.player1_id,player1_name=excluded.player1_name,
            player2_id=excluded.player2_id,player2_name=excluded.player2_name,
            tournament_id=excluded.tournament_id,tournament_name=excluded.tournament_name,
            surface=excluded.surface,tour_rank=excluded.tour_rank,round_id=excluded.round_id,
            updated_at=excluded.updated_at
        """,
        (
            tour,mid,row.get("date") or row.get("startTime"),p1id,p1.get("name"),p2id,p2.get("name"),
            _as_int(row.get("tournamentId") or tournament.get("id")),tournament.get("name"),_surface(row),
            _as_int(tournament.get("rankId") or row.get("rank")),_as_int(row.get("roundId")),seen,seen,
        ),
    )
    return bool(changed)


def sync_upcoming_fixtures(db, client: RichClient, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or _now()
    start = now.date()
    end = start + timedelta(days=FIXTURE_HORIZON_DAYS)
    out: Dict[str, Any] = {}
    for tour in ("atp", "wta"):
        if not client.available:
            break
        page = 1
        seen = 0
        stored = 0
        while client.available:
            payload = client.get(
                f"/tennis/v2/{tour}/fixtures/{start.isoformat()}/{end.isoformat()}",
                params={
                    "include": "round,tournament,tournament.court,tournament.rank",
                    "filter": "PlayerGroup:singles",
                    "pageNo": page,
                    "pageSize": 500,
                },
                purpose=f"upcoming_fixtures_{tour}",
            )
            rows = _extract_rows(payload)
            if not rows:
                break
            seen += len(rows)
            for row in rows:
                stored += int(_store_fixture(db, tour, row, now))
            if not (isinstance(payload, Mapping) and bool(payload.get("hasNextPage"))):
                break
            page += 1
        out[tour] = {"seen": seen, "stored": stored}
    if out:
        db.execute("UPDATE tennis_rich_context_state SET last_fixture_sync_at=? WHERE singleton_id=1", (_iso(now),))
    return out


def maintenance(db, max_requests: int = 40, now: Optional[datetime] = None) -> Dict[str, Any]:
    install_schema(db)
    now = now or _now()
    client = RichClient(db, max_requests=max_requests)
    if not client.key:
        return {"ok": False, "version": VERSION, "reason": "TENNIS_RAPIDAPI_KEY_missing", "requests": 0, "betting_authority": False}

    state = db.fetchone("SELECT * FROM tennis_rich_context_state WHERE singleton_id=1") or {}
    last_fixture = str(state.get("last_fixture_sync_at") or "")
    fixtures = {}
    if not last_fixture or last_fixture[:10] != now.date().isoformat():
        fixtures = sync_upcoming_fixtures(db, client, now)

    rankings = sync_historical_rankings(db, client, now) if client.available else {"snapshots": 0, "rows_stored": 0}
    db.execute("UPDATE tennis_rich_context_state SET last_maintenance_at=? WHERE singleton_id=1", (_iso(now),))
    return {
        "ok": True,
        "version": VERSION,
        "requests": client.requests,
        "errors": client.errors,
        "fixtures": fixtures,
        "historical_rankings": rankings,
        "betting_authority": False,
    }


def match_provider_player(db, tour: str, name: str) -> Optional[Dict[str, Any]]:
    norm = _norm_name(name)
    if not norm:
        return None
    row = db.fetchone(
        """
        SELECT * FROM tennis_rich_players
        WHERE tour=? AND player_name_norm=?
        ORDER BY last_seen_at DESC,id DESC LIMIT 1
        """,
        (tour.lower(), norm),
    )
    return dict(row) if row else None


def fixture_context(db, tour: str, p1: str, p2: str, commence_time: Optional[str] = None) -> Optional[Dict[str, Any]]:
    n1, n2 = _norm_name(p1), _norm_name(p2)
    rows = db.fetchall(
        """
        SELECT * FROM tennis_rich_fixtures
        WHERE tour=? AND start_time>=? ORDER BY start_time,provider_match_id
        """,
        (tour.lower(), (_now() - timedelta(days=1)).isoformat()),
    )
    best = None
    for row in rows:
        a, b = _norm_name(row.get("player1_name")), _norm_name(row.get("player2_name"))
        if {a,b} != {n1,n2}:
            continue
        best = dict(row)
        if commence_time and str(row.get("start_time") or "")[:10] == str(commence_time)[:10]:
            return best
    return best
