from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import tennis_predictive_runtime_patch as runtime
import tennis_result_priority_budget_patch as priority


class FreeTpredFixtureTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 6, 0, 10, tzinfo=timezone.utc)

    def test_no_provider_calls_until_priority_guard_installed(self):
        with patch.object(priority, "_INSTALLED", False):
            result = runtime._run_free_fixture_context(object(), self.now)
        self.assertEqual(result["reason"], "free_priority_guard_not_installed")
        self.assertEqual(result["requests"], 0)

    def test_new_daily_fixtures_use_small_budget_no_historical_rankings(self):
        class Db:
            def fetchone(self, *_args, **_kwargs):
                return {"last_fixture_sync_at": "2026-11-05T04:00:00Z"}
        class FakeClient:
            def __init__(self, db, max_requests):
                self.max_requests = max_requests
                self.requests = 0
                self.errors = 0
                self.available = True
        calls = []
        def fetch_fixtures(db, client, now):
            calls.append(("fixtures", client.max_requests))
            client.requests = 2
            return {"atp": {"seen": 60, "stored": 60},
                    "wta": {"seen": 40, "stored": 40}}
        with (
            patch.object(priority, "_INSTALLED", True),
            patch.object(runtime.rich_context, "install_schema"),
            patch.object(runtime.rich_context, "sync_upcoming_fixtures", fetch_fixtures),
            patch.object(runtime.rich_source, "_used_today", return_value=4),
            patch.object(runtime.rich_source, "DAILY_REQUEST_CAP", 50),
            patch.object(runtime.rich_source, "RichClient", FakeClient),
        ):
            result = runtime._run_free_fixture_context(Db(), self.now)
        self.assertEqual(result["mode"], "FREE_UPCOMING_FIXTURES_ONLY")
        self.assertEqual(result["requests"], 2)
        self.assertEqual(calls, [("fixtures", 4)])
        self.assertEqual(result["historical_rankings"]["skipped"],
                         "free_tier_weekly_current_rankings")

    def test_fixture_refresh_can_never_take_reserved_results_capacity(self):
        class Db:
            def fetchone(self, *_args, **_kwargs):
                return {"last_fixture_sync_at": "2026-11-04T00:00:00Z"}
        with (
            patch.object(priority, "_INSTALLED", True),
            patch.object(runtime.rich_context, "install_schema"),
            patch.object(runtime.rich_source, "_used_today", return_value=10),
            patch.object(runtime.rich_source, "DAILY_REQUEST_CAP", 50),
            patch.object(runtime.rich_source, "RichClient",
                         side_effect=AssertionError("must not call provider")),
        ):
            result = runtime._run_free_fixture_context(Db(), self.now)
        self.assertEqual(result["reason"], "free_fixture_budget_unavailable")
        self.assertEqual(result["requests"], 0)

    def test_already_fresh_context_does_not_fetch_again(self):
        class Db:
            def fetchone(self, *_args, **_kwargs):
                return {"last_fixture_sync_at": "2026-11-06T00:01:00Z"}
        with (
            patch.object(priority, "_INSTALLED", True),
            patch.object(runtime.rich_context, "install_schema"),
            patch.object(runtime.rich_source, "RichClient",
                         side_effect=AssertionError("should use cache")),
        ):
            result = runtime._run_free_fixture_context(Db(), self.now)
        self.assertEqual(result["reason"], "free_fixtures_already_current")
        self.assertEqual(result["requests"], 0)


if __name__ == "__main__":
    unittest.main()
