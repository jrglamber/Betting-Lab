import unittest
from unittest.mock import patch
from datetime import datetime, timezone

import tennis_result_priority_budget_patch as priority


class ResultPriorityBudgetTests(unittest.TestCase):
    def test_research_cannot_consume_reserved_result_budget(self):
        self.assertEqual(priority._research_allowance(29_500, 30_000), 0)
        self.assertEqual(priority._research_allowance(29_300, 30_000), 200)
        self.assertEqual(priority._research_allowance(0, 30_000), 29_500)

    def test_optional_stats_drain_stops_at_small_daily_limit(self):
        self.assertEqual(priority._optional_stats_allowance(100, 30_000, 0), 24)
        self.assertEqual(priority._optional_stats_allowance(100, 30_000, 23), 1)
        self.assertEqual(priority._optional_stats_allowance(100, 30_000, 24), 0)
        self.assertEqual(priority._optional_stats_allowance(29_490, 30_000, 0), 10)

    def test_existing_rich_results_fetch_is_not_wrapped(self):
        # The patch changes only rich.maintenance and match-stats enrichment;
        # the result collector creates its own client and remains untouched.
        called = []
        def original_maintenance(db, now=None, max_requests=150):
            called.append(max_requests)
            return {"ok": True, "requests": max_requests}
        def original_stats(db, client, limit=100):
            return {"fetched": limit, "nonempty": limit}

        old = priority._INSTALLED
        priority._INSTALLED = False
        try:
            with (
                patch.object(priority.rich, "maintenance", original_maintenance),
                patch.object(priority.rich, "sync_missing_match_stats", original_stats),
                patch.object(priority.rich, "_used_today", return_value=29_400),
                patch.object(priority, "_stats_used_today", return_value=0),
                patch.object(priority.rich, "DAILY_REQUEST_CAP", 30_000),
            ):
                priority.install()
                run = priority.rich.maintenance(object(), max_requests=150)
                self.assertEqual(run["requests"], 100)
                self.assertEqual(called, [100])
                self.assertEqual(
                    priority.rich.sync_missing_match_stats(object(), object(), limit=30)["fetched"],
                    24,
                )
        finally:
            priority._INSTALLED = old

    def test_free_plan_reserves_forty_for_results_and_ten_for_tpred_context(self):
        self.assertTrue(priority._free_tier(50))
        self.assertEqual(priority._result_reserve(50), 40)
        self.assertEqual(priority._stats_daily_cap(50), 0)
        self.assertEqual(priority._research_allowance(0, 50), 10)
        self.assertEqual(priority._research_allowance(45, 50), 0)
        self.assertEqual(priority._optional_stats_allowance(0, 50, 0), 0)
        self.assertFalse(priority._free_tier(500))
        self.assertEqual(priority._result_reserve(30_000), 500)

    def test_free_plan_runs_weekly_rankings_only_and_keeps_stats_paused(self):
        called = []
        now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        class DB:
            def __init__(self):
                self.updated = []
                self.last_rankings_at = "2026-10-26T00:00:00+00:00"
            def fetchone(self, *args):
                return {"last_rankings_at": self.last_rankings_at}
            def execute(self, sql, args):
                self.updated.append((sql, args))
        class Client:
            def __init__(self, db, max_requests):
                self.key = "configured"
                self.max_requests = max_requests
                self.requests = 0
                self.errors = 0
            @property
            def available(self):
                return self.requests < self.max_requests
        def rankings(db, client, tour, now):
            client.requests += 2
            called.append(tour)
            return {"seen": 900, "stored": 900}
        def original_maintenance(db, now=None, max_requests=150):
            called.append("HISTORICAL_RESEARCH_SHOULD_NOT_RUN")
            return {"requests": 1}
        def original_stats(db, client, limit=100):
            called.append("MATCH_STATS_SHOULD_NOT_RUN")
            return {"fetched": limit}
        db = DB()
        old = priority._INSTALLED
        priority._INSTALLED = False
        try:
            with (
                patch.object(priority.rich, "maintenance", original_maintenance),
                patch.object(priority.rich, "sync_missing_match_stats", original_stats),
                patch.object(priority.rich, "install_schema"),
                patch.object(priority.rich, "RichClient", Client),
                patch.object(priority.rich, "sync_current_rankings", rankings),
                patch.object(priority.rich, "_used_today", return_value=0),
                patch.object(priority, "_stats_used_today", return_value=0),
                patch.object(priority.rich, "DAILY_REQUEST_CAP", 50),
            ):
                status = priority.install()
                self.assertEqual(status["mode"], "FREE_RESULTS_TPRED_CONTEXT")
                self.assertEqual(status["result_reserve"], 40)
                self.assertEqual(status["free_fixtures_daily_budget"], 4)
                run = priority.rich.maintenance(db, now=now)
                self.assertEqual(run["requests"], 4)
                self.assertEqual(called, ["atp", "wta"])
                self.assertEqual(len(db.updated), 1)
                self.assertEqual(
                    priority.rich.sync_missing_match_stats(db, object())["fetched"], 0
                )
                db.last_rankings_at = now.isoformat()
                run2 = priority.rich.maintenance(db, now=now)
                self.assertEqual(run2["reason"], "free_rankings_current_week")
                self.assertEqual(run2["requests"], 0)
                self.assertEqual(called, ["atp", "wta"])
        finally:
            priority._INSTALLED = old

    def test_free_ranking_does_not_take_protected_result_or_fixture_budget(self):
        class DB:
            def fetchone(self, *args):
                return {"last_rankings_at": "2026-10-01T00:00:00Z"}
        old = priority._INSTALLED
        priority._INSTALLED = False
        try:
            with (
                patch.object(priority.rich, "maintenance", lambda *args, **kwargs: {}),
                patch.object(priority.rich, "sync_missing_match_stats", lambda *args, **kwargs: {}),
                patch.object(priority.rich, "install_schema"),
                patch.object(priority.rich, "_used_today", return_value=8),
                patch.object(priority.rich, "DAILY_REQUEST_CAP", 50),
            ):
                priority.install()
                run = priority.rich.maintenance(DB(), now=datetime(2026, 11, 5, tzinfo=timezone.utc))
                self.assertEqual(run["reason"], "free_rankings_budget_unavailable")
                self.assertEqual(run["requests"], 0)
        finally:
            priority._INSTALLED = old

    def test_no_stats_requests_if_ledger_is_unreadable(self):
        class DB:
            def fetchone(self, *args, **kwargs):
                raise RuntimeError("no table")
        with patch.object(priority.rich, "DAILY_REQUEST_CAP", 50):
            self.assertEqual(priority._stats_used_today(DB()), 0)
        with patch.object(priority.rich, "DAILY_REQUEST_CAP", 30_000):
            self.assertEqual(priority._stats_used_today(DB()), priority.PAID_OPTIONAL_STATS_DAILY_CAP)


if __name__ == "__main__":
    unittest.main()
