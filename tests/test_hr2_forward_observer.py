"""Regression tests for the isolated HR2 forward observer."""
import unittest
from unittest.mock import patch
import sys
import types

import hr2_forward_observer as observer


class FakeDB:
    def __init__(self):
        self.writes = []
        self.candidates = [
            dict(id=1, event_id="a", market_key="h2h", selection="A",
                 first_seen_at="2026-11-07T12:00:00Z", commence_time="2026-11-07T18:00:00Z"),
            dict(id=2, event_id="b", market_key="h2h", selection="B",
                 first_seen_at="2026-11-07T12:01:00Z", commence_time="2026-11-07T19:00:00Z"),
            dict(id=3, event_id="c", market_key="h2h", selection="C",
                 first_seen_at="2026-11-07T12:02:00Z", commence_time="2026-11-07T20:00:00Z"),
            dict(id=4, event_id="d", market_key="h2h", selection="D",
                 first_seen_at="2026-11-07T12:03:00Z", commence_time="2026-11-07T21:00:00Z"),
        ]
    def execute(self, sql, params=()):
        self.writes.append((sql, params))
    def fetchall(self, sql, params=()):
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
        self.assertEqual(result["priced_same_book_pools"], 1)
        inserts = [params for sql, params in db.writes if "INSERT INTO" in sql]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][-3:], (4, 6, 1))
        self.assertEqual(inserts[0][3], "williamhill")
        self.assertFalse(any("telegram" in sql.lower() or "UPDATE hit_rate" in sql for sql, _ in db.writes))

    def test_no_quotes_no_replay(self):
        db = FakeDB()
        mock = types.ModuleType("hit_rate_multiples_lab")
        mock._quotes = lambda *args: {}
        with patch.dict(sys.modules, {"hit_rate_multiples_lab": mock}):
            result = observer.capture(db)
        self.assertEqual(result["priced_same_book_pools"], 0)


if __name__ == "__main__":
    unittest.main()
