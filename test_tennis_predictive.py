from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import tennis_predictive as tp


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

    def executemany(self, sql, rows):
        self.conn.executemany(sql, list(rows))
        self.conn.commit()

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
    db.execute("""CREATE TABLE tennis_execution_bets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT,event_id TEXT,offered_odds REAL,
        edge_pct REAL,status TEXT,pnl_units REAL,net_pnl_units REAL,clv_pct REAL,
        clv_quality TEXT,reference_book_count INTEGER)""")
    return db


def test_tpred_walkforward_and_forward_forecast():
    db = _db()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(20):
        p1 = "A" if i % 2 == 0 else "B"
        p2 = "B" if i % 2 == 0 else "A"
        winner = "A" if i % 3 else "B"
        at = base + timedelta(days=i)
        event_id = f"e{i}"
        db.execute("INSERT INTO tennis_events VALUES(?,?,?,?,?,?,?,?,?)", (
            event_id,"tennis_atp_test","Test","ATP","TOUR_EVENT",at.isoformat(),p1,p2,"COMPLETED"))
        db.execute("INSERT INTO tennis_results VALUES(?,?,?,?,?,?,?,?,?,?)", (
            event_id,at.isoformat(),at.isoformat(),winner,None,None,"test","A","TEST","{}"))
        db.execute("""INSERT INTO tennis_consensus_snapshots(
            event_id,sport_key,captured_at,selection,fair_probability,fair_odds,num_books,
            price_dispersion_pct,mean_overround_pct,model) VALUES(?,?,?,?,?,?,?,?,?,?)""", (
            event_id,"tennis_atp_test",(at-timedelta(hours=2)).isoformat(),p1,
            0.55,1/0.55,10,2.0,5.0,"test"))

    future = base + timedelta(days=30)
    db.execute("INSERT INTO tennis_events VALUES(?,?,?,?,?,?,?,?,?)", (
        "future","tennis_atp_test","Test","ATP","TOUR_EVENT",future.isoformat(),"A","B","UPCOMING"))
    db.execute("""INSERT INTO tennis_consensus_snapshots(
        event_id,sport_key,captured_at,selection,fair_probability,fair_odds,num_books,
        price_dispersion_pct,mean_overround_pct,model) VALUES(?,?,?,?,?,?,?,?,?,?)""", (
        "future","tennis_atp_test",(future-timedelta(hours=2)).isoformat(),"A",
        0.60,1/0.60,12,2.0,5.0,"test"))

    tp.install_schema(db)
    rebuilt = tp.rebuild_walkforward(db)
    assert rebuilt["created"]["TPRED1"] == 20
    assert rebuilt["created"]["TPRED2"] == 20
    score = tp.scoreboard(db)
    assert score["models"]["TPRED1"]["evidence_ready_walkforward"]["samples"] > 0

    created = tp.create_forward_predictions(db, future - timedelta(hours=3))
    assert created == {"TPRED1": 2, "TPRED2": 2}
    assert len(db.fetchall("SELECT * FROM tennis_predictive_predictions")) == 4
    assert score["betting_authority"] is False
