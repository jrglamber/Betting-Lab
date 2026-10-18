from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import tennis_predictive3 as tp3
import tennis_predictive3_safety_patch as safety
import tennis_rich_context as rich_context
import tennis_rich_source as rich_source


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
    rich_source.install_schema(db)
    rich_context.install_schema(db)
    tp3.install_schema(db)
    safety.install()
    return db


def _stats_payload():
    return json.dumps({
        "data": {
            "player1Stats": {
                "firstServeOf": 100,
                "winningOnFirstServe": 45,
                "winningOnFirstServeOf": 62,
                "winningOnSecondServe": 20,
                "winningOnSecondServeOf": 38,
                "returnPtsWin": 38,
                "returnPtsWinOf": 100,
                "aces": 8,
                "doubleFaults": 3,
                "breakPointFacedGm": 6,
                "breakPointSavedGm": 4,
            },
            "player2Stats": {
                "firstServeOf": 100,
                "winningOnFirstServe": 40,
                "winningOnFirstServeOf": 60,
                "winningOnSecondServe": 18,
                "winningOnSecondServeOf": 40,
                "returnPtsWin": 35,
                "returnPtsWinOf": 100,
                "aces": 5,
                "doubleFaults": 4,
                "breakPointFacedGm": 8,
                "breakPointSavedGm": 5,
            },
        }
    })


def test_tpred3_safety_removes_winner_first_orientation_and_parses_exact_stats():
    row = {
        "player1_id": 200,
        "player1_name": "Winner",
        "player2_id": 100,
        "player2_name": "Loser",
        "stats_json": _stats_payload(),
    }
    ordered = safety._ordered_match(row)
    assert ordered is not None
    assert ordered["p1id"] == 100
    assert ordered["p2id"] == 200
    assert ordered["y"] == 0

    winner_stats, loser_stats = safety._exact_stats(_stats_payload())
    assert round(winner_stats["serve_points_won"], 2) == 0.65
    assert round(winner_stats["return_points_won"], 2) == 0.38
    assert round(loser_stats["serve_points_won"], 2) == 0.58
    assert round(loser_stats["return_points_won"], 2) == 0.35


def test_tpred3_chronological_walkforward_and_forward_freeze():
    db = _db()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    base = now - timedelta(days=230)
    players = [(101,"Alpha"),(102,"Bravo"),(103,"Charlie"),(104,"Delta")]

    for pid, name in players:
        db.execute(
            """INSERT INTO tennis_rich_players(
                tour,provider_player_id,player_name,player_name_norm,country,first_seen_at,last_seen_at
            ) VALUES(?,?,?,?,?,?,?)""",
            ("atp",pid,name,name.lower(),"GBR",base.isoformat(),now.isoformat()),
        )

    for week in range(40):
        day = (base + timedelta(days=7*week)).date().isoformat()
        for i, (pid,name) in enumerate(players):
            db.execute(
                """INSERT INTO tennis_rich_rankings(
                    ranking_date,tour,provider_player_id,player_name,ranking,ranking_points,fetched_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (day,"atp",pid,name,12+i*13,5200-i*650,(base+timedelta(days=7*week)).isoformat()),
            )

    # Provider completed-results rows are winner-first. Alternate winners and
    # player-ID order so a model that accidentally learns presentation order
    # would be exposed by the safety orientation test above.
    for i in range(220):
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
                _stats_payload(),at.isoformat(),at.isoformat(),
            ),
        )

    rebuilt = tp3.rebuild_walkforward(db)
    assert rebuilt["walkforward_rows"] == 220
    assert rebuilt["evidence_ready"] > 0
    assert rebuilt["result_order_leakage"] is False

    score = tp3.scoreboard(db)
    assert score["all_walkforward"]["samples"] == 220
    assert score["evidence_ready_walkforward"]["samples"] > 0
    assert score["betting_authority"] is False

    future = now + timedelta(days=2)
    db.execute("INSERT INTO tennis_events VALUES(?,?,?,?,?,?,?,?,?)", (
        "future","tennis_atp_test","Test Open","ATP","TOUR_EVENT",future.isoformat(),"Alpha","Bravo","UPCOMING"))
    db.execute(
        """INSERT INTO tennis_rich_fixtures(
            tour,provider_match_id,start_time,player1_id,player1_name,player2_id,player2_name,
            tournament_id,tournament_name,surface,tour_rank,round_id,discovered_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("atp",9001,future.isoformat(),101,"Alpha",102,"Bravo",55,"Test Open","Hard",2,4,
         now.isoformat(),now.isoformat()),
    )
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
    assert all(r["surface"] == "Hard" for r in rows)
