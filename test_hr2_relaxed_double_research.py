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



class _ObservationDb:
    def __init__(self, observations, candidates):
        self.observations = observations
        self.candidates = candidates

    def fetchall(self, sql, params):
        cursor_id, limit = params
        return [
            row for row in self.observations
            if row["status"] == "AWAITING_SETTLEMENT" and row["id"] > cursor_id
        ][:limit]

    def fetchone(self, sql, params):
        return self.candidates.get(params[0])

    def execute(self, sql, params):
        status, stamp, pnl, outcomes, id_value = params
        row = next(x for x in self.observations if x["id"] == id_value)
        row.update(status=status, settled_at=stamp, pnl_units=pnl,
                   leg_results_json=outcomes)


class RelaxedDoubleSettlementTests(unittest.TestCase):
    def setUp(self):
        import hr2_relaxed_double_research as module
        module._SETTLE_CURSOR_ID = 0

    @staticmethod
    def _db():
        import json
        observation = dict(
            id=1, status="AWAITING_SETTLEMENT", combined_odds=4.25,
            candidate_ids_json=json.dumps([11, 22]),
            candidate_sources_json=json.dumps([
                dict(source_table="THEORETICAL_MULTISPORT_H2H", source_id=111),
                dict(source_table="THEORETICAL_PRED1_BTTS", source_id=222),
            ]),
        )
        candidates = {
            11: dict(id=11, source_table="THEORETICAL_MULTISPORT_H2H",
                     source_id=111, event_id="event-a", market_key="h2h",
                     selection="Team A"),
            22: dict(id=22, source_table="THEORETICAL_PRED1_BTTS",
                     source_id=222, event_id="event-b", market_key="btts",
                     selection="Yes"),
        }
        return _ObservationDb([observation], candidates)

    def test_settlement_resolves_full_candidate_not_only_source_ids(self):
        import sys
        import types
        from unittest.mock import patch
        from hr2_relaxed_double_research import settle
        db = self._db()
        seen = []
        def resolver(db_arg, leg):
            seen.append((leg["event_id"], leg["selection"]))
            return "WIN" if leg["event_id"] == "event-a" else "LOSS"
        hr = types.SimpleNamespace(_source_result=resolver, _result=lambda x: x)
        with patch.dict(sys.modules, {"hit_rate_multiples_lab": hr}):
            outcome = settle(db, NOW)
        self.assertEqual(outcome["settled_or_voided"], 1)
        self.assertEqual(seen, [("event-a", "Team A"), ("event-b", "Yes")])
        self.assertEqual(db.observations[0]["status"], "SETTLED")
        self.assertEqual(db.observations[0]["pnl_units"], -1.0)

    def test_missing_candidate_identity_cannot_be_settled(self):
        import sys
        import types
        from unittest.mock import patch
        from hr2_relaxed_double_research import settle
        db = self._db()
        db.candidates[22]["source_id"] = 999
        hr = types.SimpleNamespace(_source_result=lambda *a: "WIN",
                                   _result=lambda x: x)
        with patch.dict(sys.modules, {"hit_rate_multiples_lab": hr}):
            outcome = settle(db, NOW)
        self.assertEqual(outcome["settled_or_voided"], 0)
        self.assertEqual(outcome["missing_or_mismatched_candidates"], 1)
        self.assertEqual(db.observations[0]["status"], "AWAITING_SETTLEMENT")

if __name__ == "__main__":
    unittest.main()
