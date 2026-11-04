"""Unit tests for the fixed forward HR portfolio accounting.

No external odds API, real betting or database required.
"""
from math import isclose

from hr_portfolio_shadow import _allocations, _pick_unique, _return_for


def _leg(result, odds=2.0):
    return {"result": result, "entry_odds": odds}


def test_four_winners_equal_line_stakes():
    legs = [_leg("WIN")] * 4
    assert isclose(_return_for("DOUBLE", legs), 4.0)
    assert isclose(_return_for("TRIXIE", legs), 5.0)
    assert isclose(_return_for("YANKEE", legs), 72.0 / 11.0)
    assert isclose(_return_for("SINGLES", legs), 2.0)


def test_losing_legs_are_not_treated_as_voids():
    legs = [_leg("WIN"), _leg("WIN"), _leg("LOSS"), _leg("LOSS")]
    assert isclose(_return_for("DOUBLE", legs), 4.0)
    assert isclose(_return_for("TRIXIE", legs), 1.0)
    assert isclose(_return_for("YANKEE", legs), 4.0 / 11.0)
    assert isclose(_return_for("SINGLES", legs), 1.5)


def test_void_leg_refunds_that_leg_in_combination():
    legs = [_leg("WIN"), _leg("VOID")]
    assert isclose(_return_for("DOUBLE", legs), 2.0)


def test_fixed_total_stake_is_not_triple_counted():
    for n in (2, 3, 4):
        a = _allocations(n)
        assert isclose(sum(a.values()), 1.0)
        assert a["SINGLES"] == 0.0
    assert _allocations(4)["DOUBLE"] == 0.5


def test_forced_ncaaf_is_one_of_distinct_events():
    rows = [
        ({"event_id": "A", "source_lane": "NORMAL", "source_created_at": "2026-11-04T01:00:00Z",
          "market_key": "h2h", "selection": "a"}, {"price": 2}),
        ({"event_id": "A", "source_lane": "NORMAL", "source_created_at": "2026-11-04T01:00:01Z",
          "market_key": "h2h", "selection": "other"}, {"price": 2}),
        ({"event_id": "B", "source_lane": "NCAAF_3_TO_4_99_EXPERIMENTAL",
          "source_created_at": "2026-11-04T02:00:00Z",
          "market_key": "h2h", "selection": "b"}, {"price": 4}),
    ]
    picked = _pick_unique(rows, force_ncaaf=True)
    assert len(picked) == 2
    assert picked[0][0]["event_id"] == "B"
    assert {x[0]["event_id"] for x in picked} == {"A", "B"}


def test_ncaaf_cohort_cannot_be_formed_without_ncaaf():
    rows = [
        ({"event_id": "A", "source_lane": "NORMAL", "source_created_at": "2026-11-04",
          "market_key": "h2h", "selection": "a"}, {"price": 2}),
        ({"event_id": "B", "source_lane": "NORMAL", "source_created_at": "2026-11-04",
          "market_key": "h2h", "selection": "b"}, {"price": 2}),
    ]
    assert _pick_unique(rows, force_ncaaf=True) == []
