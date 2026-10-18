from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import tennis_predictive3 as tp3
import tennis_rich_context as rc
import tennis_rich_source as rs


class MiniDB:
    is_postgres = False

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, tuple(params))
        self.conn.commit()
        return max(0, int(cur.rowcount if cur.rowcount != -1 else 0))

    def fetchall(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def fetchone(self, sql, params=()):
        rows = self.fetchall(sql, params)
        return rows[0] if rows else None


def _db():
    db = MiniDB()
    db.execute("""CREATE TABLE tennis_events(
        event_id TEXT PRIMARY KEY,sport_key TEXT,tournament_title TEXT,tour TEXT,
        tournament_level TEXT,commence_time TEXT,player_one TEXT,player_two TEXT,status TEXT)""")
    db.execute("""CREATE TABLE tennis_results(
        event_id TEXT PRIMARY KEY,fetched_at TEXT,completed_at TEXT,winner TEXT,
        player_one_score INTEGER,player_two_score INTEGER,source TEXT,
        settlement_quality TEXT,settlement_provenance TEXT,raw_json TEXT)""")
    db.execute("""CREATE TABLE tennis_consensus_snapshots(
        id INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT,sport_key TEXT,captured_at TEXT,
        selection TEXT,fair_probability REAL,fair_odds REAL,num_books INTEGER,
        median_reference_odds REAL,best_reference_odds REAL,price_dispersion_pct REAL,
        mean_overround_pct REAL,model TEXT)""")
    rs.install_schema(db)
    rc.install_schema(db)
    tp3.install_schema(db)
    return db


def _stats(p1_id, p2_id, p1_service=0.64, p1_return=0.39, p2_service=0.61, p2_return=0.36):
    def row(pid, service, ret):
        attempts = 100
        first_in = 62
        first_won = int(service * 72)
        second_won = int(service * 28)
        return {
            "id": pid,
            "firstServe": first_in,
            "firstServeOf": attempts,
            "winningOnFirstServe": first_won,
            "winningOnFirstServeOf": first_in,
            "winningOnSecondServe": second_won,
            "winningOnSecondServeOf": attempts-first_in,
            "returnPtsWin": int(ret*100),
            "returnPtsWinOf": 100,
            "aces": 8,
            "doubleFaults": 3,
            "breakPointFacedGm": 6,
            "breakPointSavedGm": 4,
        }
    return json.dumps({"data": {"player1Stats": row(p1_id,p1_service,p1_return), "player2Stats": row(p2_id,p2_service,p2_return)}})


def test_tpred3_rich_walkforward_and_forward_freeze():
    db = _db()
    base = datetime(2025, 10, 1, tzinfo=timezone.utc)
    players = [(101,"Alpha"),(102,"Bravo"),(103,"Charlie"),(104,"Delta")]
    for pid, name in players:
        db.execute(
            """INSERT INTO tennis_rich_players(
                tour,provider_player_id,player_name,player_name_norm,country,first_seen_at,last_seen_at
            ) VALUES(?,?,?,?,?,?,?)""",
            ("atp",pid,name,name.lower(),"GBR",base.isoformat(),base.isoformat()),
        )

    # Weekly dated rankings: every historical match can only see a snapshot
    # dated on/before that match.
    for week in range(60):
        day = (base + timedelta(days=7*week)).date().isoformat()
        for i, (pid,name) in enumerate(players):
            db.execute(
                """INSERT INTO tennis_rich_rankings(
                    ranking_date,tour,provider_player_id,player_name,ranking,ranking_points,fetched_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (day,"atp",pid,name,10+i*15,5000-i*700,(base+timedelta(days=7*week)).isoformat()),
            )

    # 140 completed provider matches. Provider row order deliberately follows
    # winner-first semantics; TPRED3 must re-orient by stable player ID.
    for i in range(140):
        left = players[i % 4]
        right = players[(i+1) % 4]
        winner = left if i % 3 else right
        loser = right if winner == left else left
        at = base + timedelta(days=i)
        db.execute(
            """INSERT INTO tennis_rich_matches(
                tour,provider_match_id,match_date,player1_id,player1_name,player2_id,player2_name,
                winner_id,tournament_id,tournament_name,surface,tour_rank,round_id,result,result_type,
                stats_json,stats_fetched_at,discovered_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "atp",1000+i,at.isoformat(),winner[0],winner[1],loser[0],loser[1],winner[0],
                55,"Test Open","Hard",2,4,"6-4 6-4","completed",
                _stats(winner[0],loser[0]),at.isoformat(),at.isoformat(),
            ),
        )

    rebuilt = tp3.rebuild_walkforward(db)
    assert rebuilt["created"] == 140
    assert rebuilt["evidence_ready"] > 0
    score = tp3.scoreboard(db)
    assert score["all_walkforward"]["samples"] == 140
    assert score["evidence_ready_walkforward"]["samples"] > 0
    assert score["betting_authority"] is False

    future = base + timedelta(days=160)
    db.execute("INSERT INTO tennis_events VALUES(?,?,?,?,?,?,?,?,?)", (
        "future","tennis_atp_test","Test Open","ATP","TOUR_EVENT",future.isoformat(),"Alpha","Bravo","UPCOMING"))
    db.execute(
        """INSERT INTO tennis_rich_fixtures(
            tour,provider_match_id,start_time,player1_id,player1_name,player2_id,player2_name,
            tournament_id,tournament_name,surface,tour_rank,round_id,discovered_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("atp",9001,future.isoformat(),101,"Alpha",102,"Bravo",55,"Test Open","Hard",2,4,
         (future-timedelta(days=1)).isoformat(),(future-timedelta(days=1)).isoformat()),
    )
    now = future - timedelta(hours=24)
    for selection, fair in (("Alpha",0.58),("Bravo",0.42)):
        db.execute(
            """INSERT INTO tennis_consensus_snapshots(
                event_id,sport_key,captured_at,selection,fair_probability,fair_odds,num_books,
                price_dispersion_pct,mean_overround_pct,model
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("future","tennis_atp_test",now.isoformat(),selection,fair,1/fair,10,2.0,5.0,"test"),
        )

    made = tp3.create_forward_predictions(db, now)
    assert made["created"] == 2
    rows = db.fetchall("SELECT * FROM tennis_predictive3_predictions ORDER BY selection")
    assert len(rows) == 2
    assert all(r["status"] == "OPEN" for r in rows)
    assert all(r["surface"] == "HARD" for r in rows)
