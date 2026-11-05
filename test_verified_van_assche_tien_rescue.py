from __future__ import annotations

import unittest
from unittest.mock import patch

import verified_van_assche_tien_rescue as rescue


def _event(p1="Luca Van Assche", p2="Learner Tien", day="2026-11-04", tour="atp"):
    return {
        "event_id": "fixture-1", "commence_time": day + "T15:40:00Z",
        "tour": tour, "player_one": p1, "player_two": p2,
    }


class RescueTests(unittest.TestCase):
    def test_exact_pair_date_tour_and_diacritics(self):
        self.assertTrue(rescue._is_fixture(_event()))
        self.assertTrue(rescue._is_fixture(_event("Learner Tien", "Luca Van Assche")))
        self.assertFalse(rescue._is_fixture(_event("Luca Van Assche", "Other Player")))
        self.assertFalse(rescue._is_fixture(_event(day="2026-10-26")))
        self.assertFalse(rescue._is_fixture(_event(tour="wta")))

    def test_execution_grades_only_recognised_original_h2h_selection(self):
        class DB:
            def __init__(self):
                self.updated = []

            def fetchall(self, sql, params):
                self_id = params[0]
                assert self_id == "fixture-1"
                return [
                    {"id": 1, "selection": "Luca Van Assche", "offered_odds": 1.5, "bookmaker_key": "book1"},
                    {"id": 2, "selection": "Learner Tien", "offered_odds": 2.7, "bookmaker_key": "book1"},
                    {"id": 3, "selection": "Over 22.5", "offered_odds": 1.8, "bookmaker_key": "book1"},
                ]

            def execute(self, sql, params):
                self.updated.append(params)

        db = DB()
        with patch.object(rescue, "commission_adjusted_pnl", side_effect=lambda gross, _: (0.0, 0.0, gross)):
            result = rescue._settle_target_execution(db, "fixture-1")
        self.assertEqual(result["settled"], 2)
        self.assertEqual(len(result["unrecognised_selections"]), 1)
        self.assertEqual([params[0] for params in db.updated], ["WIN", "LOSS"])

    def test_existing_conflicting_result_is_never_overwritten(self):
        class DB:
            def __init__(self):
                self.writes = []

            def fetchall(self, sql, params):
                return [_event()]

            def fetchone(self, sql, params):
                return {"winner": "Learner Tien"}

            def execute(self, sql, params):
                self.writes.append((sql, params))

            def record_collector_run(self, *args, **kwargs):
                pass

        db = DB()
        result = rescue.run_once(db)
        self.assertEqual(result["matched_ids"], [])
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(result["inserted_results"], 0)
        self.assertEqual(db.writes, [])


if __name__ == "__main__":
    unittest.main()
