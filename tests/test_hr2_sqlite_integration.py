"""SQLite integration smoke test for HR2 research isolation and migration."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import sys
import types

from db import Database
import hr2_forward_observer as observer


class HR2SQLiteIntegration(unittest.TestCase):
    def test_capture_backfill_and_dedup_on_real_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(db_path=os.path.join(directory, "hr2.sqlite"))
            db.execute("""CREATE TABLE hit_rate_candidates (
                id INTEGER PRIMARY KEY, experiment_key TEXT, status TEXT,
                event_id TEXT, market_key TEXT, selection TEXT,
                first_seen_at TEXT, commence_time TEXT, point REAL
            )""")
            future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
            for i in range(1, 5):
                db.execute("""INSERT INTO hit_rate_candidates
                    (id,experiment_key,status,event_id,market_key,selection,first_seen_at,commence_time)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (i,"HR2","QUEUED",str(i),"h2h",str(i),
                     datetime.now(timezone.utc).isoformat(),future))
            mock = types.ModuleType("hit_rate_multiples_lab")
            mock._quotes = lambda db, c, now, allow_min_miss: {
                "williamhill": {"price": 1.8, "captured_at": now.isoformat()}}
            with patch.dict(sys.modules, {"hit_rate_multiples_lab": mock}):
                self.assertEqual(observer.capture(db)["priced_same_book_pools_attempted"], 1)
                self.assertEqual(observer.capture(db)["priced_same_book_pools_attempted"], 0)
            self.assertEqual(db.fetchone("SELECT COUNT(*) AS n FROM hr2_forward_pool_snapshots")["n"], 1)
            self.assertEqual(db.fetchone("SELECT COUNT(*) AS n FROM hr2_forward_reservations")["n"], 4)
            self.assertEqual(db.fetchone("SELECT COUNT(*) AS n FROM hr2_forward_schema_migrations")["n"], 1)


if __name__ == "__main__":
    unittest.main()
