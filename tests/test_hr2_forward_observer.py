"""Regression tests for the isolated HR2 forward observer."""
import unittest
from contextlib import contextmanager
from unittest.mock import patch
import sys
import types
from datetime import datetime, timedelta, timezone

import hr2_forward_observer as observer


class FakeDB:
    def __init__(self):
        self.writes = []
        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        self.candidates = [
            dict(id=1, event_id="a", market_key="h2h", selection="A",
                 first_seen_at="2026-11-07T12:00:00Z", commence_time=future),
            dict(id=2, event_id="b", market_key="h2h", selection="B",
                 first_seen_at="2026-11-07T12:01:00Z", commence_time=future),
            dict(id=3, event_id="c", market_key="h2h", selection="C",
                 first_seen_at="2026-11-07T12:02:00Z", commence_time=future),
            dict(id=4, event_id="d", market_key="h2h", selection="D",
                 first_seen_at="2026-11-07T12:03:00Z", commence_time=future),
        ]
    def _sql(self, sql):
        return sql
    @contextmanager
    def connection(self):
        db = self
        class Cursor:
            def execute(self, sql, params=()):
                db.writes.append((sql, params))
            def fetchone(self):
                return ("reservation_backfill_v1",)
            def fetchall(self):
                return []
        class Conn:
            def cursor(self):
                return Cursor()
        yield Conn()
    def execute(self, sql, params=()):
        self.writes.append((sql, params))
    def fetchone(self, sql, params=()):
        if "hr2_forward_schema_migrations" in sql:
            return {"migration_key": "reservation_backfill_v1"}
        return None
    def fetchall(self, sql, params=()):
        if "hr2_forward_reservations" in sql:
            return []
        assert "hit_rate_candidates" in sql
        return self.candidates


class ObserverTests(unittest.TestCase):
    def test_four_distinct_events_same_book(self):
        db = FakeDB()
        mock = types.ModuleType("hit_rate_multiples_lab")
        mock._quotes = lambda db, c, now, allow_min_miss: {
            "williamhill": {"price": 1.8, "captured_at": "2026-11-07T12:04:00Z"}}
        with patch.dict(sys.modules, {"hit_rate_multiples_lab": mock}):
            result = observer.capture(db)
        self.assertEqual(result["priced_same_book_pools_attempted"], 1)
        inserts = [params for sql, params in db.writes if "INSERT INTO hr2_forward_pool_snapshots" in sql]
        self.assertEqual(len([x for x in db.writes if "INSERT INTO hr2_forward_pool_snapshots" in x[0]]), 1)
        self.assertEqual(inserts[0][-3:], (4, 6, 1))
        self.assertEqual(inserts[0][3], "williamhill")
        self.assertFalse(any("telegram" in sql.lower() or "UPDATE hit_rate" in sql for sql, _ in db.writes))

    def test_already_reserved_cohort_is_not_recaptured(self):
        db = FakeDB()
        original = db.fetchall
        def fetchall(sql, params=()):
            if "FROM hr2_forward_reservations" in sql:
                return [{"candidate_id": i} for i in (1,2,3,4)]
            return original(sql, params)
        db.fetchall = fetchall
        mock = types.ModuleType("hit_rate_multiples_lab")
        mock._quotes = lambda db, c, now, allow_min_miss: {
            "williamhill": {"price": 1.8, "captured_at": now.isoformat()}}
        with patch.dict(sys.modules, {"hit_rate_multiples_lab": mock}):
            result = observer.capture(db)
        self.assertEqual(result["priced_same_book_pools_attempted"], 0)

    def test_no_quotes_no_replay(self):
        db = FakeDB()
        mock = types.ModuleType("hit_rate_multiples_lab")
        mock._quotes = lambda *args: {}
        with patch.dict(sys.modules, {"hit_rate_multiples_lab": mock}):
            result = observer.capture(db)
        self.assertEqual(result["priced_same_book_pools_attempted"], 0)


if __name__ == "__main__":
    unittest.main()
