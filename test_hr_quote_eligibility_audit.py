"""Read-only HR quote eligibility accounting: no provider or bookmaker calls."""
from datetime import datetime, timedelta, timezone

import pytest

import hr_quote_eligibility_audit as audit
import hit_rate_multiples_lab as hr
import credit_policy_unlimited_patch as base


class FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self, sql, args=()):
        assert "hit_rate_candidates" in sql
        return self.rows


def candidate(i, now, hours, policy=None, exp="HR2", system="DOUBLE"):
    return {
        "id": i, "policy_version": policy or hr.POLICY_VERSION,
        "experiment_key": exp, "system_type": system,
        "event_id": f"e{i}", "sport_key": "soccer_test",
        "home_team": f"Home{i}", "away_team": f"Away{i}",
        "commence_time": (now + timedelta(hours=hours)).isoformat(),
        "source_lane": "FOOTBALL_H2H_2_50_2_99",
    }


@pytest.mark.parametrize("within,expected", [
    (0, "FEWER_THAN_TWO_WITHIN_HORIZON"),
    (1, "FEWER_THAN_TWO_WITHIN_HORIZON"),
    (2, "NATIVE_DUE_FILTER_EMPTY"),
])
def test_audit_horizon_is_read_only_and_groups_distinct_events(monkeypatch, within, expected):
    now = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
    rows = [candidate(i, now, 12 if i <= within else 42) for i in (1, 2, 3)]
    rows += [candidate(4, now, 5, exp="HR1"), candidate(5, now, -2)]
    # Repeated selection of an already counted fixture must not create another event.
    duplicate = dict(rows[0], id=10)
    rows.append(duplicate)
    monkeypatch.setattr(base, "_ORIGINAL_HRM_TARGETS", lambda db, stamp: [])
    monkeypatch.setattr(hr, "_targets", lambda db, stamp: [])
    monkeypatch.setattr(hr, "_credits_today", lambda db, stamp: 0)

    result = audit.snapshot(FakeDB(rows), now)
    assert result["queued_hr2_double_rows"] == 5
    assert result["distinct_hr2_double_fixtures"] == 4
    assert result["within_30h"] == within
    assert result["outside_30h"] == 3 - within
    assert result["expired"] == 1
    assert result["base_due_events"] == 0
    assert result["actual_due_events"] == 0
    assert result["no_provider_calls"] is True
    assert result["diagnosis"] == expected


def test_audit_detects_downstream_pacing_block(monkeypatch):
    now = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
    rows = [candidate(1, now, 2), candidate(2, now, 3)]
    monkeypatch.setattr(base, "_ORIGINAL_HRM_TARGETS", lambda db, stamp: [{"event_id": "e1"}])
    monkeypatch.setattr(hr, "_targets", lambda db, stamp: [])
    monkeypatch.setattr(hr, "_credits_today", lambda db, stamp: 0)
    assert audit.snapshot(FakeDB(rows), now)["diagnosis"] == "PACING_OR_BACKOFF"


def test_audit_validates_quote_targets_when_due(monkeypatch):
    now = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
    rows = [candidate(1, now, 2), candidate(2, now, 3)]
    monkeypatch.setattr(base, "_ORIGINAL_HRM_TARGETS", lambda db, stamp: [{"event_id": "e1"}])
    monkeypatch.setattr(hr, "_targets", lambda db, stamp: [{"event_id": "e1"}])
    monkeypatch.setattr(hr, "_credits_today", lambda db, stamp: 0)
    assert audit.snapshot(FakeDB(rows), now)["diagnosis"] == "ELIGIBLE_TO_POLL"
