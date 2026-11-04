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
    assert isclose(_return_for("SINGLES", legs), 1.0)


def test_void_leg_refunds_that_leg_in_combination():
    legs = [_leg("WIN"), _leg("VOID")]
    assert isclose(_return_for("DOUBLE", legs), 2.0)


def test_fixed_total_stake_is_not_triple_counted():
    for n in (2, 3, 4):
        a = _allocations(n)
        assert isclose(sum(a.values()), 1.0)
        assert a["SINGLES"] == 0.0
    assert _allocations(4)["DOUBLE"] == 0.5


def _row(event, lane, created, price=2):
    return (
        {"event_id": event, "source_lane": lane, "source_created_at": created,
         "market_key": "h2h", "selection": event.lower()},
        {"price": price, "captured_at": "2026-11-04T12:00:00Z"},
    )


def test_unified_pool_can_have_zero_ncaaf():
    rows = [
        _row("A", "FOOTBALL", "2026-11-04T01:00:00Z"),
        _row("B", "EUROLEAGUE", "2026-11-04T01:01:00Z"),
        _row("C", "FOOTBALL", "2026-11-04T01:02:00Z"),
        _row("D", "NFL", "2026-11-04T01:03:00Z"),
    ]
    chosen = _pick_unique(rows)
    assert len(chosen) == 4
    assert not any("NCAAF" in r[0]["source_lane"] for r in chosen)


def test_unified_pool_can_have_all_ncaaf():
    rows = [
        _row("A", "NCAAF_3_TO_4_99_EXPERIMENTAL", "2026-11-04T01:00:00Z", 4),
        _row("B", "NCAAF_3_TO_4_99_EXPERIMENTAL", "2026-11-04T01:01:00Z", 3),
        _row("C", "NCAAF_3_TO_4_99_EXPERIMENTAL", "2026-11-04T01:02:00Z", 4),
        _row("D", "NCAAF_3_TO_4_99_EXPERIMENTAL", "2026-11-04T01:03:00Z", 3),
    ]
    chosen = _pick_unique(rows)
    assert len(chosen) == 4
    assert all("NCAAF" in r[0]["source_lane"] for r in chosen)


def test_unified_pool_flexible_mixed_and_unique_events():
    rows = [
        _row("A", "FOOTBALL", "2026-11-04T01:00:00Z"),
        _row("A", "NCAAF_3_TO_4_99_EXPERIMENTAL", "2026-11-04T01:00:01Z"),
        _row("B", "NCAAF_3_TO_4_99_EXPERIMENTAL", "2026-11-04T01:01:00Z", 4),
        _row("C", "EUROLEAGUE", "2026-11-04T01:02:00Z"),
        _row("D", "NCAAF_3_TO_4_99_EXPERIMENTAL", "2026-11-04T01:03:00Z", 4),
    ]
    chosen = _pick_unique(rows)
    assert len(chosen) == 4
    assert len({x[0]["event_id"] for x in chosen}) == 4
    assert sum("NCAAF" in x[0]["source_lane"] for x in chosen) == 2
