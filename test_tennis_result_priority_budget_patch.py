import unittest
from unittest.mock import patch

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

    def test_no_stats_requests_if_ledger_is_unreadable(self):
        class DB:
            def fetchone(self, *args, **kwargs):
                raise RuntimeError("no table")
        self.assertEqual(priority._stats_used_today(DB()), priority.OPTIONAL_STATS_DAILY_CAP)


if __name__ == "__main__":
    unittest.main()
