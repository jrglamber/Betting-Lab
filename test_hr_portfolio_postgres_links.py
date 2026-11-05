"""Regression: standard HR2 NCAAF portfolio queries must be Postgres-safe.

Former parallel NCAAF SELECT used a raw '%' and failed psycopg formatting.
Use the canonical HR2 queue instead. Old frozen cards remain readable.
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


def test_canonical_hr2_ncaaf_source_query_is_postgres_safe(tmp_path):
    # NCAAF is promoted into HR2 itself; the shadow no longer executes a
    # separate NCAAF-specific source query.
    db = Database(db_path=str(tmp_path / "portfolio_ncaaf.sqlite"))
    db.execute("""CREATE TABLE hit_rate_policy_versions(
        policy_version TEXT PRIMARY KEY,activated_at TEXT,is_active INTEGER
    )""")
    db.execute("""CREATE TABLE hit_rate_candidates(
        policy_version TEXT,experiment_key TEXT,status TEXT,first_seen_at TEXT,
        source_created_at TEXT,event_id TEXT,sport_key TEXT,source_lane TEXT,
        market_key TEXT,selection TEXT,point REAL,commence_time TEXT,
        source_table TEXT,source_id INTEGER,min_odds REAL,source_odds REAL
    )""")
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    db.execute("""INSERT INTO hit_rate_policy_versions VALUES(?,?,?)""", (
        "HRM_2026-10-12_V1", now.isoformat(), 1,
    ))
    db.execute("""INSERT INTO hit_rate_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        "HRM_2026-10-12_V1", "HR2", "QUEUED", now.isoformat(),
        now.isoformat(), "game-1", "americanfootball_ncaaf",
        "NCAAF_H2H_3_00_4_99", "h2h", "Team A", None,
        (now + timedelta(hours=2)).isoformat(),
        "multisport_execution_bets", 1, 3.0, 3.5,
    ))
    rows = shadow._base_candidates(CheckedSql(db), now)
    assert len(rows) == 1
    assert rows[0]["source_lane"] == "NCAAF_H2H_3_00_4_99"



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
