from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

from db import Database
from outcome_edge import odds_band
from proxy_xg import (
    FEATURES,
    ProxyXgEngine,
    extract_api_football_features,
    extract_statsbomb_features,
    fit_ridge,
    map_api_football_league,
    predict_proxy_xg,
)


def _db(tmp_path):
    db = Database("", str(tmp_path / "v017.sqlite"))
    db.init_schema()
    return db


def test_v017_schema_and_no_key_cycle_is_fail_closed(tmp_path):
    db = _db(tmp_path)
    settings = SimpleNamespace(
        proxy_xg_enabled=True,
        proxy_xg_api_football_key="",
        proxy_xg_statsbomb_matches_per_cycle=2,
        proxy_xg_min_training_samples=200,
        proxy_xg_refit_every_samples=40,
        proxy_xg_ridge_alpha=3.0,
        predictive_football_pred3_statsbomb_base_url="https://example.invalid",
    )
    result = ProxyXgEngine(db, settings).one_cycle()
    assert result["enabled"] is True
    assert result["api_football"] == "WAITING_FOR_API_KEY"
    for table in (
        "football_pxg_statsbomb_samples",
        "football_pxg_models",
        "football_pxg_api_manifest",
        "football_pxg_current_matches",
        "football_pxg_api_usage",
    ):
        assert db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")["n"] == 0


def test_statsbomb_feature_extraction_matches_proxy_feature_contract():
    events = [
        {"team": {"name": "Home"}, "type": {"name": "Shot"}, "location": [110, 40], "shot": {"outcome": {"name": "Goal"}}},
        {"team": {"name": "Home"}, "type": {"name": "Shot"}, "location": [95, 30], "shot": {"outcome": {"name": "Blocked"}}},
        {"team": {"name": "Home"}, "type": {"name": "Pass"}, "pass": {"type": {"name": "Corner"}}},
        {"team": {"name": "Away"}, "type": {"name": "Shot"}, "location": [108, 50], "shot": {"outcome": {"name": "Saved"}}},
        {"team": {"name": "Away"}, "type": {"name": "Bad Behaviour"}, "bad_behaviour": {"card": {"name": "Red Card"}}},
    ]
    got = extract_statsbomb_features(events, ("Home", "Away"))
    assert got["Home"]["total_shots"] == 2
    assert got["Home"]["shots_on_target"] == 1
    assert got["Home"]["blocked_shots"] == 1
    assert got["Home"]["shots_inside_box"] == 1
    assert got["Home"]["shots_outside_box"] == 1
    assert got["Home"]["corners"] == 1
    assert got["Away"]["shots_on_target"] == 1
    assert got["Away"]["red_cards"] == 1


def test_api_football_embedded_statistics_parser():
    item = {
        "teams": {"home": {"id": 1, "name": "Alpha"}, "away": {"id": 2, "name": "Beta"}},
        "statistics": [
            {"team": {"id": 1, "name": "Alpha"}, "statistics": [
                {"type": "Shots on Goal", "value": 6}, {"type": "Shots off Goal", "value": 4},
                {"type": "Total Shots", "value": 13}, {"type": "Blocked Shots", "value": 3},
                {"type": "Shots insidebox", "value": 9}, {"type": "Shots outsidebox", "value": 4},
                {"type": "Corner Kicks", "value": 7}, {"type": "Red Cards", "value": None},
            ]},
            {"team": {"id": 2, "name": "Beta"}, "statistics": [
                {"type": "Shots on Goal", "value": 2}, {"type": "Shots off Goal", "value": 5},
                {"type": "Total Shots", "value": 9}, {"type": "Blocked Shots", "value": 2},
                {"type": "Shots insidebox", "value": 5}, {"type": "Shots outsidebox", "value": 4},
                {"type": "Corner Kicks", "value": 3}, {"type": "Red Cards", "value": 1},
            ]},
        ],
    }
    got = extract_api_football_features(item)
    assert set(got["Alpha"]) == set(FEATURES)
    assert got["Alpha"]["red_cards"] == 0
    assert got["Beta"]["red_cards"] == 1
    assert got["Alpha"]["shots_inside_box"] == 9


def test_proxy_ridge_holdout_beats_constant_on_learnable_data():
    rows = []
    for i in range(240):
        shots = 5 + (i % 15)
        on = 1 + (i % 7)
        inside = 2 + (i % 10)
        row = {
            "id": i + 1,
            "played_at": f"2024-{1 + (i // 28) % 8:02d}-{1 + i % 28:02d}T12:00:00+00:00",
            "total_shots": shots,
            "shots_on_target": on,
            "shots_off_target": max(0, shots - on - 2),
            "blocked_shots": 2,
            "shots_inside_box": inside,
            "shots_outside_box": max(0, shots - inside),
            "corners": i % 8,
            "red_cards": 0,
        }
        row["target_xg"] = 0.08 * row["total_shots"] + 0.12 * row["shots_on_target"] + 0.05 * row["shots_inside_box"]
        rows.append(row)
    fit = fit_ridge(rows, alpha=3.0)
    assert fit["holdout_mae"] < fit["baseline_mae"]
    pred = predict_proxy_xg(rows[-1], fit["model"])
    assert abs(pred - rows[-1]["target_xg"]) < 0.5


def test_league_mapping_and_odds_bands_are_explicit():
    assert map_api_football_league("England", "Premier League") == "soccer_epl"
    assert map_api_football_league("Germany", "Bundesliga") == "soccer_germany_bundesliga"
    assert map_api_football_league("Austria", "Bundesliga") == "soccer_austria_bundesliga"
    assert map_api_football_league("Mars", "Premier League") is None
    assert odds_band(4.2) == "4.00-4.99"
    assert odds_band(7.49) == "5.00-7.49"
    assert odds_band(15.0) == "15.00+"


def test_pxg_discovery_retries_stale_plan_gate_after_upgrade(tmp_path):
    db = _db(tmp_path)
    settings = SimpleNamespace(proxy_xg_api_backfill_days=1)
    day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    db.execute(
        "INSERT INTO football_pxg_api_discovery_days(day,status,attempts,fixtures_seen,relevant_found,last_error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (day, "ERROR", 3, 0, 0, "API-Football errors: {'plan': 'Free plans do not have access to this date'}", old, old),
    )
    engine = ProxyXgEngine(db, settings)
    assert engine._next_discovery_day() == day


def test_pxg_discovery_does_not_hot_loop_recent_or_permanent_errors(tmp_path):
    db = _db(tmp_path)
    settings = SimpleNamespace(proxy_xg_api_backfill_days=1)
    day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    recent = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO football_pxg_api_discovery_days(day,status,attempts,fixtures_seen,relevant_found,last_error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (day, "ERROR", 3, 0, 0, "API-Football errors: {'plan': 'Free plans do not have access to this date'}", recent, recent),
    )
    engine = ProxyXgEngine(db, settings)
    assert engine._next_discovery_day() is None
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    db.execute(
        "UPDATE football_pxg_api_discovery_days SET attempts=?,last_error=?,updated_at=? WHERE day=?",
        (3, "API-Football fixture detail returned no rows", old, day),
    )
    assert engine._next_discovery_day() is None
