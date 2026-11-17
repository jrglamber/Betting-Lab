"""No-network tests of SQLite volume migration and time-safe closing price."""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import betting_lab_lite_app as lite
from db import Database


class FakePg:
    def __init__(self):
        self.records = {
            "lite_runs": [
                dict(id=1,at="2026-11-17T13:40:00+00:00",kind="odds",
                     status="OK",credits=1,info="ok")
            ],
            "lite_picks": [
                dict(event_id="e1",observed_at="2026-11-17T11:00:00+00:00",
                     commence_time="2026-11-17T12:00:00+00:00",
                     home_team="A",away_team="B",selection="A",
                     bookmaker_key="williamhill",bookmaker_title="William Hill",
                     entry_odds=1.91,reference_probability=.55,
                     estimated_edge_pct=5,reference_books=3,
                     commission_assumption=0,result=None,home_score=None,
                     away_score=None,pnl_units=None,settled_at=None)
            ],
            "lite_quotes": [
                dict(event_id="e1",captured_at="2026-11-17T11:30:00+00:00",
                     bookmaker_key="williamhill",selection="A",price=1.8,
                     commence_time="2026-11-17T12:00:00+00:00")
            ]
        }

    def fetchall(self, query):
        for table,records in self.records.items():
            if "FROM " + table in query:
                return records
        raise AssertionError("Unexpected Postgres query " + query)


class SqliteMigrationTests(unittest.TestCase):
    def test_migration_preserves_new_lite_rows_and_closing_quotes(self):
        with tempfile.TemporaryDirectory() as folder:
            filepath=os.path.join(folder,"lite.db")
            db=Database(database_url="",db_path=filepath)
            with patch.object(lite,"db",db), \
                 patch.object(lite,"SQLITE_PATH",filepath), \
                 patch.object(lite,"LEGACY_PG","postgresql://placeholder"), \
                 patch.object(lite,"Database",return_value=FakePg()), \
                 patch.object(lite.os.path,"ismount",return_value=True):
                lite.schema()
                self.assertEqual(db.fetchone("SELECT COUNT(*) AS n FROM lite_runs")["n"],1)
                self.assertEqual(db.fetchone("SELECT COUNT(*) AS n FROM lite_picks")["n"],1)
                self.assertEqual(db.fetchone("SELECT COUNT(*) AS n FROM lite_quotes")["n"],1)
                self.assertIsNotNone(db.fetchone("SELECT * FROM lite_migration_status WHERE id=1"))
                # Idempotent; a second start never overwrites ongoing Lite rows.
                lite.schema()
                self.assertEqual(db.fetchone("SELECT COUNT(*) AS n FROM lite_picks")["n"],1)
                measured=lite.refresh_closing_prices(datetime(2026,11,17,14,tzinfo=timezone.utc))
                self.assertEqual(measured,1)
                pick=db.fetchone("SELECT * FROM lite_picks WHERE event_id=?",("e1",))
                self.assertAlmostEqual(float(pick["closing_odds"]),1.8)
                self.assertGreater(float(pick["closing_clv_pct"]),0)
                self.assertEqual(lite.refresh_closing_prices(
                    datetime(2026,11,17,14,tzinfo=timezone.utc)),0)

    def test_unmounted_or_missing_migration_source_refuses_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            filepath=os.path.join(folder,"lite.db")
            db=Database(database_url="",db_path=filepath)
            with patch.object(lite,"db",db), \
                 patch.object(lite,"SQLITE_PATH",filepath), \
                 patch.object(lite,"LEGACY_PG",""), \
                 patch.object(lite.os.path,"ismount",return_value=False):
                with self.assertRaisesRegex(RuntimeError,"not mounted"):
                    lite.schema()
            with patch.object(lite,"db",db), \
                 patch.object(lite,"SQLITE_PATH",filepath), \
                 patch.object(lite,"LEGACY_PG",""), \
                 patch.object(lite.os.path,"ismount",return_value=True):
                with self.assertRaisesRegex(RuntimeError,"migration source unavailable"):
                    lite.schema()


if __name__=="__main__":
    unittest.main()
