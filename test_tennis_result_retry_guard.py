from datetime import datetime, timedelta, timezone
import unittest
import tennis_result_retry_guard as retry


class RetryGuardTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 5, 7, 0, tzinfo=timezone.utc)

    def _row(self, hours_ago, status=429, purpose="match_stats_atp"):
        return {"requested_at": (self.now-timedelta(hours=hours_ago)).isoformat(),
                "status_code": status, "purpose": purpose}

    def test_stale_429_streak_does_not_block_results(self):
        state = retry._blocking_provider_state([self._row(47) for _ in range(8)],
                                               now=self.now)
        self.assertFalse(state["blocked"])
        self.assertEqual(state["statuses"], [])

    def test_fresh_quota_exhaustion_temporarily_blocks(self):
        state = retry._blocking_provider_state([self._row(0.05) for _ in range(8)],
                                               now=self.now)
        self.assertTrue(state["blocked"])
        self.assertEqual(state["reason"], "PROVIDER_QUOTA_429")

    def test_retries_after_one_hour(self):
        state = retry._blocking_provider_state([self._row(1.1) for _ in range(8)],
                                               now=self.now)
        self.assertFalse(state["blocked"])

    def test_bad_or_missing_timestamps_not_permanent_block(self):
        state = retry._blocking_provider_state(
            [{"requested_at": None, "status_code": 429}], now=self.now)
        self.assertFalse(state["blocked"])

    def test_fresh_subscription_failure_temporarily_blocks(self):
        state = retry._blocking_provider_state(
            [self._row(.25, status=403)], now=self.now)
        self.assertTrue(state["blocked"])
        self.assertEqual(state["reason"], "PROVIDER_AUTH_OR_PLAN")


if __name__ == "__main__":
    unittest.main()
