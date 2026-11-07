"""Weekend Hit Rate quote budget: no calls to bookmaker or sports providers."""
from datetime import datetime, timezone

import pytest

import hit_rate_jit_priority_patch as weekend
import hit_rate_multiples_lab as hr
import hit_rate_daily_boost_patch as daily_boost
import credit_policy_unlimited_patch as base
import multiples_credit_efficiency_patch as efficiency
import multiples_today_headroom_patch as legacy
import multiples_budget_balance_patch as balance
import provider_cost_control_patch as provider


def utc(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


@pytest.mark.parametrize("stamp,base_cap,hard_cap", [
    ("2026-11-05T23:59:00Z", 2500, 3000),  # Thursday
    ("2026-11-06T00:00:00Z", 5000, 5000),  # Friday
    ("2026-11-07T10:55:00Z", 5000, 5000),  # Saturday
    ("2026-11-08T23:59:00Z", 5000, 5000),  # Sunday
    ("2026-11-09T00:00:00Z", 2500, 3000),  # Monday
    ("2027-03-26T00:00:00Z", 5000, 5000),  # Friday
    ("2027-03-29T00:00:00Z", 2500, 3000),  # Monday
])
def test_utc_day_caps(stamp, base_cap, hard_cap):
    now = utc(stamp)
    assert weekend._effective_base_cap(now) == base_cap
    assert weekend._burst_hard_cap(now) == hard_cap
    assert weekend._weekend_utc(now) is (hard_cap == 5000)


def test_legacy_caps_switch_both_directions_at_midnight(monkeypatch):
    attrs = [
        (hr, "DAILY_REFRESH_CREDIT_BUDGET"),
        (base, "HIT_RATE_DAILY_CREDITS"),
        (efficiency, "HIT_RATE_DAILY_CREDITS"),
        (daily_boost, "NORMAL_DAILY_CAP"),
        (daily_boost, "BOOST_DAILY_CAP"),
        (daily_boost, "_effective_cap"),
        (legacy, "NORMAL_HIT_RATE_CAP"),
        (legacy, "BOOST_HIT_RATE_CAP"),
        (balance, "HIT_RATE_DAILY_CREDITS"),
        (provider, "HR_DAILY_CREDIT_CEILING"),
    ]
    for module, name in attrs:
        monkeypatch.setattr(module, name, getattr(module, name))

    for stamp, expected in [
        ("2026-11-06T23:59:00Z", 5000),
        ("2026-11-07T00:00:00Z", 5000),
        ("2026-11-08T23:59:00Z", 5000),
        ("2026-11-09T00:00:00Z", 3000),
        ("2026-11-13T00:00:00Z", 5000),
    ]:
        assert weekend._sync_runtime_caps(utc(stamp)) == expected
        for module, name in attrs:
            if name != "_effective_cap":
                assert getattr(module, name) == expected
        assert daily_boost._effective_cap(utc(stamp)) == expected


def test_normal_weekday_protected_reserve_remains_500():
    assert weekend.PROTECTED_RESERVE_CREDITS == 500
    assert weekend.BURST_DAILY_CREDITS == 3000
    assert weekend.BASE_DAILY_CREDITS == 2500
    assert weekend.WEEKEND_DAILY_CREDITS == 5000
    assert weekend.WEEKEND_DAYS_UTC == {4, 5, 6}
