import unittest
from datetime import datetime, timezone
from hr2_relaxed_double_research import compare_doubles

NOW = datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc)


def candidate(i):
    return dict(id=i, event_id=str(i), fixture_key="fixture-"+str(i),
                market_key="totals", selection="Under", point=2.5,
                first_seen_at="2026-11-09T12:00:00+00:00",
                min_odds=2.0, band_min=1.8, band_max=3.0)


def quote(i, book="williamhill", price=1.94, minute=45):
    return dict(event_id=str(i), market_key="totals", selection="Under",
                point=2.5, bookmaker_key=book, price=price,
                captured_at=f"2026-11-09T13:{minute:02d}:00+00:00")


class RelaxedDoubleTests(unittest.TestCase):
    def test_relaxed_only_for_price_and_spread(self):
        result = compare_doubles([candidate(1),candidate(2)],
                                 [quote(1,minute=40),quote(2,minute=55)], NOW)
        self.assertEqual(len(result["strict"]),0)
        self.assertEqual(len(result["relaxed"]),1)

    def test_same_book_required(self):
        result = compare_doubles([candidate(1),candidate(2)],
                                 [quote(1),quote(2,book="ladbrokes")],NOW)
        self.assertEqual(len(result["relaxed"]),0)

    def test_no_same_fixture(self):
        a,b=candidate(1),candidate(2)
        b["fixture_key"]=a["fixture_key"]
        result=compare_doubles([a,b],[quote(1),quote(2)],NOW)
        self.assertEqual(len(result["relaxed"]),0)

    def test_price_floor(self):
        result=compare_doubles([candidate(1),candidate(2)],
                               [quote(1,price=1.89),quote(2)],NOW)
        self.assertEqual(len(result["relaxed"]),0)

    def test_quote_identity(self):
        q=quote(1)
        q["selection"]="Over"
        result=compare_doubles([candidate(1),candidate(2)],[q,quote(2)],NOW)
        self.assertEqual(len(result["relaxed"]),0)


if __name__ == "__main__":
    unittest.main()
