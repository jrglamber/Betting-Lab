import unittest

import singles_vs_multiples_roi_audit as audit


def leg(event, selection, odds, result):
    return {
        "event_id": event, "market_key": "h2h", "selection": selection,
        "point": None, "entry_odds": odds, "result": result,
    }


class MatchedRoiTests(unittest.TestCase):
    def test_equal_total_stake(self):
        cards = [
            {"multiple_pnl": 1.0, "legs": [
                leg("1", "A", 2.0, "WIN"), leg("2", "B", 3.0, "LOSS")]}
        ]
        s = audit._summary(cards)
        self.assertEqual(s["multiples_roi_pct"], 100.0)
        self.assertEqual(s["matched_singles_roi_pct"], 0.0)
        self.assertEqual(s["multiples_pnl_per_10_gbp"], 10.0)
        self.assertEqual(s["singles_pnl_per_10_gbp"], 0.0)

    def test_duplicate_selections_are_staked_each_card_and_deduped_separately(self):
        a = leg("1", "A", 2.0, "WIN")
        cards = [
            {"multiple_pnl": -1.0, "legs": [a, leg("2", "B", 2.0, "LOSS")]},
            {"multiple_pnl": 1.0, "legs": [a, leg("3", "C", 2.0, "WIN")]},
        ]
        s = audit._summary(cards)
        self.assertEqual(s["leg_appearances"], 4)
        self.assertEqual(s["unique_selections"], 3)
        self.assertEqual(s["duplicates_removed_in_unique_view"], 1)
        self.assertEqual(s["matched_singles_roi_pct"], 50.0)
        self.assertAlmostEqual(s["unique_singles_roi_pct"], 100/3, places=2)

    def test_void_refunds_stake(self):
        s = audit._summary([{
            "multiple_pnl": -1.0,
            "legs": [leg("1", "A", 2.2, "VOID"), leg("2", "B", 2.2, "LOSS")],
        }])
        self.assertEqual(s["matched_singles_roi_pct"], -50.0)

    def test_incomplete_cards_excluded(self):
        s = audit._summary([{
            "multiple_pnl": -1.0,
            "legs": [leg("1", "A", 2.0, "WIN"), leg("2", "B", 2.0, None)],
        }])
        self.assertEqual(s["settled_cards"], 0)
        self.assertEqual(s["skipped_incomplete"], 1)
        self.assertIsNone(s["matched_singles_roi_pct"])


if __name__ == "__main__":
    unittest.main()
