"""NCAAF 3.00–4.99 must enter the ordinary evolving HR2 pool.

No standalone NCAAF experiment, no historical reassembly and no HR1 change.
"""
from datetime import datetime, timedelta, timezone

import hit_rate_multiples_lab as hr
import hit_rate_lane_rotation_patch as rotation
import multiples_theoretical_queue_patch as theoretical
from unittest.mock import patch


def _selection(sport, odds, *, kind="MULTISPORT", market="h2h"):
    return {
        "source_odds": odds, "market_key": market, "sport_key": sport,
    }


def test_ncaaf_is_hr2_only_at_exact_band_boundaries():
    for p in (3.0, 3.75, 4.99, 4.999):
        row = _selection("americanfootball_ncaaf", p)
        qualified = hr._qualify("MULTISPORT", row)
        assert "HR2" in qualified
        assert "HR1" not in qualified
        assert qualified["HR2"] == {
            "lane": "NCAAF_H2H_3_00_4_99", "lo": 3.0, "hi": 5.0,
        }
    for p in (1.80, 2.99, 5.00, 6.0):
        qualified = hr._qualify(
            "MULTISPORT", _selection("americanfootball_ncaaf", p)
        )
        assert "NCAAF_H2H_3_00_4_99" not in [
            x["lane"] for x in qualified.values()
        ]


def test_nfl_existing_hr1_hr2_bands_unchanged():
    low = hr._qualify(
        "MULTISPORT", _selection("americanfootball_nfl", 2.50)
    )
    assert low["HR1"]["lane"] == "NFL_H2H_1_50_2_99"
    assert low["HR2"]["lane"] == "NFL_H2H_1_50_2_99"
    high = hr._qualify(
        "MULTISPORT", _selection("americanfootball_nfl", 3.50)
    )
    assert "HR2" not in high


def test_ncaaf_other_markets_not_accidentally_promoted():
    q = hr._qualify(
        "MULTISPORT", _selection("americanfootball_ncaaf", 3.5,
                                market="spreads")
    )
    assert not q


def test_hr2_pocket_visible_without_duplicate_policy_version():
    assert "NCAAF H2H 3.00-4.99" in hr.POLICY["HR2"]["pockets"]
    assert "NCAAF H2H 3.00-4.99" not in hr.POLICY["HR1"]["pockets"]
    assert hr.POLICY_VERSION == "HRM_2026-10-12_V1"


def test_existing_qualifying_multisport_source_enters_normal_hr2_queue():
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    class Db:
        def fetchall(self, sql):
            if "FROM multisport_execution_bets b JOIN multisport_events" in sql:
                return [{
                    "source_id": 1, "source_created_at": now.isoformat(),
                    "event_id": "game-123", "sport_key": "americanfootball_ncaaf",
                    "league": "NCAAF", "commence_time": (
                        now + timedelta(hours=2)).isoformat(),
                    "home_team": "Team A", "away_team": "Team B",
                    "market_key": "h2h", "selection": "Team A", "point": None,
                    "min_odds": 3.1, "source_odds": 3.8,
                }]
            return []
    source = hr._source_rows(Db(), now)
    assert len(source) == 1
    assert source[0]["experiment_key"] == "HR2"
    assert source[0]["source_lane"] == "NCAAF_H2H_3_00_4_99"


def test_runtime_rotated_hr2_policy_lists_ncaaf():
    # Production applies this wrapper AFTER the base HR2 policy module;
    # it must not silently overwrite the promoted NCAAF pocket.
    pockets = rotation._policy()
    assert "NCAAF H2H 3.00-4.99" in pockets["HR2"]["pockets"]
    assert "NCAAF H2H 3.00-4.99" not in pockets["HR1"]["pockets"]


def test_ncaaf_preprice_eval_is_a_normal_hr2_candidate():
    class Db:
        def fetchall(self, *args, **kwargs):
            return []
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    events = [
        {
            "source_id": 123, "source_created_at": now.isoformat(),
            "event_id": "ncaaf-a", "sport_key": "americanfootball_ncaaf",
            "market_key": "h2h", "selection": "Team A",
            "min_required_odds": 3.6,
            "commence_time": (now + timedelta(hours=44)).isoformat(),
        },
        {
            "source_id": 124, "source_created_at": now.isoformat(),
            "event_id": "ncaaf-b", "sport_key": "americanfootball_ncaaf",
            "market_key": "h2h", "selection": "Team B",
            "min_required_odds": 5.1,
            "commence_time": (now + timedelta(hours=44)).isoformat(),
        },
    ]
    with patch.object(theoretical, "_multisport_eval_rows", return_value=events):
        rows = theoretical._hr_theoretical_rows(Db(), now)
    assert len(rows) == 1
    row = rows[0]
    assert row["experiment_key"] == "HR2"
    assert row["source_lane"] == "NCAAF_H2H_3_00_4_99"
    assert row["source_table"] == "THEORETICAL_MULTISPORT_H2H"
    assert row["source_odds"] == 3.6
    assert row["min_odds"] == 3.6
    assert row["band_min"] == 3.0 and row["band_max"] == 5.0


def test_ncaaf_preprice_uses_offered_quote_only_at_formation():
    # Pre-price inclusion is NOT a claim that a 3.60 manual price exists.
    # It only seeds the normal monitored HR2 queue, and the usual minimum,
    # band, quote-freshness, bookmaker and kickoff filters remain active.
    assert theoretical._target_price(4.99, 3.0, 5.0) == 4.99
    assert theoretical._target_price(5.0, 3.0, 5.0) is None
    assert theoretical._target_price(2.25, 3.0, 5.0) == 3.0
