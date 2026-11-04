"""Regression: portfolio navigation and NCAAF maintenance must be Postgres-safe.

A raw percent sign in a psycopg2 SQL string with parameters causes
IndexError: tuple index out of range even though SQLite accepts it.
"""
from datetime import datetime, timedelta, timezone
import hr_portfolio_shadow as shadow
from db import Database


class CheckedSql:
    """Forward to a real SQLite database, but validate psycopg2 SQL formatting."""
    def __init__(self, db):
        self.db = db

    def __getattr__(self, name):
        return getattr(self.db, name)

    def fetchall(self, sql, params=()):
        # Postgres database adapter converts '?' placeholders to '%s'.
        # This dry-run catches unescaped '%' wildcard literals which psycopg2
        # interprets as formatting directives when parameter tuples are passed.
        if "LIKE" in sql:
            pg = sql.replace("?", "%s")
            _ = pg % tuple(repr(v) for v in params)
        return self.db.fetchall(sql, params)


def test_ncaaf_source_query_works_with_postgres_style_binding(tmp_path):
    db = Database(db_path=str(tmp_path / "portfolio_ncaaf.sqlite"))
    db.execute("""CREATE TABLE multisport_events(
        event_id TEXT PRIMARY KEY,sport_key TEXT,league_title TEXT,
        commence_time TEXT,home_team TEXT,away_team TEXT,status TEXT
    )""")
    db.execute("""CREATE TABLE multisport_execution_bets(
        id INTEGER PRIMARY KEY,created_at TEXT,event_id TEXT,status TEXT,
        selection TEXT,min_odds REAL,offered_odds REAL
    )""")
    now = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
    kickoff = now + timedelta(hours=2)
    db.execute("""INSERT INTO multisport_events VALUES(?,?,?,?,?,?,?)""",
               ("game-1", "americanfootball_ncaaf", "NCAAF",
                kickoff.isoformat(), "Team A", "Team B", "UPCOMING"))
    db.execute("""INSERT INTO multisport_execution_bets VALUES(?,?,?,?,?,?,?)""",
               (1, now.isoformat(), "game-1", "OPEN", "Team A", 3.0, 3.5))
    rows = shadow._ncaaf_candidates(CheckedSql(db), now)
    assert len(rows) == 1
    assert rows[0]["source_lane"] == "NCAAF_3_TO_4_99_EXPERIMENTAL"


def test_portfolio_status_and_ncaaf_count_with_postgres_binding(tmp_path):
    db = Database(db_path=str(tmp_path / "portfolio_status.sqlite"))
    shadow._schema(db)
    db.execute("""INSERT INTO hr_portfolio_groups(
        group_key,created_at,policy_version,cohort,bookmaker_key,
        selection_count,status,reference_stake_gbp
    ) VALUES(?,?,?,?,?,?,?,?)""", (
        "regression-1", "2026-11-04T12:00:00+00:00", shadow.VERSION,
        "UNIFIED", "williamhill", 2, "OPEN", 10.0,
    ))
    gid = db.fetchone("SELECT id FROM hr_portfolio_groups WHERE group_key=?",
                      ("regression-1",))["id"]
    for i, lane in enumerate(("NCAAF_3_TO_4_99_EXPERIMENTAL", "FOOTBALL_H2H_2_50_2_99"), 1):
        db.execute("""INSERT INTO hr_portfolio_legs(
            group_id,leg_order,source_lane,source_table,source_id,
            event_id,market_key,selection,entry_odds,bookmaker_key,quote_captured_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
            gid, i, lane, "multisport_execution_bets", i, f"game-{i}",
            "h2h", f"Team {i}", 3.5, "williamhill",
            "2026-11-04T11:59:00+00:00",
        ))
    result = shadow.status(CheckedSql(db))
    assert result["version"] == shadow.VERSION
    assert result["latest_groups"][0]["ncaaf_legs"] == 1
    assert result["recent_ncaaf_mix"]["some"] == 1
