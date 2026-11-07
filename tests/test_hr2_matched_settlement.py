import unittest
from hr2_matched_settlement import compare_four


class MatchedSettlementTests(unittest.TestCase):
    def legs(self, results):
        return [dict(bookmaker_key="williamhill", event_id=str(i),
                     odds=2.0, result=result) for i, result in enumerate(results)]

    def test_four_wins(self):
        x = compare_four(self.legs(["WIN"] * 4))
        self.assertAlmostEqual(x["SINGLES"]["return_units"], 2)
        self.assertAlmostEqual(x["DOUBLES"]["return_units"], 4)
        self.assertAlmostEqual(x["YANKEE"]["return_units"], 52 / 11)
        self.assertEqual(x["YANKEE"]["bets"], 11)

    def test_all_losses(self):
        x = compare_four(self.legs(["LOSS"] * 4))
        for v in x.values():
            self.assertEqual(v["pnl_units"], -1)

    def test_two_wins(self):
        x = compare_four(self.legs(["WIN", "WIN", "LOSS", "LOSS"]))
        self.assertAlmostEqual(x["DOUBLES"]["return_units"], 4 / 6)
        self.assertAlmostEqual(x["YANKEE"]["return_units"], 4 / 11)

    def test_reject_cross_book(self):
        legs = self.legs(["WIN"] * 4)
        legs[0]["bookmaker_key"] = "ladbrokes"
        with self.assertRaises(ValueError):
            compare_four(legs)


if __name__ == "__main__":
    unittest.main()
