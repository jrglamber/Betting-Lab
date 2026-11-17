"""Coverage tests for HR2 quote-only result polling (no provider calls)."""
import json
import unittest
from datetime import datetime, timezone

from hit_rate_theoretical_result_collection_patch_v2 import (
    _hr2_quote_observation_events,
    _pending_hr_multisport,
    _pending_hr_tennis,
)

NOW = datetime(2026, 11, 17, 12, 8, tzinfo=timezone.utc)


class FakeDb:
    def __init__(self, *, resolved=False):
        self.resolved = resolved
        self.candidates = [
            {"id": 1, "source_table": "THEORETICAL_MULTISPORT_H2H", "event_id": "nfl-1"},
            {"id": 2, "source_table": "THEORETICAL_TENNIS_H2H", "event_id": "tennis-1"},
            {"id": 3, "source_table": "THEORETICAL_MULTISPORT_H2H", "event_id": "nfl-old"},
        ]
        self.events = {
            "multisport_events": [
                {"event_id": "nfl-1", "commence_time": "2026-11-15T18:00:00Z",
                 "sport_family": "americanfootball", "sport_key": "americanfootball_nfl"},
                {"event_id": "nfl-old", "commence_time": "2026-11-12T10:00:00Z",
                 "sport_family": "americanfootball", "sport_key": "americanfootball_nfl"},
            ],
            "tennis_events": [
                {"event_id": "tennis-1", "commence_time": "2026-11-16T12:00:00Z",
                 "sport_key": "tennis_atp_finals"},
            ],
        }

    def fetchall(self, query, params=()):
        if "FROM hit_rate_legs l" in query:
            return []
        if "FROM hr2_relaxed_double_observations" in query:
            return [
                {"candidate_ids_json": json.dumps([1, 2, 3])},
                {"candidate_ids_json": json.dumps([1, 2])},
            ]
        if "FROM hit_rate_candidates" in query:
            source = params[0]
            ids = set(params[1:])
            return [
                {"event_id": c["event_id"]} for c in self.candidates
                if c["source_table"] == source and c["id"] in ids
            ]
        if "FROM multisport_events" in query:
            return [x for x in self.events["multisport_events"] if x["event_id"] in params]
        if "FROM tennis_events" in query:
            return [x for x in self.events["tennis_events"] if x["event_id"] in params]
        if "FROM multisport_results" in query:
            return [{"event_id": "nfl-1"}] if self.resolved else []
        if "FROM tennis_results" in query:
            return []
        raise AssertionError("Unexpected SQL: " + query)


class QuoteOnlyScoreCoverageTests(unittest.TestCase):
    def test_quote_only_nfl_due_within_three_days_not_old_events(self):
        db = FakeDb()
        found = _hr2_quote_observation_events(
            db, source_table="THEORETICAL_MULTISPORT_H2H",
            event_table="multisport_events", result_table="multisport_results", now=NOW,
        )
        self.assertEqual([e["event_id"] for e in found], ["nfl-1"])
        self.assertEqual([e["event_id"] for e in _pending_hr_multisport(db, NOW)], ["nfl-1"])

    def test_existing_results_not_repolled(self):
        db = FakeDb(resolved=True)
        found = _hr2_quote_observation_events(
            db, source_table="THEORETICAL_MULTISPORT_H2H",
            event_table="multisport_events", result_table="multisport_results", now=NOW,
        )
        self.assertEqual(found, [])

    def test_quote_only_tennis_due_and_distinct(self):
        db = FakeDb()
        found = _pending_hr_tennis(db, NOW)
        self.assertEqual([e["event_id"] for e in found], ["tennis-1"])


if __name__ == "__main__":
    unittest.main()
