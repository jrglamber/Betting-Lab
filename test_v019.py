from dataclasses import replace
from datetime import datetime, timedelta, timezone

from config import Settings
from db import Database
from outcome_edge import outcome_edge_report, ensure_watch_cohorts, _source_rows
from predictive_football_pred4 import PredictiveFootballPred4Engine


def _db(tmp_path):
    db = Database("", str(tmp_path / "v019.sqlite"))
    db.init_schema()
    return db


def _seed_pxg_current(db, kickoff):
    teams = [
        ("Alpha", "Gamma", 2.10, 0.75),
        ("Gamma", "Alpha", 0.85, 1.85),
        ("Beta", "Delta", 0.80, 1.60),
        ("Delta", "Beta", 1.55, 0.85),
    ]
    for i in range(28):
        home, away, hxg, axg = teams[i % len(teams)]
        played = (kickoff - timedelta(days=2 + i * 1.5)).isoformat()
        db.execute(
            """INSERT INTO football_pxg_current_matches(
                fixture_id,sport_key,league_name,country,played_at,home_team,away_team,
                home_goals,away_goals,home_features_json,away_features_json,
                home_proxy_xg,away_proxy_xg,pxg_model_id,source,imported_at,scored_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"pxg-{i}", "soccer_epl", "Premier League", "England", played,
             home, away, 1, 1, "{}", "{}", hxg, axg, None, "API-FOOTBALL", played, played),
        )


def test_v019_pred4_defaults_and_schema(tmp_path):
    db = _db(tmp_path)
    s = Settings()
    assert s.predictive_football_pred4_enabled is True
    assert s.predictive_football_pred4_min_team_matches == 5
    assert s.predictive_football_pred4_half_life_days == 21.0
    assert db.fetchone("SELECT COUNT(*) AS n FROM football_predictive4_predictions")["n"] == 0
    assert db.fetchone("SELECT COUNT(*) AS n FROM outcome_edge_watch_cohorts")["n"] == 0


def test_v019_pred4_uses_current_pxg_and_freezes_markets(tmp_path):
    db = _db(tmp_path)
    kickoff = datetime(2030, 9, 1, 18, 0, tzinfo=timezone.utc)
    _seed_pxg_current(db, kickoff)
    settings = replace(Settings(), predictive_football_pred4_lookback_days=60)
    engine = PredictiveFootballPred4Engine(db, settings, execution_bookmaker_keys=("matchbook",))
    fit, reason = engine.fit_event({
        "event_id": "p4-e1", "sport_key": "soccer_epl", "league": "EPL",
        "commence_time": kickoff.isoformat(), "home_team": "Alpha", "away_team": "Beta",
    }, now=kickoff - timedelta(hours=23))
    assert reason == "OK"
    assert fit is not None
    assert fit["expected_home_goals"] > fit["expected_away_goals"]
    assert fit["pxg_data_age_days"] < 10

    db.execute(
        """INSERT INTO events(event_id,sport_key,league,commence_time,home_team,away_team,first_seen_at,last_seen_at,status)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        ("p4-e1", "soccer_epl", "EPL", kickoff.isoformat(), "Alpha", "Beta",
         (kickoff - timedelta(days=1)).isoformat(), (kickoff - timedelta(days=1)).isoformat(), "UPCOMING"),
    )
    out = engine.freeze_due_predictions(kickoff - timedelta(hours=23))
    assert out["created"] == 1
    pred = db.fetchone("SELECT * FROM football_predictive4_predictions WHERE event_id='p4-e1'")
    assert pred["model_version"] == "PRED4_PROXY_XG_POISSON_V1"
    assert pred["pxg_data_age_days"] is not None
    assert engine.ensure_market_predictions() == 8


def _seed_pred1_btts(db):
    kickoff = "2030-09-01T18:00:00+00:00"
    db.execute(
        """INSERT INTO events(event_id,sport_key,league,commence_time,home_team,away_team,first_seen_at,last_seen_at,status)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        ("btts-e1", "soccer_epl", "EPL", kickoff, "Alpha", "Beta", kickoff, kickoff, "FINAL"),
    )
    db.execute(
        """INSERT INTO football_predictive_predictions(
            event_id,created_at,sport_key,league,commence_time,home_team,away_team,
            mapped_home_team,mapped_away_team,home_mapping_score,away_mapping_score,
            expected_home_goals,expected_away_goals,home_probability,draw_probability,away_probability,
            home_fair_odds,draw_fair_odds,away_fair_odds,league_training_matches,
            home_effective_matches,away_effective_matches,model_version,config_hash
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("btts-e1", kickoff, "soccer_epl", "EPL", kickoff, "Alpha", "Beta", "Alpha", "Beta",
         1.0, 1.0, 1.5, 1.2, 0.45, 0.28, 0.27, 1/0.45, 1/0.28, 1/0.27, 100, 10, 10, "P1", "cfg"),
    )
    pid = db.fetchone("SELECT id FROM football_predictive_predictions WHERE event_id='btts-e1'")["id"]
    db.execute(
        """INSERT INTO football_predictive_market_predictions(
            prediction_id,event_id,created_at,market_key,selection,point,line_key,probability,fair_odds,actual_hit,brier_score,settled_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pid, "btts-e1", kickoff, "btts", "Yes", None, "", 0.55, 1/0.55, 1, 0.20, kickoff),
    )
    mpid = db.fetchone("SELECT id FROM football_predictive_market_predictions WHERE prediction_id=?", (pid,))["id"]
    db.execute(
        """INSERT INTO football_predictive_market_bets(
            predictive_key,market_prediction_id,prediction_id,event_id,created_at,market_key,selection,point,line_key,
            bookmaker_key,bookmaker_title,offered_odds,model_probability,model_fair_odds,edge_pct,min_odds,strong_candidate,
            status,closing_odds,clv_pct,closing_observed_at,closing_minutes_before_kickoff,clv_quality,result,pnl_units,
            commission_rate_pct,commission_units,net_pnl_units,settled_at,app_version,experiment_version,config_hash
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("btts-key", mpid, pid, "btts-e1", "2026-01-01T00:00:00+00:00", "btts", "Yes", None, "", "matchbook", "Matchbook", 2.25,
         0.55, 1/0.55, 23.75, 1.87, 1, "SETTLED", 2.10, 7.14, kickoff, 5.0, "A", "WIN", 1.25,
         2.0, 0.025, 1.225, kickoff, "0.19.0", "PRED1_TEST", "cfg"),
    )


def test_v019_outcome_edge_includes_pred_derived_markets_and_freezes_watch(tmp_path):
    db = _db(tmp_path)
    _seed_pred1_btts(db)
    rows = _source_rows(db)
    assert any(r["source"] == "PRED1" and r["market_key"] == "btts" for r in rows)
    ensure_watch_cohorts(db)
    report = outcome_edge_report(db)
    watches = {x["cohort_key"]: x for x in report["frozen_watch_cohorts"]}
    assert "PRED12_BTTS" in watches
    # The pre-existing BTTS result is discovery evidence, not forward validation.
    assert watches["PRED12_BTTS"]["discovery_sample"]["selections"] == 1
    assert watches["PRED12_BTTS"]["forward_sample"]["selections"] == 0
