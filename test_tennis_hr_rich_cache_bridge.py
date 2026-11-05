import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import tennis_result_pipeline_fix as pipeline


class HitRateRichCacheBridgeTests(unittest.TestCase):
    def test_hr_only_event_gets_canonical_result_without_provider_calls(self):
        event = {
            "event_id": "paris-nov4", "tour": "atp",
            "commence_time": "2026-11-04T11:20:00+00:00",
            "player_one": "Luca Van Assche", "player_two": "Learner Tien",
        }
        rich_match = {
            "provider_match_id": "source-paris",
            "tour": "atp", "match_date": "2026-11-04",
            "player1_name": "Luca Van Assche", "player2_name": "Learner Tien",
            "winner_id": "p1", "result": "7-6(4) 6-4",
            "result_type": "completed",
        }

        class FakeDB:
            def __init__(self):
                self.sql = []
            def fetchall(self, sql, params=None):
                self.sql.append(sql)
                if "FROM tennis_events e" in sql:
                    return [event]
                return []
            def fetchone(self, sql, params=None):
                self.sql.append(sql)
                if "SELECT winner FROM tennis_results" in sql:
                    return {"winner": "Luca Van Assche"}
                return {}
            def execute(self, sql, params=None):
                self.sql.append(sql)
            def record_collector_run(self, *args, **kwargs):
                pass

        db = FakeDB()
        with (
            patch.object(pipeline, "_candidate_rich_result", return_value={
                "matched": True, "winner": "Luca Van Assche", "row": rich_match,
            }),
            patch.object(pipeline, "settle_tennis_event", return_value=0),
            patch.object(pipeline.tpred3, "settle_forward_predictions", return_value=0),
            patch.object(pipeline.adaptive, "_tpred4_current_predictions", return_value={"ok": True}),
            patch.object(pipeline.adaptive, "run_tennis_w1", return_value={"ok": True}),
            patch("hit_rate_multiples_lab.settle_cards",
                  return_value={"touched": 1, "settled": 1}) as settle_hr,
        ):
            out = pipeline.bridge_cached_results(
                db, now=datetime(2026, 11, 5, 7, tzinfo=timezone.utc)
            )

        self.assertEqual(out["provider_calls"], 0)
        self.assertEqual(out["results_inserted"], 1)
        self.assertEqual(out["hit_rate_reconciliation"]["settled"], 1)
        self.assertTrue(any("FROM hit_rate_legs h" in sql for sql in db.sql))
        self.assertTrue(any("INTO tennis_results" in sql for sql in db.sql))
        settle_hr.assert_called_once_with(db)


if __name__ == "__main__":
    unittest.main()
