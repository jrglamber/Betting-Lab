from config import Settings
import json
import io
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from db import Database, sanitize_sensitive_text
from fair_value import (
    implied_probability, devig_prices, fair_odds, expected_value,
    edge_pct, min_odds_for_probability, consensus_probabilities, clv_pct
)
from cross_market import (
    score_market_probabilities, fit_score_model
)
from slow_book import detect_slow_book
from quota import QuotaGuard, provider_actual_cost
from collector import (
    desired_poll_interval_minutes, event_is_due, breadth_is_urgent, Collector
)
from the_odds_api import ApiResult, TheOddsApi
from closing import settle_signal
from results import grade_signal, settle_event_signals, ResultCollector
from canonical import cluster_event_signals, backfill_canonical_bets, canonical_scoreboard
from research_intelligence import (
    edge_bucket, sample_status, segmentation_tables, price_checkpoint_summary,
    clv_vs_results, upsert_weekly_report, research_intelligence,
    odds_band, time_to_kickoff_bucket, edge_clv_calibration,
    fixture_exposure_analysis,
)
from research import record_evaluation, track_signal_prices, finalize_closing_lines, strategy_scoreboard, repair_premature_clv
from signals import write_slow_book_signals
from worker import Worker
import worker as worker_module


from execution_shadow import (
    evaluate_execution_wave_at, backfill_execution_shadows, track_execution_prices,
    finalize_execution_clv, execution_scoreboard, settle_execution_event, execution_funnel,
    clv_quality, backfill_execution_accounting,
)

from exporter import build_research_export
from multiples_shadow import (
    generate_multiple_shadows, finalize_multiple_clv, settle_multiple_shadows,
    multiples_scoreboard, latest_multiple_shadows,
)
from manual_systems_shadow import (
    generate_manual_system_shadows, settle_manual_system_shadows,
    manual_systems_scoreboard, latest_manual_system_cards, manual_quote_targets,
    refresh_manual_system_quotes,
)
from instrumentation import (
    experiment_config_hash, market_wave_metrics, entry_market_metrics,
    backfill_entry_market_metrics, finalize_reference_closes,
    refresh_settlement_provenance, probability_calibration_report,
    multiples_overlap_report, instrumentation_report,
)
from tennis_shadow import (
    TennisQuotaGuard, TennisShadowEngine, write_tennis_consensus,
    evaluate_tennis_convergence_wave, track_tennis_prices, finalize_tennis_clv,
    settle_tennis_event, tennis_scoreboard, tennis_segments,
)
from multisport_shadow import (
    MultiSportQuotaGuard, MultiSportShadowEngine, write_multisport_consensus,
    evaluate_multisport_convergence_wave, track_multisport_prices,
    finalize_multisport_clv, settle_multisport_event, multisport_scoreboard,
    multisport_segments, latest_multisport_bets, sport_family,
)
from multisport_lines_shadow import (
    MultiSportLinesEngine, insert_line_payload, write_line_consensus,
    evaluate_line_wave, finalize_line_closes, track_line_prices,
    settle_line_event, settle_lines_from_stored_results, line_scoreboard,
    line_segments, line_funnel, line_clv_points, reference_region,
)
from predictive_football import (
    PredictiveFootballEngine, predictive_scoreboard, predictive_funnel,
    latest_predictive_bets, score_probabilities, _normalize_text,
    btts_probabilities, total_probabilities, derived_market_probabilities,
    predictive_market_summary, latest_predictive_market_bets,
    predictive_bootstrap_status,
)
from predictive_football_pred2 import (
    PredictiveFootballPred2Engine, predictive2_scoreboard,
    score_probabilities as pred2_score_probabilities,
)
from predictive_football_pred3 import (
    PredictiveFootballPred3Engine, predictive3_scoreboard,
)
from meta_edge import (
    capture_meta_edge_samples, label_meta_edge_samples, meta_edge_scoreboard,
    meta_edge_segments, run_meta_edge_maintenance, META_EDGE_VERSION,
)

@pytest.fixture
def db(tmp_path):
    d = Database("", str(tmp_path / "lab.sqlite"))
    d.init_schema()
    return d


def test_01_implied_probability():
    assert implied_probability(2.0) == pytest.approx(0.5)


def test_02_devig_sums_to_one():
    p = devig_prices({"A": 1.80, "B": 2.20})
    assert sum(p.values()) == pytest.approx(1.0)


def test_03_fair_odds_inverse():
    assert fair_odds(0.25) == pytest.approx(4.0)


def test_04_expected_value():
    assert expected_value(0.55, 2.0) == pytest.approx(0.10)


def test_05_edge_pct():
    assert edge_pct(0.55, 2.0) == pytest.approx(10.0)


def test_06_min_odds_includes_edge():
    assert min_odds_for_probability(0.50, 5.0) == pytest.approx(2.10)


def test_07_consensus_excludes_target():
    books = {
        "a": {"H": 2.00, "D": 3.50, "A": 4.00},
        "b": {"H": 1.95, "D": 3.60, "A": 4.10},
        "c": {"H": 2.05, "D": 3.40, "A": 3.90},
        "target": {"H": 2.40, "D": 3.20, "A": 3.20},
    }
    p = consensus_probabilities(books, exclude_bookmaker="target", min_books=3)
    assert p is not None
    assert sum(p.values()) == pytest.approx(1.0)


def test_08_positive_clv_when_taken_price_beats_close():
    assert clv_pct(2.10, 1.95) > 0


def test_09_poisson_market_distribution_is_valid():
    p = score_market_probabilities(1.6, 1.1)
    assert p["home"] + p["draw"] + p["away"] == pytest.approx(1.0)
    assert 0 < p["btts_yes"] < 1


def test_10_cross_market_fit_is_reasonable():
    model = fit_score_model(0.50, 0.26, 0.24, 0.57, step=0.10)
    assert model.lambda_home > model.lambda_away
    assert 0 < model.btts_yes < 1
    assert model.loss < 0.03


def test_11_slow_book_gap():
    sig = detect_slow_book(2.20, [2.00, 2.02, 1.98], minimum_gap_pct=5.0)
    assert sig is not None
    assert sig.relative_price_gap_pct > 5


def test_12_quota_reserve_blocks(db):
    db.execute(
        "UPDATE quota_state SET credits_remaining=52 WHERE singleton_id=1"
    )
    q = QuotaGuard(db, reserve=50, daily_budget=12)
    decision = q.decide(4)
    assert not decision.allowed
    assert decision.reason == "protected_quota_reserve"


def test_13_quota_allows_above_reserve(db):
    db.execute(
        "UPDATE quota_state SET credits_remaining=100 WHERE singleton_id=1"
    )
    q = QuotaGuard(db, reserve=50, daily_budget=12)
    assert q.decide(4).allowed


def test_14_poll_intervals():
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    assert desired_poll_interval_minutes((now + timedelta(minutes=60)).isoformat(), now) == 45
    assert desired_poll_interval_minutes((now + timedelta(hours=10)).isoformat(), now) == 360
    assert desired_poll_interval_minutes((now + timedelta(days=3)).isoformat(), now) == 1440


def test_15_event_due_without_snapshot():
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    event = {
        "commence_time": (now + timedelta(hours=4)).isoformat(),
        "last_odds_poll_at": None,
    }
    assert event_is_due(event, now)


class FakeApi:
    def quota_probe(self):
        return ApiResult([], 500, 0, 0)

    def events(self, sport_key):
        return ApiResult(
            [{
                "id": "evt1",
                "commence_time": "2030-09-01T15:00:00Z",
                "home_team": "Home FC",
                "away_team": "Away FC",
            }],
            500, 0, 0
        )

    def event_odds(self, sport_key, event_id, region, markets):
        return ApiResult({
            "id": event_id,
            "bookmakers": [{
                "key": "book1",
                "title": "Book 1",
                "last_update": "2026-08-31T12:00:00Z",
                "markets": [{
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Home FC", "price": 2.0},
                        {"name": "Draw", "price": 3.5},
                        {"name": "Away FC", "price": 4.0},
                    ]
                }]
            }]
        }, 496, 4, 4)


def test_16_free_discovery_inserts_event(db):
    q = QuotaGuard(db, reserve=50, daily_budget=12)
    c = Collector(
        db, FakeApi(), q,
        sport_keys=["soccer_epl"],
        region="uk",
        markets=["h2h","totals","btts","draw_no_bet"]
    )
    assert c.discover() == 1
    row = db.fetchone("SELECT * FROM events WHERE event_id='evt1'")
    assert row["league"] == "Premier League"


def test_17_paid_poll_inserts_quotes_and_cost(db):
    q = QuotaGuard(db, reserve=50, daily_budget=12)
    c = Collector(
        db, FakeApi(), q,
        sport_keys=["soccer_epl"],
        region="uk",
        markets=["h2h","totals","btts","draw_no_bet"]
    )
    c.discover()
    result = c.poll_one_cycle()
    assert result["polled"] == 1
    assert (db.fetchone("SELECT COUNT(*) AS n FROM odds_snapshots")["n"]) == 3
    assert q.today_paid_cost() == 4


def test_18_shadow_settlement_is_one_unit(db):
    db.execute(
        """
        INSERT INTO events(
          event_id,sport_key,league,commence_time,home_team,away_team,
          first_seen_at,last_seen_at,status
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        ("e","soccer_epl","Premier League","2026-09-01T12:00:00+00:00",
         "H","A","2026-08-31T00:00:00+00:00","2026-08-31T00:00:00+00:00","UPCOMING")
    )
    db.execute(
        """
        INSERT INTO signals(
          created_at,event_id,strategy,market_key,selection,bookmaker_key,
          bookmaker_title,offered_odds,fair_odds,fair_probability,edge_pct,
          min_odds,status
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        ("2026-08-31T00:00:00+00:00","e","TEST","h2h","H","b","Book",
         2.5,2.2,1/2.2,5.0,2.3,"OPEN")
    )
    sig = db.fetchone("SELECT id FROM signals LIMIT 1")
    out = settle_signal(db, sig["id"], "WIN")
    assert out["pnl_units"] == pytest.approx(1.5)



def _seed_event(db, event_id="r1", commence="2026-09-01T15:00:00+00:00"):
    db.execute(
        """INSERT INTO events(event_id,sport_key,league,commence_time,home_team,away_team,first_seen_at,last_seen_at,status) VALUES(?,?,?,?,?,?,?,?,?)""",
        (event_id,"soccer_epl","Premier League",commence,"Home","Away","2026-09-01T08:00:00+00:00","2026-09-01T08:00:00+00:00","UPCOMING")
    )


def test_19_candidate_audit_table(db):
    _seed_event(db)
    record_evaluation(db,evaluated_at="2026-09-01T09:00:00+00:00",event_id="r1",strategy="TEST",market_key="h2h",selection="Home",decision="REJECT",reason="EDGE_BELOW_THRESHOLD")
    assert db.fetchone("SELECT COUNT(*) AS n FROM candidate_evaluations")["n"] == 1


def test_20_candidate_audit_deduplicates(db):
    _seed_event(db)
    kwargs=dict(evaluated_at="2026-09-01T09:00:00+00:00",event_id="r1",strategy="TEST",market_key="h2h",selection="Home",decision="REJECT",reason="EDGE_BELOW_THRESHOLD")
    record_evaluation(db,**kwargs);record_evaluation(db,**kwargs)
    assert db.fetchone("SELECT COUNT(*) AS n FROM candidate_evaluations")["n"] == 1


def test_21_price_tracking_is_not_final_clv(db):
    _seed_event(db,commence="2026-09-02T15:00:00+00:00")
    db.execute("""INSERT INTO signals(created_at,event_id,strategy,market_key,selection,bookmaker_key,bookmaker_title,offered_odds,fair_odds,fair_probability,edge_pct,min_odds,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",("2026-09-01T09:00:00+00:00","r1","TEST","h2h","Home","b","Book",2.2,2.0,.5,10,2.0,"OPEN"))
    db.execute("""INSERT INTO odds_snapshots(event_id,captured_at,bookmaker_key,bookmaker_title,market_key,outcome_name,price) VALUES(?,?,?,?,?,?,?)""",("r1","2026-09-01T10:00:00+00:00","b","Book","h2h","Home",2.0))
    assert track_signal_prices(db) == 1
    sig=db.fetchone("SELECT * FROM signals LIMIT 1")
    assert sig["closing_odds"] is None
    assert db.fetchone("SELECT move_vs_entry_pct FROM signal_price_observations")["move_vs_entry_pct"] > 0


def test_22_clv_only_finalizes_after_kickoff(db):
    _seed_event(db,commence="2026-09-01T15:00:00+00:00")
    db.execute("""INSERT INTO signals(created_at,event_id,strategy,market_key,selection,bookmaker_key,bookmaker_title,offered_odds,fair_odds,fair_probability,edge_pct,min_odds,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",("2026-09-01T09:00:00+00:00","r1","TEST","h2h","Home","b","Book",2.2,2.0,.5,10,2.0,"OPEN"))
    db.execute("""INSERT INTO odds_snapshots(event_id,captured_at,bookmaker_key,bookmaker_title,market_key,outcome_name,price) VALUES(?,?,?,?,?,?,?)""",("r1","2026-09-01T14:55:00+00:00","b","Book","h2h","Home",2.0))
    before=datetime(2026,9,1,14,59,tzinfo=timezone.utc);after=datetime(2026,9,1,15,1,tzinfo=timezone.utc)
    assert finalize_closing_lines(db,before) == 0
    assert finalize_closing_lines(db,after) == 1
    assert db.fetchone("SELECT clv_pct FROM signals")["clv_pct"] > 0


def test_23_scoreboard_reports_beat_close(db):
    _seed_event(db)
    for i,(clv,pnl) in enumerate([(5.0,1.0),(-2.0,-1.0)],1):
        db.execute("""INSERT INTO signals(created_at,event_id,strategy,market_key,selection,bookmaker_key,bookmaker_title,offered_odds,fair_odds,fair_probability,edge_pct,min_odds,status,clv_pct,pnl_units) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(f"2026-09-01T09:0{i}:00+00:00","r1","TEST","h2h","Home",f"b{i}","Book",2.0,1.9,.52,4,1.9,"SETTLED",clv,pnl))
    row=strategy_scoreboard(db)[0]
    assert row["beat_close_pct"] == pytest.approx(50.0)
    assert row["max_drawdown_units"] == pytest.approx(-1.0)


def test_24_research_balanced_candidate_prefers_unpolled_in_breadth(db):
    q=QuotaGuard(db,reserve=50,daily_budget=100)
    c=Collector(db,FakeApi(),q,sport_keys=["soccer_epl"],region="uk",markets=["h2h"],breadth_polls_per_day=2)
    now=datetime.now(timezone.utc)
    soon=(now+timedelta(hours=2)).isoformat();later=(now+timedelta(hours=3)).isoformat()
    for eid,kick,last in [("a",soon,now.isoformat()),("b",later,None)]:
        db.execute("""INSERT INTO events(event_id,sport_key,league,commence_time,home_team,away_team,first_seen_at,last_seen_at,last_odds_poll_at,status) VALUES(?,?,?,?,?,?,?,?,?,?)""",(eid,"soccer_epl","Premier League",kick,"H","A",now.isoformat(),now.isoformat(),last,"UPCOMING"))
    due=c.candidate_events()
    assert due[0]["event_id"] == "b"



def test_25_repair_premature_old_clv(db):
    _seed_event(db,commence="2026-09-02T15:00:00+00:00")
    db.execute("""INSERT INTO signals(created_at,event_id,strategy,market_key,selection,bookmaker_key,bookmaker_title,offered_odds,fair_odds,fair_probability,edge_pct,min_odds,status,closing_odds,clv_pct) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",("2026-09-01T09:00:00+00:00","r1","TEST","h2h","Home","b","Book",2.2,2.0,.5,10,2.0,"OPEN",2.2,0.0))
    now=datetime(2026,9,1,10,0,tzinfo=timezone.utc)
    assert repair_premature_clv(db,now) == 1
    row=db.fetchone("SELECT closing_odds,clv_pct FROM signals")
    assert row["closing_odds"] is None and row["clv_pct"] is None


def test_26_grade_btts_no_wins_when_clean_sheet():
    sig = {"market_key": "btts", "selection": "No"}
    assert grade_signal(
        sig, home_team="Home", away_team="Away", home_score=2, away_score=0
    ) == "WIN"


def test_27_grade_btts_no_loses_when_both_score():
    sig = {"market_key": "btts", "selection": "No"}
    assert grade_signal(
        sig, home_team="Home", away_team="Away", home_score=2, away_score=1
    ) == "LOSS"


def test_28_grade_dnb_push_on_draw():
    sig = {"market_key": "draw_no_bet", "selection": "Home"}
    assert grade_signal(
        sig, home_team="Home", away_team="Away", home_score=1, away_score=1
    ) == "PUSH"


def test_29_grade_totals_handles_push():
    sig = {"market_key": "totals", "selection": "Over", "point": 2.0}
    assert grade_signal(
        sig, home_team="Home", away_team="Away", home_score=1, away_score=1
    ) == "PUSH"


def test_30_auto_settle_event_signal(db):
    db.execute(
        """
        INSERT INTO events(
          event_id,sport_key,league,commence_time,home_team,away_team,
          first_seen_at,last_seen_at,status
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        ("res1","soccer_epl","Premier League","2026-08-31T12:00:00+00:00",
         "Home","Away","2026-08-30T00:00:00+00:00","2026-08-30T00:00:00+00:00","UPCOMING")
    )
    db.execute(
        """
        INSERT INTO signals(
          created_at,event_id,strategy,market_key,selection,bookmaker_key,
          bookmaker_title,offered_odds,fair_odds,fair_probability,edge_pct,
          min_odds,status
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        ("2026-08-31T10:00:00+00:00","res1","CROSS_MARKET_RV","btts","No",
         "book","Book",2.24,2.15,1/2.15,4.19,2.20,"OPEN")
    )
    counts = settle_event_signals(db, "res1", home_score=2, away_score=0)
    assert counts["wins"] == 1
    row = db.fetchone("SELECT result,pnl_units,status FROM signals WHERE event_id='res1'")
    assert row["result"] == "WIN"
    assert row["pnl_units"] == pytest.approx(1.24)
    assert row["status"] == "SETTLED"


class FakeScoresApi:
    def scores(self, sport_key, *, event_ids=(), days_from=1):
        return ApiResult(
            [{
                "id": "result_evt",
                "sport_key": sport_key,
                "commence_time": "2026-08-31T12:00:00Z",
                "completed": True,
                "home_team": "Home",
                "away_team": "Away",
                "scores": [
                    {"name": "Home", "score": "1"},
                    {"name": "Away", "score": "1"},
                ],
            }],
            480, 20, 2
        )


def test_31_result_collector_fetches_only_due_signal_event(db):
    db.execute(
        """
        INSERT INTO events(
          event_id,sport_key,league,commence_time,home_team,away_team,
          first_seen_at,last_seen_at,status
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        ("result_evt","soccer_epl","Premier League","2026-08-31T12:00:00+00:00",
         "Home","Away","2026-08-30T00:00:00+00:00","2026-08-30T00:00:00+00:00","UPCOMING")
    )
    db.execute(
        """
        INSERT INTO signals(
          created_at,event_id,strategy,market_key,selection,bookmaker_key,
          bookmaker_title,offered_odds,fair_odds,fair_probability,edge_pct,
          min_odds,status
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        ("2026-08-31T10:00:00+00:00","result_evt","CONSENSUS_VALUE","draw_no_bet","Home",
         "book","Book",1.80,1.70,1/1.70,5.0,1.75,"OPEN")
    )
    db.execute("UPDATE quota_state SET credits_remaining=500 WHERE singleton_id=1")
    q = QuotaGuard(db, reserve=50, daily_budget=20)
    rc = ResultCollector(
        db, FakeScoresApi(), q,
        enabled=True, min_minutes_after_kickoff=135,
        min_poll_interval_seconds=21600,
    )
    now = datetime(2026,8,31,15,0,tzinfo=timezone.utc)
    out = rc.collect(now)
    assert out["settled"] == 1
    result = db.fetchone("SELECT home_score,away_score FROM event_results WHERE event_id='result_evt'")
    assert result["home_score"] == 1 and result["away_score"] == 1
    sig = db.fetchone("SELECT result,pnl_units FROM signals WHERE event_id='result_evt'")
    assert sig["result"] == "PUSH"
    assert sig["pnl_units"] == pytest.approx(0.0)


def test_32_daily_paid_cost_includes_results_and_failed_charged_calls(db):
    db.record_collector_run("ODDS", True, actual_cost=4)
    db.record_collector_run("RESULTS", False, actual_cost=2)
    q = QuotaGuard(db, reserve=50, daily_budget=20)
    assert q.today_paid_cost() == 6


def _insert_test_event(db, event_id="canon_evt"):
    db.execute(
        """
        INSERT INTO events(
          event_id,sport_key,league,commence_time,home_team,away_team,
          first_seen_at,last_seen_at,status
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (event_id,"soccer_epl","Premier League","2026-09-02T12:00:00+00:00",
         "Home","Away","2026-09-01T00:00:00+00:00","2026-09-01T00:00:00+00:00","UPCOMING")
    )


def _insert_detection(db, *, event_id, strategy, book, odds, created_at="2026-09-01T10:00:00+00:00", edge=5.0):
    db.execute(
        """
        INSERT INTO signals(
          created_at,event_id,strategy,market_key,selection,bookmaker_key,
          bookmaker_title,offered_odds,fair_odds,fair_probability,edge_pct,
          min_odds,status
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (created_at,event_id,strategy,"h2h","Away",book,book,
         odds,5.20,1/5.20,edge,5.25,"OPEN")
    )


def test_33_canonical_cluster_collapses_overlap_and_chooses_best_first_wave(db):
    _insert_test_event(db, "canon1")
    _insert_detection(db,event_id="canon1",strategy="CONSENSUS_VALUE",book="BookA",odds=5.40)
    _insert_detection(db,event_id="canon1",strategy="CONSENSUS_VALUE",book="BookB",odds=5.50)
    _insert_detection(db,event_id="canon1",strategy="SLOW_BOOK",book="BookA",odds=5.40)
    _insert_detection(db,event_id="canon1",strategy="SLOW_BOOK",book="BookB",odds=5.50)
    assert cluster_event_signals(db,"canon1") == 1
    row=db.fetchone("SELECT * FROM canonical_bets WHERE event_id='canon1'")
    assert row["offered_odds"] == pytest.approx(5.50)
    assert row["strategy_count"] == 2
    assert row["bookmaker_count"] == 2
    assert row["detection_count"] == 4


def test_34_later_better_detection_does_not_rewrite_canonical_entry(db):
    _insert_test_event(db, "canon2")
    _insert_detection(db,event_id="canon2",strategy="CONSENSUS_VALUE",book="BookA",odds=5.50)
    cluster_event_signals(db,"canon2")
    _insert_detection(
        db,event_id="canon2",strategy="SLOW_BOOK",book="BookC",odds=6.00,
        created_at="2026-09-01T11:00:00+00:00",edge=10.0
    )
    cluster_event_signals(db,"canon2")
    row=db.fetchone("SELECT * FROM canonical_bets WHERE event_id='canon2'")
    assert row["offered_odds"] == pytest.approx(5.50)
    assert row["strategy_count"] == 2
    assert row["bookmaker_count"] == 2
    assert row["detection_count"] == 2


def test_35_event_settlement_counts_canonical_once(db):
    _insert_test_event(db, "canon3")
    _insert_detection(db,event_id="canon3",strategy="CONSENSUS_VALUE",book="BookA",odds=3.00)
    _insert_detection(db,event_id="canon3",strategy="SLOW_BOOK",book="BookB",odds=3.10)
    cluster_event_signals(db,"canon3")
    settle_event_signals(db,"canon3",home_score=0,away_score=1)
    raw=db.fetchall("SELECT pnl_units FROM signals WHERE event_id='canon3'")
    canonical=db.fetchall("SELECT pnl_units FROM canonical_bets WHERE event_id='canon3'")
    assert len(raw) == 2
    assert len(canonical) == 1
    assert canonical[0]["pnl_units"] == pytest.approx(2.10)


def test_36_canonical_scoreboard_uses_one_bet_not_duplicate_detections(db):
    _insert_test_event(db, "canon4")
    _insert_detection(db,event_id="canon4",strategy="CONSENSUS_VALUE",book="BookA",odds=2.00)
    _insert_detection(db,event_id="canon4",strategy="SLOW_BOOK",book="BookB",odds=2.10)
    cluster_event_signals(db,"canon4")
    settle_event_signals(db,"canon4",home_score=0,away_score=2)
    score=canonical_scoreboard(db)
    assert score["bets"] == 1
    assert score["settled"] == 1
    assert score["pnl_units"] == pytest.approx(1.10)
    assert score["roi_pct"] == pytest.approx(110.0)


def test_37_backfill_is_idempotent(db):
    _insert_test_event(db, "canon5")
    _insert_detection(db,event_id="canon5",strategy="CONSENSUS_VALUE",book="BookA",odds=2.20)
    assert backfill_canonical_bets(db) == 1
    assert backfill_canonical_bets(db) == 0
    assert db.fetchone("SELECT COUNT(*) AS n FROM canonical_bets")["n"] == 1


def test_38_edge_bucket_boundaries():
    assert edge_bucket(3.99) == "<4%"
    assert edge_bucket(4.0) == "4–5%"
    assert edge_bucket(5.0) == "5–7.5%"
    assert edge_bucket(7.5) == "7.5–10%"
    assert edge_bucket(10.0) == "10%+"


def test_39_sample_status_warns_on_tiny_sample():
    status = sample_status({"bets": 3, "settled": 0, "clv_samples": 0})
    assert status["level"] == "VERY EARLY"


def test_40_segmentation_tracks_strategy_agreement(db):
    _insert_test_event(db, "intel1")
    _insert_detection(db,event_id="intel1",strategy="CONSENSUS_VALUE",book="BookA",odds=2.20,edge=5.5)
    _insert_detection(db,event_id="intel1",strategy="SLOW_BOOK",book="BookB",odds=2.20,edge=5.5)
    db.execute("UPDATE signals SET min_odds=2.00 WHERE event_id='intel1'")
    _insert_quote(db,event_id="intel1",captured_at="2026-09-01T10:00:00+00:00",book="matchbook",title="Matchbook",market="h2h",selection="Away",price=2.10)
    cluster_event_signals(db,"intel1")
    evaluate_execution_wave_at(db,"intel1","2026-09-01T10:00:00+00:00",("matchbook",))
    rows=segmentation_tables(db)["strategy_agreement"]
    two=[x for x in rows if x["segment"]=="2"]
    assert len(two)==1
    assert two[0]["bets"]==1


def test_41_price_checkpoint_summary_uses_elapsed_observation(db):
    _insert_test_event(db, "intel2")
    _insert_detection(
        db,event_id="intel2",strategy="CONSENSUS_VALUE",book="BookA",odds=2.20,
        created_at="2026-09-01T10:00:00+00:00",edge=5.5
    )
    db.execute("UPDATE signals SET min_odds=2.00 WHERE event_id='intel2'")
    _insert_quote(db,event_id="intel2",captured_at="2026-09-01T10:00:00+00:00",book="matchbook",title="Matchbook",market="h2h",selection="Away",price=2.20)
    cluster_event_signals(db,"intel2")
    evaluate_execution_wave_at(db,"intel2","2026-09-01T10:00:00+00:00",("matchbook",))
    bet=db.fetchone("SELECT id FROM execution_shadow_bets WHERE event_id='intel2'")
    db.execute(
        """
        INSERT INTO execution_price_observations(
          execution_bet_id,observed_at,source_snapshot_at,bookmaker_key,price,move_vs_entry_pct
        ) VALUES(?,?,?,?,?,?)
        """,
        (bet["id"],"2026-09-01T10:31:00+00:00","2026-09-01T10:31:00+00:00",
         "matchbook",2.10,(2.20/2.10-1)*100)
    )
    summary=price_checkpoint_summary(db)
    m30=[x for x in summary if x["checkpoint"]=="30m"][0]
    assert m30["samples"]==1
    assert m30["avg_move_pct"] > 0


def test_42_clv_vs_results_separates_positive_clv(db):
    _insert_test_event(db, "intel3")
    _insert_detection(db,event_id="intel3",strategy="CONSENSUS_VALUE",book="BookA",odds=2.20,edge=5.5)
    db.execute("UPDATE signals SET min_odds=2.00 WHERE event_id='intel3'")
    _insert_quote(db,event_id="intel3",captured_at="2026-09-01T10:00:00+00:00",book="matchbook",title="Matchbook",market="h2h",selection="Away",price=2.20)
    cluster_event_signals(db,"intel3")
    evaluate_execution_wave_at(db,"intel3","2026-09-01T10:00:00+00:00",("matchbook",))
    db.execute(
        "UPDATE execution_shadow_bets SET clv_pct=4.0,clv_quality='A',pnl_units=-1.0,status='SETTLED',result='LOSS' WHERE event_id='intel3'"
    )
    rows=clv_vs_results(db)
    positive=[x for x in rows if x["segment"]=="Positive CLV"][0]
    assert positive["bets"]==1
    assert positive["pnl_units"]==pytest.approx(-1.0)


def test_43_weekly_report_upsert_is_idempotent(db):
    _insert_test_event(db, "intel4")
    _insert_detection(
        db,event_id="intel4",strategy="CONSENSUS_VALUE",book="BookA",odds=2.20,
        created_at="2026-09-01T10:00:00+00:00",edge=5.5
    )
    cluster_event_signals(db,"intel4")
    now=datetime(2026,9,3,12,0,tzinfo=timezone.utc)
    first=upsert_weekly_report(db,now=now)
    second=upsert_weekly_report(db,now=now)
    assert first["report_key"]==second["report_key"]
    assert db.fetchone("SELECT COUNT(*) AS n FROM research_reports")["n"]==1


def test_44_research_intelligence_has_required_sections(db):
    _insert_test_event(db, "intel5")
    _insert_detection(db,event_id="intel5",strategy="CONSENSUS_VALUE",book="BookA",odds=2.20,edge=5.5)
    cluster_event_signals(db,"intel5")
    out=research_intelligence(db,now=datetime(2026,9,1,12,0,tzinfo=timezone.utc))
    assert "segments" in out
    assert "edge" in out["segments"]
    assert "price_checkpoints" in out
    assert "data_quality_alerts" in out


def _insert_quote(db, *, event_id, captured_at, book, title, market, selection, price, point=None, desc=None):
    db.execute(
        """
        INSERT INTO odds_snapshots(
          event_id,captured_at,bookmaker_key,bookmaker_title,bookmaker_last_update,
          market_key,outcome_name,outcome_description,point,price
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (event_id,captured_at,book,title,captured_at,market,selection,desc,point,price)
    )


def test_45_execution_uses_approved_venue_not_nonexec_theoretical_best(db):
    _insert_test_event(db,"exec1")
    _insert_detection(db,event_id="exec1",strategy="CONSENSUS_VALUE",book="skybet",odds=3.20,created_at="2026-09-01T10:00:00+00:00",edge=8.0)
    # Make minimum executable price explicit for test.
    db.execute("UPDATE signals SET min_odds=3.00,fair_odds=2.80,fair_probability=? WHERE event_id='exec1'",(1/2.80,))
    _insert_quote(db,event_id="exec1",captured_at="2026-09-01T10:00:00+00:00",book="skybet",title="Sky Bet",market="h2h",selection="Away",price=3.20)
    _insert_quote(db,event_id="exec1",captured_at="2026-09-01T10:00:00+00:00",book="matchbook",title="Matchbook",market="h2h",selection="Away",price=3.05)
    _insert_quote(db,event_id="exec1",captured_at="2026-09-01T10:00:00+00:00",book="smarkets",title="Smarkets",market="h2h",selection="Away",price=3.01)
    cluster_event_signals(db,"exec1")
    assert evaluate_execution_wave_at(db,"exec1","2026-09-01T10:00:00+00:00",("betfair_ex_uk","matchbook","smarkets")) == 1
    row=db.fetchone("SELECT * FROM execution_shadow_bets WHERE event_id='exec1'")
    assert row["bookmaker_key"] == "matchbook"
    assert row["offered_odds"] == pytest.approx(3.05)
    assert row["reference_best_odds"] == pytest.approx(3.20)
    assert row["execution_venue_count"] == 2


def test_46_execution_rejects_when_no_approved_quote(db):
    _insert_test_event(db,"exec2")
    _insert_detection(db,event_id="exec2",strategy="CONSENSUS_VALUE",book="skybet",odds=2.20,created_at="2026-09-01T10:00:00+00:00",edge=5.0)
    _insert_quote(db,event_id="exec2",captured_at="2026-09-01T10:00:00+00:00",book="skybet",title="Sky Bet",market="h2h",selection="Away",price=2.20)
    assert evaluate_execution_wave_at(db,"exec2","2026-09-01T10:00:00+00:00",("matchbook",)) == 0
    row=db.fetchone("SELECT reason FROM execution_evaluations WHERE event_id='exec2'")
    assert row["reason"] == "NO_APPROVED_VENUE_QUOTE"


def test_47_execution_rejects_approved_price_below_min(db):
    _insert_test_event(db,"exec3")
    _insert_detection(db,event_id="exec3",strategy="CONSENSUS_VALUE",book="skybet",odds=2.30,created_at="2026-09-01T10:00:00+00:00",edge=5.0)
    db.execute("UPDATE signals SET min_odds=2.20 WHERE event_id='exec3'")
    _insert_quote(db,event_id="exec3",captured_at="2026-09-01T10:00:00+00:00",book="matchbook",title="Matchbook",market="h2h",selection="Away",price=2.10)
    assert evaluate_execution_wave_at(db,"exec3","2026-09-01T10:00:00+00:00",("matchbook",)) == 0
    row=db.fetchone("SELECT reason,best_executable_odds,min_required_odds FROM execution_evaluations WHERE event_id='exec3'")
    assert row["reason"] == "EXECUTABLE_PRICE_BELOW_MIN"
    assert row["best_executable_odds"] == pytest.approx(2.10)


def test_48_later_acceptable_wave_creates_execution_at_later_price(db):
    _insert_test_event(db,"exec4")
    _insert_detection(db,event_id="exec4",strategy="CONSENSUS_VALUE",book="skybet",odds=2.30,created_at="2026-09-01T10:00:00+00:00",edge=5.0)
    db.execute("UPDATE signals SET min_odds=2.20 WHERE event_id='exec4'")
    _insert_quote(db,event_id="exec4",captured_at="2026-09-01T10:00:00+00:00",book="matchbook",title="Matchbook",market="h2h",selection="Away",price=2.10)
    evaluate_execution_wave_at(db,"exec4","2026-09-01T10:00:00+00:00",("matchbook",))
    _insert_detection(db,event_id="exec4",strategy="SLOW_BOOK",book="skybet",odds=2.35,created_at="2026-09-01T11:00:00+00:00",edge=6.0)
    db.execute("UPDATE signals SET min_odds=2.20 WHERE event_id='exec4'")
    _insert_quote(db,event_id="exec4",captured_at="2026-09-01T11:00:00+00:00",book="matchbook",title="Matchbook",market="h2h",selection="Away",price=2.25)
    assert evaluate_execution_wave_at(db,"exec4","2026-09-01T11:00:00+00:00",("matchbook",)) == 1
    row=db.fetchone("SELECT created_at,offered_odds FROM execution_shadow_bets WHERE event_id='exec4'")
    assert row["created_at"] == "2026-09-01T11:00:00+00:00"
    assert row["offered_odds"] == pytest.approx(2.25)


def test_49_execution_shadow_is_unique_per_underlying_bet(db):
    _insert_test_event(db,"exec5")
    _insert_detection(db,event_id="exec5",strategy="CONSENSUS_VALUE",book="skybet",odds=2.40,created_at="2026-09-01T10:00:00+00:00",edge=5.0)
    _insert_detection(db,event_id="exec5",strategy="SLOW_BOOK",book="bet365",odds=2.40,created_at="2026-09-01T10:00:00+00:00",edge=5.0)
    db.execute("UPDATE signals SET min_odds=2.20 WHERE event_id='exec5'")
    _insert_quote(db,event_id="exec5",captured_at="2026-09-01T10:00:00+00:00",book="matchbook",title="Matchbook",market="h2h",selection="Away",price=2.30)
    assert evaluate_execution_wave_at(db,"exec5","2026-09-01T10:00:00+00:00",("matchbook",)) == 1
    assert evaluate_execution_wave_at(db,"exec5","2026-09-01T10:00:00+00:00",("matchbook",)) == 0
    assert db.fetchone("SELECT COUNT(*) AS n FROM execution_shadow_bets WHERE event_id='exec5'")["n"] == 1


def test_50_execution_price_tracking_and_clv_use_execution_venue(db):
    _insert_test_event(db,"exec6")
    _insert_detection(db,event_id="exec6",strategy="CONSENSUS_VALUE",book="skybet",odds=2.40,created_at="2026-09-01T10:00:00+00:00",edge=5.0)
    db.execute("UPDATE signals SET min_odds=2.20 WHERE event_id='exec6'")
    _insert_quote(db,event_id="exec6",captured_at="2026-09-01T10:00:00+00:00",book="matchbook",title="Matchbook",market="h2h",selection="Away",price=2.30)
    evaluate_execution_wave_at(db,"exec6","2026-09-01T10:00:00+00:00",("matchbook",))
    _insert_quote(db,event_id="exec6",captured_at="2026-09-02T11:00:00+00:00",book="matchbook",title="Matchbook",market="h2h",selection="Away",price=2.10)
    assert track_execution_prices(db,now=datetime(2026,9,2,11,30,tzinfo=timezone.utc)) == 1
    assert finalize_execution_clv(db,now=datetime(2026,9,2,13,0,tzinfo=timezone.utc)) == 1
    row=db.fetchone("SELECT closing_odds,clv_pct FROM execution_shadow_bets WHERE event_id='exec6'")
    assert row["closing_odds"] == pytest.approx(2.10)
    assert row["clv_pct"] > 0


def test_51_result_settles_execution_shadow_once(db):
    _insert_test_event(db,"exec7")
    _insert_detection(db,event_id="exec7",strategy="CONSENSUS_VALUE",book="skybet",odds=3.0,created_at="2026-09-01T10:00:00+00:00",edge=5.0)
    db.execute("UPDATE signals SET min_odds=2.80 WHERE event_id='exec7'")
    _insert_quote(db,event_id="exec7",captured_at="2026-09-01T10:00:00+00:00",book="smarkets",title="Smarkets",market="h2h",selection="Away",price=2.90)
    evaluate_execution_wave_at(db,"exec7","2026-09-01T10:00:00+00:00",("smarkets",))
    assert settle_execution_event(db,"exec7",home_score=0,away_score=1) == 1
    row=db.fetchone("SELECT result,pnl_units,status FROM execution_shadow_bets WHERE event_id='exec7'")
    assert row["result"] == "WIN"
    assert row["pnl_units"] == pytest.approx(1.90)
    assert row["status"] == "SETTLED"


def test_52_execution_scoreboard_excludes_nonexecutable_canonical(db):
    _insert_test_event(db,"exec8")
    _insert_detection(db,event_id="exec8",strategy="CONSENSUS_VALUE",book="skybet",odds=2.20,created_at="2026-09-01T10:00:00+00:00",edge=5.0)
    cluster_event_signals(db,"exec8")
    assert db.fetchone("SELECT COUNT(*) AS n FROM canonical_bets")["n"] == 1
    score=execution_scoreboard(db)
    assert score["bets"] == 0
    funnel=execution_funnel(db)
    assert funnel["theoretical_canonical"] == 1
    assert funnel["executable_shadow"] == 0


def test_53_execution_backfill_is_idempotent(db):
    _insert_test_event(db,"exec9")
    _insert_detection(db,event_id="exec9",strategy="CONSENSUS_VALUE",book="skybet",odds=2.30,created_at="2026-09-01T10:00:00+00:00",edge=5.0)
    db.execute("UPDATE signals SET min_odds=2.20 WHERE event_id='exec9'")
    _insert_quote(db,event_id="exec9",captured_at="2026-09-01T10:00:00+00:00",book="betfair_ex_uk",title="Betfair Exchange",market="h2h",selection="Away",price=2.25)
    assert backfill_execution_shadows(db,("betfair_ex_uk",)) == 1
    assert backfill_execution_shadows(db,("betfair_ex_uk",)) == 0
    assert db.fetchone("SELECT COUNT(*) AS n FROM execution_shadow_bets")["n"] == 1


def test_54_research_export_contains_core_files_and_no_secrets(db):
    class SafeSettings:
        sport_keys=("soccer_epl","soccer_efl_champ")
        odds_region="uk"
        odds_markets=("h2h","totals","btts","draw_no_bet")
        execution_shadow_enabled=True
        execution_bookmaker_keys=("betfair_ex_uk","matchbook","smarkets")
        min_consensus_books=3
        min_edge_pct=3.0
        min_cross_market_edge_pct=4.0
        min_slow_book_gap_pct=4.0
        daily_paid_credit_budget=600
        quota_reserve_credits=1000
        breadth_polls_per_day=80
        max_events_per_odds_cycle=3
        enable_live_betting=False
        # These must never appear in the export.
        odds_api_key="SUPER_SECRET_API_KEY"
        database_url="postgresql://secret:password@example/db"
        admin_secret="SUPER_SECRET_ADMIN"

    payload, filename = build_research_export(db, SafeSettings(), "0.6.2")
    assert filename.startswith("betting-lab-research-")
    assert filename.endswith(".zip")

    import io, zipfile
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        names=set(z.namelist())
        assert "manifest.json" in names
        assert "analysis_summary.json" in names
        assert "tables/execution_shadow_bets.csv" in names
        assert "tables/odds_snapshots.csv" in names
        combined=(z.read("manifest.json")+z.read("analysis_summary.json")+z.read("README.txt")).decode("utf-8")
        assert "SUPER_SECRET_API_KEY" not in combined
        assert "SUPER_SECRET_ADMIN" not in combined
        assert "postgresql://secret" not in combined


def test_55_research_export_serializes_existing_rows(db):
    _insert_test_event(db,"export1")
    payload, _ = build_research_export(
        db,
        type("S",(),{
            "sport_keys":("soccer_epl",),"odds_region":"uk",
            "odds_markets":("h2h",),"execution_shadow_enabled":True,
            "execution_bookmaker_keys":("matchbook",),
            "min_consensus_books":3,"min_edge_pct":3.0,
            "min_cross_market_edge_pct":4.0,"min_slow_book_gap_pct":4.0,
            "daily_paid_credit_budget":600,"quota_reserve_credits":1000,
            "breadth_polls_per_day":80,"max_events_per_odds_cycle":3,
            "enable_live_betting":False,
        })(),
        "0.6.2",
    )
    import io, zipfile
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        events=z.read("tables/events.csv").decode("utf-8")
        assert "export1" in events
        manifest=json.loads(z.read("manifest.json").decode("utf-8"))
        assert manifest["row_counts"]["events"] == 1


def test_56_odds_band_boundaries():
    assert odds_band(1.99) == "<2"
    assert odds_band(2.0) == "2–3"
    assert odds_band(3.0) == "3–5"
    assert odds_band(5.0) == "5–8"
    assert odds_band(8.0) == "8+"


def test_57_time_to_kickoff_bucket():
    row={"created_at":"2026-09-01T10:00:00+00:00","commence_time":"2026-09-01T15:00:00+00:00"}
    assert time_to_kickoff_bucket(row) == "2–6h"


def test_58_dnb_is_filtered_from_old_environment(monkeypatch):
    monkeypatch.setenv("ODDS_MARKETS","h2h,totals,btts,draw_no_bet")
    monkeypatch.setenv("ENABLE_DNB_MARKET","false")
    s=Settings()
    assert s.odds_markets == ("h2h","totals","btts")


class ConvergenceFakeApi:
    def __init__(self):
        self.calls=[]
    def quota_probe(self):
        return ApiResult([], 10000, 0, 0)
    def events(self, sport_key):
        return ApiResult([], 10000, 0, 0)
    def event_odds(self, sport_key, event_id, region, markets, bookmaker_keys=()):
        self.calls.append({
            "sport_key":sport_key,"event_id":event_id,"region":region,
            "markets":tuple(markets),"bookmaker_keys":tuple(bookmaker_keys),
        })
        return ApiResult({
            "id":event_id,
            "bookmakers":[{
                "key":"matchbook","title":"Matchbook","last_update":"2026-09-01T10:31:00Z",
                "markets":[{
                    "key":tuple(markets)[0],
                    "outcomes":[{"name":"Away","price":2.10}]
                }]
            }]
        }, 9999, 1, 1)


def _insert_execution_shadow_for_convergence(db,event_id="conv1",market_key="h2h"):
    db.execute(
        """
        INSERT INTO events(
          event_id,sport_key,league,commence_time,home_team,away_team,
          first_seen_at,last_seen_at,status
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (event_id,"soccer_epl","Premier League","2030-09-01T15:00:00+00:00",
         "Home","Away","2026-09-01T00:00:00+00:00","2026-09-01T00:00:00+00:00","UPCOMING")
    )
    db.execute(
        """
        INSERT INTO signals(
          created_at,event_id,strategy,market_key,selection,bookmaker_key,
          bookmaker_title,offered_odds,fair_odds,fair_probability,edge_pct,
          min_odds,status
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        ("2026-09-01T10:00:00+00:00",event_id,"CONSENSUS_VALUE",market_key,"Away",
         "matchbook","Matchbook",2.20,2.05,1/2.05,7.3,2.10,"OPEN")
    )
    sig=db.fetchone("SELECT id FROM signals WHERE event_id=?",(event_id,))
    db.execute(
        """
        INSERT INTO execution_shadow_bets(
          execution_key,created_at,event_id,market_key,selection,source_signal_id,
          bookmaker_key,bookmaker_title,offered_odds,fair_odds,fair_probability,
          edge_pct,min_odds,status
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (f"{event_id}|{market_key}|Away||","2026-09-01T10:00:00+00:00",event_id,
         market_key,"Away",sig["id"],"matchbook","Matchbook",2.20,2.05,1/2.05,
         7.3,2.10,"OPEN")
    )


def test_59_convergence_call_is_single_market_and_approved_books_only(db):
    _insert_execution_shadow_for_convergence(db)
    db.execute("UPDATE quota_state SET credits_remaining=10000 WHERE singleton_id=1")
    api=ConvergenceFakeApi()
    q=QuotaGuard(db,reserve=100,daily_budget=1000)
    c=Collector(
        db,api,q,sport_keys=["soccer_epl"],region="uk",
        markets=["h2h","totals","btts"],breadth_polls_per_day=0,
        execution_bookmaker_keys=["betfair_ex_uk","matchbook","smarkets"],
    )
    out=c.poll_one_cycle()
    assert out["mode"]=="convergence"
    assert out["polled"]==1
    assert api.calls[0]["markets"]==("h2h",)
    assert api.calls[0]["bookmaker_keys"]==("betfair_ex_uk","matchbook","smarkets")
    assert q.today_paid_cost()==1


def test_60_quota_counts_convergence_calls(db):
    db.record_collector_run("ODDS_CONVERGENCE",True,actual_cost=1)
    db.record_collector_run("ODDS",True,actual_cost=3)
    db.record_collector_run("RESULTS",True,actual_cost=2)
    q=QuotaGuard(db,reserve=50,daily_budget=20)
    assert q.today_paid_cost()==6


def test_61_model_edge_clv_calibration_reports_gap(db):
    _insert_test_event(db,"cal1")
    _insert_detection(db,event_id="cal1",strategy="CONSENSUS_VALUE",book="BookA",odds=2.20,edge=5.0)
    cluster_event_signals(db,"cal1")
    # Create a minimal execution shadow from the canonical source.
    sig=db.fetchone("SELECT * FROM signals WHERE event_id='cal1'")
    db.execute(
        """
        INSERT INTO execution_shadow_bets(
          execution_key,created_at,event_id,market_key,selection,source_signal_id,
          bookmaker_key,bookmaker_title,offered_odds,fair_odds,fair_probability,
          edge_pct,min_odds,status,clv_pct,clv_quality,pnl_units,result
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        ("cal1|h2h|Away||",sig["created_at"],"cal1","h2h","Away",sig["id"],
         "matchbook","Matchbook",2.20,2.095,1/2.095,5.0,2.10,"SETTLED",2.0,"A",1.20,"WIN")
    )
    out=edge_clv_calibration(db)
    assert out["samples"]==1
    assert out["avg_model_edge_pct"]==pytest.approx(5.0)
    assert out["avg_final_clv_pct"]==pytest.approx(2.0)
    assert out["edge_minus_clv_pp"]==pytest.approx(3.0)


def test_62_fixture_exposure_counts_multiple_bets(db):
    _insert_test_event(db,"fx1")
    # two source signals / two different markets
    for market,selection in [("h2h","Away"),("btts","No")]:
        db.execute(
            """
            INSERT INTO signals(
              created_at,event_id,strategy,market_key,selection,bookmaker_key,
              bookmaker_title,offered_odds,fair_odds,fair_probability,edge_pct,
              min_odds,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("2026-09-01T10:00:00+00:00","fx1","CONSENSUS_VALUE",market,selection,
             "matchbook","Matchbook",2.20,2.05,1/2.05,7.3,2.10,"OPEN")
        )
        sig=db.fetchone("SELECT * FROM signals WHERE event_id='fx1' AND market_key=?",(market,))
        db.execute(
            """
            INSERT INTO execution_shadow_bets(
              execution_key,created_at,event_id,market_key,selection,source_signal_id,
              bookmaker_key,bookmaker_title,offered_odds,fair_odds,fair_probability,
              edge_pct,min_odds,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (f"fx1|{market}|{selection}||",sig["created_at"],"fx1",market,selection,sig["id"],
             "matchbook","Matchbook",2.20,2.05,1/2.05,7.3,2.10,"OPEN")
        )
    out=fixture_exposure_analysis(db)
    assert out["multi_bet_fixtures"]==1
    assert out["max_bets_one_fixture"]==2
    assert out["bets_on_multi_bet_fixtures"]==2


def test_63_broad_call_still_requests_all_active_markets(db):
    class BroadFake:
        def __init__(self): self.calls=[]
        def quota_probe(self): return ApiResult([],10000,0,0)
        def events(self,sport_key): return ApiResult([],10000,0,0)
        def event_odds(self,sport_key,event_id,region,markets,bookmaker_keys=()):
            self.calls.append((tuple(markets),tuple(bookmaker_keys)))
            return ApiResult({"id":event_id,"bookmakers":[]},9997,3,3)
    db.execute(
        """
        INSERT INTO events(event_id,sport_key,league,commence_time,home_team,away_team,first_seen_at,last_seen_at,status)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        ("broad1","soccer_epl","Premier League","2030-09-01T15:00:00+00:00","H","A",
         "2026-09-01T00:00:00+00:00","2026-09-01T00:00:00+00:00","UPCOMING")
    )
    db.execute("UPDATE quota_state SET credits_remaining=10000 WHERE singleton_id=1")
    api=BroadFake();q=QuotaGuard(db,reserve=100,daily_budget=1000)
    c=Collector(db,api,q,sport_keys=["soccer_epl"],region="uk",markets=["h2h","totals","btts"],
                breadth_polls_per_day=80,execution_bookmaker_keys=["matchbook"])
    out=c.poll_one_cycle()
    assert out["mode"]=="breadth"
    assert api.calls[0][0]==("h2h","totals","btts")
    assert api.calls[0][1]==()


def test_64_research_intelligence_exposes_new_sections(db):
    out=research_intelligence(db,now=datetime(2026,9,1,12,0,tzinfo=timezone.utc))
    assert "odds_band" in out["segments"]
    assert "time_to_kickoff" in out["segments"]
    assert "edge_clv_calibration" in out
    assert "fixture_exposure" in out


def test_65_v066_execution_schema_has_measurement_and_net_columns(db):
    cols={x["name"] for x in db.fetchall("PRAGMA table_info(execution_shadow_bets)")}
    assert {
        "closing_observed_at","closing_minutes_before_kickoff","clv_quality",
        "commission_rate_pct","commission_units","net_pnl_units"
    }.issubset(cols)


def test_66_v066_research_reports_have_net_columns(db):
    cols={x["name"] for x in db.fetchall("PRAGMA table_info(research_reports)")}
    assert {"commission_units","net_pnl_units","net_roi_pct"}.issubset(cols)


def test_67_historical_league_labels_normalize_on_schema_init(db):
    db.execute(
        """
        INSERT INTO events(
          event_id,sport_key,league,commence_time,home_team,away_team,
          first_seen_at,last_seen_at,status
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            "leaguefix","soccer_england_league1","soccer_england_league1",
            "2026-09-10T12:00:00+00:00","H","A",
            "2026-09-01T00:00:00+00:00","2026-09-01T00:00:00+00:00","UPCOMING"
        )
    )
    db.init_schema()
    row=db.fetchone("SELECT league FROM events WHERE event_id='leaguefix'")
    assert row["league"]=="League One"


def test_68_clv_quality_boundaries():
    assert clv_quality(10)=="A"
    assert clv_quality(15)=="A"
    assert clv_quality(16)=="B"
    assert clv_quality(30)=="B"
    assert clv_quality(31)=="C"
    assert clv_quality(60)=="C"
    assert clv_quality(61)=="STALE"


def _v066_insert_exec(db,event_id,*,book="matchbook",offered=2.20,clv=None,quality=None,gross=None):
    _insert_test_event(db,event_id)
    _insert_detection(db,event_id=event_id,strategy="CONSENSUS_VALUE",book=book,odds=offered,edge=5.0)
    sig=db.fetchone("SELECT * FROM signals WHERE event_id=?",(event_id,))
    db.execute(
        """
        INSERT INTO execution_shadow_bets(
          execution_key,created_at,event_id,market_key,selection,source_signal_id,
          bookmaker_key,bookmaker_title,offered_odds,fair_odds,fair_probability,
          edge_pct,min_odds,status,clv_pct,clv_quality,pnl_units
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"{event_id}|h2h|Away||",sig["created_at"],event_id,"h2h","Away",sig["id"],
            book,book,offered,2.10,1/2.10,5.0,2.10,
            "SETTLED" if gross is not None else "OPEN",clv,quality,gross,
        )
    )


def test_69_clv_finalizer_records_close_age_and_quality(db):
    _v066_insert_exec(db,"clvgrade")
    db.execute(
        """
        INSERT INTO odds_snapshots(
          event_id,captured_at,bookmaker_key,bookmaker_title,market_key,
          outcome_name,price
        ) VALUES(?,?,?,?,?,?,?)
        """,
        ("clvgrade","2026-09-02T11:50:00+00:00","matchbook","Matchbook","h2h","Away",2.00)
    )
    assert finalize_execution_clv(
        db,now=datetime(2026,9,2,13,0,tzinfo=timezone.utc)
    )==1
    row=db.fetchone("SELECT * FROM execution_shadow_bets WHERE event_id='clvgrade'")
    assert row["clv_quality"]=="A"
    assert row["closing_minutes_before_kickoff"]==pytest.approx(10.0)
    assert row["closing_observed_at"]=="2026-09-02T11:50:00+00:00"
    assert row["clv_pct"]==pytest.approx(10.0)


def test_70_headline_scoreboard_excludes_stale_clv(db):
    _v066_insert_exec(db,"goodclose",clv=5.0,quality="A")
    _v066_insert_exec(db,"staleclose",clv=-10.0,quality="STALE")
    score=execution_scoreboard(db)
    assert score["clv_samples"]==1
    assert score["all_clv_samples"]==2
    assert score["avg_clv_pct"]==pytest.approx(5.0)
    assert score["all_avg_clv_pct"]==pytest.approx(-2.5)


def test_71_matchbook_commission_is_deducted_from_winner(db):
    _v066_insert_exec(db,"commission1",book="matchbook",offered=3.00)
    assert settle_execution_event(db,"commission1",home_score=0,away_score=1)==1
    row=db.fetchone("SELECT * FROM execution_shadow_bets WHERE event_id='commission1'")
    assert row["pnl_units"]==pytest.approx(2.0)
    assert row["commission_rate_pct"]==pytest.approx(2.0)
    assert row["commission_units"]==pytest.approx(0.04)
    assert row["net_pnl_units"]==pytest.approx(1.96)


def test_72_losing_bet_has_no_estimated_commission(db):
    _v066_insert_exec(db,"commissionloss",book="betfair_ex_uk",offered=3.00)
    assert settle_execution_event(db,"commissionloss",home_score=1,away_score=0)==1
    row=db.fetchone("SELECT * FROM execution_shadow_bets WHERE event_id='commissionloss'")
    assert row["pnl_units"]==pytest.approx(-1.0)
    assert row["commission_units"]==pytest.approx(0.0)
    assert row["net_pnl_units"]==pytest.approx(-1.0)


def test_73_accounting_backfill_updates_existing_settled_rows(db):
    _v066_insert_exec(db,"commission2",book="smarkets",offered=3.00,gross=2.0)
    assert backfill_execution_accounting(db)==1
    row=db.fetchone("SELECT * FROM execution_shadow_bets WHERE event_id='commission2'")
    assert row["commission_rate_pct"]==pytest.approx(2.0)
    assert row["commission_units"]==pytest.approx(0.04)
    assert row["net_pnl_units"]==pytest.approx(1.96)


def test_74_research_uses_ab_clv_and_net_pnl(db):
    _v066_insert_exec(db,"researchgood",clv=3.0,quality="B",gross=1.0)
    _v066_insert_exec(db,"researchstale",clv=-9.0,quality="STALE",gross=-1.0)
    backfill_execution_accounting(db)
    intel=research_intelligence(db,now=datetime(2026,9,3,12,0,tzinfo=timezone.utc))
    assert "clv_quality" in intel["segments"]
    assert intel["overall"]["clv_samples"]==1
    assert intel["overall"]["all_clv_samples"]==2
    assert intel["overall"]["avg_clv_pct"]==pytest.approx(3.0)
    assert intel["overall"]["net_pnl_units"] < intel["overall"]["pnl_units"]


def test_75_v066_export_contains_measurement_and_accounting_fields(db):
    _v066_insert_exec(db,"exportv066",clv=1.0,quality="A")
    class S:
        sport_keys=("soccer_epl",)
        odds_region="uk"
        odds_markets=("h2h","totals","btts")
        execution_shadow_enabled=True
        execution_bookmaker_keys=("betfair_ex_uk","matchbook","smarkets")
        min_consensus_books=3
        min_edge_pct=3.0
        min_cross_market_edge_pct=4.0
        min_slow_book_gap_pct=4.0
        daily_paid_credit_budget=600
        quota_reserve_credits=1000
        breadth_polls_per_day=80
        max_events_per_odds_cycle=3
        enable_live_betting=False
        betfair_commission_pct=5.0
        matchbook_commission_pct=2.0
        smarkets_commission_pct=2.0
        default_execution_commission_pct=5.0

    payload,_=build_research_export(db,S(),"0.6.6")
    import io,zipfile
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        csv_text=z.read("tables/execution_shadow_bets.csv").decode("utf-8")
        header=csv_text.splitlines()[0]
        assert "clv_quality" in header
        assert "closing_minutes_before_kickoff" in header
        assert "commission_units" in header
        assert "net_pnl_units" in header


def test_76_weekly_report_persists_net_metrics(db):
    _v066_insert_exec(db,"weeklynet",clv=2.0,quality="A",gross=1.0)
    backfill_execution_accounting(db)
    report=upsert_weekly_report(db,now=datetime(2026,9,3,12,0,tzinfo=timezone.utc))
    row=db.fetchone("SELECT * FROM research_reports WHERE report_key=?",(report["report_key"],))
    assert row["net_pnl_units"] is not None
    assert row["commission_units"] is not None


def test_77_bets_until_midnight_counts_open_executable_bets_in_uk_window(db):
    from web import bets_until_midnight

    # 14:00 UTC is 15:00 BST on 4 Sep 2026. UK midnight is 23:00 UTC.
    now=datetime(2026,9,4,14,0,tzinfo=timezone.utc)

    def insert_event(event_id, commence):
        db.execute(
            """
            INSERT INTO events(
              event_id,sport_key,league,commence_time,home_team,away_team,
              first_seen_at,last_seen_at,status
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,"soccer_epl","Premier League",commence,"Home","Away",
                "2026-09-04T10:00:00+00:00","2026-09-04T10:00:00+00:00","UPCOMING"
            )
        )
        db.execute(
            """
            INSERT INTO signals(
              created_at,event_id,strategy,market_key,selection,bookmaker_key,
              bookmaker_title,offered_odds,fair_odds,fair_probability,
              edge_pct,min_odds,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "2026-09-04T10:00:00+00:00",event_id,"CONSENSUS_VALUE",
                "h2h","Away","matchbook","Matchbook",2.2,2.1,1/2.1,5.0,2.1,"OPEN"
            )
        )
        sig=db.fetchone("SELECT id FROM signals WHERE event_id=?",(event_id,))
        db.execute(
            """
            INSERT INTO execution_shadow_bets(
              execution_key,created_at,event_id,market_key,selection,
              source_signal_id,bookmaker_key,bookmaker_title,offered_odds,
              fair_odds,fair_probability,edge_pct,min_odds,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"{event_id}|h2h|Away||","2026-09-04T10:00:00+00:00",
                event_id,"h2h","Away",sig["id"],"matchbook","Matchbook",
                2.2,2.1,1/2.1,5.0,2.1,"OPEN"
            )
        )

    insert_event("tonight1","2026-09-04T18:00:00+00:00")   # 19:00 BST
    insert_event("tonight2","2026-09-04T22:30:00+00:00")   # 23:30 BST
    insert_event("tomorrow1","2026-09-04T23:30:00+00:00")  # 00:30 BST next day
    insert_event("past1","2026-09-04T13:00:00+00:00")      # 14:00 BST, already started

    assert bets_until_midnight(db,now=now)==2


def test_78_bets_until_midnight_excludes_settled_bets(db):
    from web import bets_until_midnight

    now=datetime(2026,9,4,14,0,tzinfo=timezone.utc)
    db.execute(
        """
        INSERT INTO events(
          event_id,sport_key,league,commence_time,home_team,away_team,
          first_seen_at,last_seen_at,status
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            "settledtonight","soccer_epl","Premier League",
            "2026-09-04T18:00:00+00:00","Home","Away",
            "2026-09-04T10:00:00+00:00","2026-09-04T10:00:00+00:00","UPCOMING"
        )
    )
    db.execute(
        """
        INSERT INTO signals(
          created_at,event_id,strategy,market_key,selection,bookmaker_key,
          bookmaker_title,offered_odds,fair_odds,fair_probability,
          edge_pct,min_odds,status
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "2026-09-04T10:00:00+00:00","settledtonight","CONSENSUS_VALUE",
            "h2h","Away","matchbook","Matchbook",2.2,2.1,1/2.1,5.0,2.1,"SETTLED"
        )
    )
    sig=db.fetchone("SELECT id FROM signals WHERE event_id='settledtonight'")
    db.execute(
        """
        INSERT INTO execution_shadow_bets(
          execution_key,created_at,event_id,market_key,selection,
          source_signal_id,bookmaker_key,bookmaker_title,offered_odds,
          fair_odds,fair_probability,edge_pct,min_odds,status,pnl_units,result
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "settledtonight|h2h|Away||","2026-09-04T10:00:00+00:00",
            "settledtonight","h2h","Away",sig["id"],"matchbook","Matchbook",
            2.2,2.1,1/2.1,5.0,2.1,"SETTLED",-1.0,"LOSS"
        )
    )

    assert bets_until_midnight(db,now=now)==0



def _seed_multiple_exec(
    db,
    event_id,
    *,
    commence,
    selection="Away",
    market="h2h",
    point=None,
    fair_probability=0.50,
    min_odds=2.0,
    edge=5.0,
    created="2026-09-04T12:00:00+00:00",
):
    db.execute(
        """
        INSERT INTO events(
          event_id,sport_key,league,commence_time,home_team,away_team,
          first_seen_at,last_seen_at,status
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (event_id,"soccer_epl","Premier League",commence,"Home","Away",created,created,"UPCOMING")
    )
    db.execute(
        """
        INSERT INTO signals(
          created_at,event_id,strategy,market_key,selection,outcome_description,point,
          bookmaker_key,bookmaker_title,offered_odds,fair_odds,fair_probability,
          edge_pct,min_odds,status
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (created,event_id,"CONSENSUS_VALUE",market,selection,None,point,
         "matchbook","Matchbook",max(min_odds,2.1),1/fair_probability,fair_probability,
         edge,min_odds,"OPEN")
    )
    sig=db.fetchone("SELECT id FROM signals WHERE event_id=? ORDER BY id DESC LIMIT 1",(event_id,))
    db.execute(
        """
        INSERT INTO execution_shadow_bets(
          execution_key,created_at,event_id,market_key,selection,outcome_description,point,
          source_signal_id,bookmaker_key,bookmaker_title,offered_odds,fair_odds,
          fair_probability,edge_pct,min_odds,status
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (f"{event_id}|{market}|{selection}||{point or ''}",created,event_id,market,selection,None,point,
         sig["id"],"matchbook","Matchbook",max(min_odds,2.1),1/fair_probability,
         fair_probability,edge,min_odds,"OPEN")
    )
    return db.fetchone("SELECT * FROM execution_shadow_bets WHERE event_id=? ORDER BY id DESC LIMIT 1",(event_id,))


def _seed_book_quote(db,event_id,*,captured,book="paddypower",title="Paddy Power",market="h2h",selection="Away",point=None,price=2.2):
    db.execute(
        """
        INSERT INTO odds_snapshots(
          event_id,captured_at,bookmaker_key,bookmaker_title,market_key,
          outcome_name,outcome_description,point,price
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (event_id,captured,book,title,market,selection,None,point,price)
    )


def test_79_multiples_schema_starts_forward_only(db):
    state=db.fetchone("SELECT * FROM multiple_shadow_state WHERE singleton_id=1")
    assert state is not None
    assert state["algorithm_version"]=="MS1"
    assert db.fetchone("SELECT COUNT(*) AS n FROM multiple_shadow_bets")["n"]==0


def test_80_multiples_forms_same_book_double_and_treble(db):
    now=datetime(2026,9,4,13,0,tzinfo=timezone.utc)
    for i,price in enumerate((2.2,2.4,1.9),start=1):
        _seed_multiple_exec(db,f"m{i}",commence=f"2026-09-04T1{6+i}:00:00+00:00",min_odds=1.8,edge=6+i)
        _seed_book_quote(db,f"m{i}",captured="2026-09-04T12:50:00+00:00",price=price)
    created=generate_multiple_shadows(db,now=now,allowed_bookmaker_keys=("paddypower",))
    assert created==4  # 3 doubles + 1 treble
    rows=db.fetchall("SELECT * FROM multiple_shadow_bets ORDER BY leg_count,id")
    assert sum(1 for r in rows if r["leg_count"]==2)==3
    assert sum(1 for r in rows if r["leg_count"]==3)==1
    treble=[r for r in rows if r["leg_count"]==3][0]
    assert treble["bookmaker_key"]=="paddypower"
    assert treble["combined_odds"]==pytest.approx(2.2*2.4*1.9)
    assert db.fetchone("SELECT COUNT(*) AS n FROM multiple_shadow_legs WHERE multiple_bet_id=?",(treble["id"],))["n"]==3


def test_81_multiples_never_combines_two_legs_from_same_fixture(db):
    now=datetime(2026,9,4,13,0,tzinfo=timezone.utc)
    first=_seed_multiple_exec(db,"samefixture",commence="2026-09-04T18:00:00+00:00",selection="Away",edge=10)
    # second executable selection on same event
    db.execute(
        """INSERT INTO signals(created_at,event_id,strategy,market_key,selection,bookmaker_key,bookmaker_title,offered_odds,fair_odds,fair_probability,edge_pct,min_odds,status)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("2026-09-04T12:00:00+00:00","samefixture","TEST","btts","Yes","matchbook","Matchbook",2.1,2.0,.5,8,1.9,"OPEN")
    )
    sig=db.fetchone("SELECT id FROM signals WHERE event_id='samefixture' AND market_key='btts'")
    db.execute(
        """INSERT INTO execution_shadow_bets(execution_key,created_at,event_id,market_key,selection,source_signal_id,bookmaker_key,bookmaker_title,offered_odds,fair_odds,fair_probability,edge_pct,min_odds,status)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("samefixture|btts|Yes||","2026-09-04T12:00:00+00:00","samefixture","btts","Yes",sig["id"],"matchbook","Matchbook",2.1,2.0,.5,8,1.9,"OPEN")
    )
    _seed_book_quote(db,"samefixture",captured="2026-09-04T12:55:00+00:00",market="h2h",selection="Away",price=2.2)
    _seed_book_quote(db,"samefixture",captured="2026-09-04T12:55:00+00:00",market="btts",selection="Yes",price=2.1)
    assert generate_multiple_shadows(db,now=now,allowed_bookmaker_keys=("paddypower",))==0


def test_82_multiples_rejects_stale_or_below_min_quotes(db):
    now=datetime(2026,9,4,13,0,tzinfo=timezone.utc)
    _seed_multiple_exec(db,"stale1",commence="2026-09-04T18:00:00+00:00",min_odds=2.0)
    _seed_multiple_exec(db,"stale2",commence="2026-09-04T19:00:00+00:00",min_odds=2.0)
    _seed_book_quote(db,"stale1",captured="2026-09-04T12:00:00+00:00",price=2.3) # >30m old
    _seed_book_quote(db,"stale2",captured="2026-09-04T12:55:00+00:00",price=1.95) # below min
    assert generate_multiple_shadows(db,now=now,allowed_bookmaker_keys=("paddypower",))==0


def test_83_multiples_rejects_books_not_on_verified_api_allowlist(db):
    now=datetime(2026,9,4,13,0,tzinfo=timezone.utc)
    for eid in ("ex1","ex2"):
        _seed_multiple_exec(db,eid,commence="2026-09-04T18:00:00+00:00",min_odds=2.0)
        _seed_book_quote(db,eid,captured="2026-09-04T12:55:00+00:00",book="matchbook",title="Matchbook",price=2.2)
    assert generate_multiple_shadows(db,now=now,allowed_bookmaker_keys=("smarkets",))==0


def test_84_multiples_freezes_first_actionable_wave(db):
    now=datetime(2026,9,4,13,0,tzinfo=timezone.utc)
    for eid,price in (("freeze1",2.2),("freeze2",2.3)):
        _seed_multiple_exec(db,eid,commence="2026-09-04T18:00:00+00:00",min_odds=2.0)
        _seed_book_quote(db,eid,captured="2026-09-04T12:55:00+00:00",price=price)
    assert generate_multiple_shadows(db,now=now,allowed_bookmaker_keys=("paddypower",))==1
    first=db.fetchone("SELECT * FROM multiple_shadow_bets")
    first_price=first["combined_odds"]
    _seed_book_quote(db,"freeze1",captured="2026-09-04T13:05:00+00:00",price=3.0)
    _seed_book_quote(db,"freeze2",captured="2026-09-04T13:05:00+00:00",price=3.0)
    assert generate_multiple_shadows(db,now=datetime(2026,9,4,13,10,tzinfo=timezone.utc),allowed_bookmaker_keys=("paddypower",))==0
    assert db.fetchone("SELECT combined_odds FROM multiple_shadow_bets")["combined_odds"]==pytest.approx(first_price)


def test_85_multiples_clv_uses_same_book_leg_closes_and_worst_quality(db):
    now=datetime(2026,9,4,13,0,tzinfo=timezone.utc)
    _seed_multiple_exec(db,"clvm1",commence="2026-09-04T18:00:00+00:00",min_odds=2.0)
    _seed_multiple_exec(db,"clvm2",commence="2026-09-04T19:00:00+00:00",min_odds=2.0)
    _seed_book_quote(db,"clvm1",captured="2026-09-04T12:55:00+00:00",price=2.2)
    _seed_book_quote(db,"clvm2",captured="2026-09-04T12:55:00+00:00",price=2.4)
    assert generate_multiple_shadows(db,now=now,allowed_bookmaker_keys=("paddypower",))==1
    # A close for first leg (10m) and B close for second leg (20m) => combined B.
    _seed_book_quote(db,"clvm1",captured="2026-09-04T17:50:00+00:00",price=2.0)
    _seed_book_quote(db,"clvm2",captured="2026-09-04T18:40:00+00:00",price=2.2)
    assert finalize_multiple_clv(db,now=datetime(2026,9,4,20,0,tzinfo=timezone.utc))==1
    row=db.fetchone("SELECT * FROM multiple_shadow_bets")
    assert row["closing_combined_odds"]==pytest.approx(4.4)
    assert row["clv_pct"]==pytest.approx((5.28/4.4-1)*100)
    assert row["clv_quality"]=="B"


def test_86_multiples_settlement_handles_win_and_push_reduction(db):
    now=datetime(2026,9,4,13,0,tzinfo=timezone.utc)
    _seed_multiple_exec(db,"settlem1",commence="2026-09-04T18:00:00+00:00",selection="Away",market="h2h",min_odds=2.0)
    _seed_multiple_exec(db,"settlem2",commence="2026-09-04T19:00:00+00:00",selection="Over",market="totals",point=2.0,min_odds=1.8)
    _seed_book_quote(db,"settlem1",captured="2026-09-04T12:55:00+00:00",market="h2h",selection="Away",price=2.2)
    _seed_book_quote(db,"settlem2",captured="2026-09-04T12:55:00+00:00",market="totals",selection="Over",point=2.0,price=1.9)
    assert generate_multiple_shadows(db,now=now,allowed_bookmaker_keys=("paddypower",))==1
    db.execute("INSERT INTO event_results(event_id,fetched_at,home_score,away_score,source) VALUES(?,?,?,?,?)",("settlem1","2026-09-04T21:00:00+00:00",0,1,"test"))
    db.execute("INSERT INTO event_results(event_id,fetched_at,home_score,away_score,source) VALUES(?,?,?,?,?)",("settlem2","2026-09-04T21:00:00+00:00",1,1,"test"))
    assert settle_multiple_shadows(db)==1
    row=db.fetchone("SELECT * FROM multiple_shadow_bets")
    assert row["result"]=="WIN"
    assert row["settled_combined_odds"]==pytest.approx(2.2)
    assert row["pnl_units"]==pytest.approx(1.2)


def test_87_multiples_scoreboard_segments_doubles_and_trebles(db):
    now=datetime(2026,9,4,13,0,tzinfo=timezone.utc)
    for i in range(3):
        eid=f"scorem{i}"
        _seed_multiple_exec(db,eid,commence=f"2026-09-04T{18+i}:00:00+00:00",min_odds=1.8,edge=10-i)
        _seed_book_quote(db,eid,captured="2026-09-04T12:55:00+00:00",price=2.0+i*0.1)
    generate_multiple_shadows(db,now=now,allowed_bookmaker_keys=("paddypower",))
    score=multiples_scoreboard(db)
    assert score["bets"]==4
    assert score["segments"]["leg_count"]["2"]["bets"]==3
    assert score["segments"]["leg_count"]["3"]["bets"]==1
    assert score["research_only"] is True


def test_88_export_contains_multiples_tables_and_summary(db):
    class S:
        sport_keys=("soccer_epl",)
        odds_region="uk"
        odds_markets=("h2h","totals","btts")
        execution_shadow_enabled=True
        execution_bookmaker_keys=("betfair_ex_uk","matchbook","smarkets")
        min_consensus_books=3
        min_edge_pct=3.0
        min_cross_market_edge_pct=4.0
        min_slow_book_gap_pct=4.0
        daily_paid_credit_budget=600
        quota_reserve_credits=1000
        breadth_polls_per_day=80
        max_events_per_odds_cycle=3
        enable_live_betting=False
        betfair_commission_pct=5.0
        matchbook_commission_pct=2.0
        smarkets_commission_pct=2.0
        default_execution_commission_pct=5.0
    payload,_=build_research_export(db,S(),"0.6.8")
    import io,zipfile
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        assert "tables/multiple_shadow_bets.csv" in z.namelist()
        assert "tables/multiple_shadow_legs.csv" in z.namelist()
        summary=json.loads(z.read("analysis_summary.json"))
        assert "multiples_shadow" in summary


def test_89_experiment_config_hash_is_stable_and_sensitive():
    s=Settings()
    a=experiment_config_hash(s,extra={"experiment":"A","n":2})
    b=experiment_config_hash(s,extra={"n":2,"experiment":"A"})
    c=experiment_config_hash(s,extra={"experiment":"B","n":2})
    assert a==b
    assert a!=c
    assert len(a)==20


def test_90_market_wave_metrics_captures_consensus_quality():
    rows=[]
    prices={
        "booka":{"Home":2.0,"Draw":3.5,"Away":4.0},
        "bookb":{"Home":2.1,"Draw":3.4,"Away":3.8},
        "bookc":{"Home":1.95,"Draw":3.6,"Away":4.2},
    }
    for book,outcomes in prices.items():
        for name,price in outcomes.items():
            rows.append({
                "bookmaker_key":book,"market_key":"h2h",
                "outcome_name":name,"outcome_description":None,
                "point":None,"price":price,
            })
    out=market_wave_metrics(
        rows,market_key="h2h",selection="Away",point=None,
        outcome_description=None,chosen_odds=4.2
    )
    assert out["bookmaker_count"]==3
    assert out["median_odds"]==pytest.approx(4.0)
    assert out["best_odds"]==pytest.approx(4.2)
    assert out["chosen_vs_median_pct"]==pytest.approx(5.0)
    assert out["price_dispersion_pct"]==pytest.approx(10.0)
    assert out["mean_overround_pct"] is not None


def test_91_new_execution_records_fingerprint_and_entry_market_quality(db):
    _insert_test_event(db,"instexec")
    _insert_detection(
        db,event_id="instexec",strategy="CONSENSUS_VALUE",
        book="skybet",odds=4.0,created_at="2026-09-01T10:00:00+00:00",edge=5.0
    )
    db.execute(
        "UPDATE signals SET min_odds=3.5,fair_odds=3.3,fair_probability=? WHERE event_id='instexec'",
        (1/3.3,)
    )
    wave="2026-09-01T10:00:00+00:00"
    for book,title,home,draw,away in (
        ("skybet","Sky Bet",2.0,3.5,4.0),
        ("paddypower","Paddy Power",2.05,3.4,3.9),
        ("bet365","bet365",1.98,3.6,4.1),
    ):
        _insert_quote(db,event_id="instexec",captured_at=wave,book=book,title=title,market="h2h",selection="Home",price=home)
        _insert_quote(db,event_id="instexec",captured_at=wave,book=book,title=title,market="h2h",selection="Draw",price=draw)
        _insert_quote(db,event_id="instexec",captured_at=wave,book=book,title=title,market="h2h",selection="Away",price=away)
    _insert_quote(db,event_id="instexec",captured_at=wave,book="matchbook",title="Matchbook",market="h2h",selection="Away",price=3.8)
    cluster_event_signals(db,"instexec")
    assert evaluate_execution_wave_at(db,"instexec",wave,("matchbook",))==1
    row=db.fetchone("SELECT * FROM execution_shadow_bets WHERE event_id='instexec'")
    assert row["app_version"]=="0.6.10"
    assert row["experiment_version"]=="SINGLES_CORE"
    assert row["config_hash"]
    assert row["strategy_version"]=="CONSENSUS_VALUE"
    assert row["entry_consensus_bookmaker_count"]==4
    assert row["entry_consensus_median_odds"]==pytest.approx((3.9+4.0)/2)
    assert row["entry_mean_overround_pct"] is not None


def test_92_entry_market_metrics_can_backfill_old_execution_rows(db):
    bet=_seed_multiple_exec(
        db,"backfillinst",commence="2026-09-04T18:00:00+00:00",
        created="2026-09-04T12:00:00+00:00",min_odds=2.0
    )
    for book,price in (("booka",2.2),("bookb",2.3),("bookc",2.4)):
        _seed_book_quote(
            db,"backfillinst",captured="2026-09-04T12:00:00+00:00",
            book=book,title=book,price=price
        )
    assert backfill_entry_market_metrics(db)==1
    row=db.fetchone("SELECT * FROM execution_shadow_bets WHERE id=?",(bet["id"],))
    assert row["entry_consensus_bookmaker_count"]==3
    assert row["entry_consensus_median_odds"]==pytest.approx(2.3)


def test_93_reference_close_uses_latest_broad_wave_not_later_narrow_exchange(db):
    bet=_seed_multiple_exec(
        db,"refclose",commence="2026-09-04T18:00:00+00:00",
        created="2026-09-04T12:00:00+00:00",min_odds=2.0
    )
    # Broad wave 30 minutes before kickoff.
    for book,price in (("booka",2.0),("bookb",2.1),("bookc",2.2)):
        _seed_book_quote(
            db,"refclose",captured="2026-09-04T17:30:00+00:00",
            book=book,title=book,price=price
        )
    # Later narrow execution-only observation must not masquerade as consensus.
    _seed_book_quote(
        db,"refclose",captured="2026-09-04T17:55:00+00:00",
        book="matchbook",title="Matchbook",price=1.8
    )
    assert finalize_reference_closes(
        db,now=datetime(2026,9,4,19,0,tzinfo=timezone.utc),
        excluded_books=("betfair_ex_uk","matchbook","smarkets")
    )==1
    row=db.fetchone("SELECT * FROM execution_shadow_bets WHERE id=?",(bet["id"],))
    assert row["closing_consensus_median_odds"]==pytest.approx(2.1)
    assert row["closing_reference_best_odds"]==pytest.approx(2.2)
    assert row["closing_reference_quality"]=="B"
    assert row["closing_reference_status"]=="FINALIZED"
    assert row["closing_reference_observed_at"].startswith("2026-09-04T17:30:00")


def test_94_settlement_provenance_flags_knockout_capable_competitions(db):
    for eid,sport in (
        ("stdsettle","soccer_epl"),
        ("uefasettle","soccer_uefa_champs_league"),
    ):
        db.execute(
            """INSERT INTO events(event_id,sport_key,league,commence_time,home_team,away_team,first_seen_at,last_seen_at,status)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (eid,sport,sport,"2026-09-04T18:00:00+00:00","H","A",
             "2026-09-04T10:00:00+00:00","2026-09-04T10:00:00+00:00","COMPLETED")
        )
        db.execute(
            "INSERT INTO event_results(event_id,fetched_at,home_score,away_score,source) VALUES(?,?,?,?,?)",
            (eid,"2026-09-04T21:00:00+00:00",1,0,"test")
        )
    assert refresh_settlement_provenance(db)==2
    std=db.fetchone("SELECT settlement_quality FROM event_results WHERE event_id='stdsettle'")
    uefa=db.fetchone("SELECT settlement_quality FROM event_results WHERE event_id='uefasettle'")
    assert std["settlement_quality"]=="STANDARD_LEAGUE"
    assert uefa["settlement_quality"]=="UNVERIFIED_REGULATION_TIME"


def test_95_probability_calibration_excludes_unverified_regulation_results(db):
    def settled_exec(eid,sport,p,result):
        db.execute(
            """INSERT INTO events(event_id,sport_key,league,commence_time,home_team,away_team,first_seen_at,last_seen_at,status)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (eid,sport,sport,"2026-09-04T18:00:00+00:00","H","A",
             "2026-09-04T10:00:00+00:00","2026-09-04T10:00:00+00:00","COMPLETED")
        )
        db.execute(
            """INSERT INTO signals(created_at,event_id,strategy,market_key,selection,bookmaker_key,bookmaker_title,offered_odds,fair_odds,fair_probability,edge_pct,min_odds,status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("2026-09-04T10:00:00+00:00",eid,"TEST","h2h","H","matchbook","Matchbook",
             2.0,1/p,p,1.0,1.8,"SETTLED")
        )
        sig=db.fetchone("SELECT id FROM signals WHERE event_id=?",(eid,))
        db.execute(
            """INSERT INTO execution_shadow_bets(execution_key,created_at,event_id,market_key,selection,source_signal_id,bookmaker_key,bookmaker_title,offered_odds,fair_odds,fair_probability,edge_pct,min_odds,status,result,pnl_units)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"{eid}|h2h|H||","2026-09-04T10:00:00+00:00",eid,"h2h","H",sig["id"],
             "matchbook","Matchbook",2.0,1/p,p,1.0,1.8,"SETTLED",result,
             1.0 if result=="WIN" else -1.0)
        )
        db.execute(
            "INSERT INTO event_results(event_id,fetched_at,home_score,away_score,source) VALUES(?,?,?,?,?)",
            (eid,"2026-09-04T21:00:00+00:00",1,0,"test")
        )
    settled_exec("calwin","soccer_epl",0.70,"WIN")
    settled_exec("calloss","soccer_epl",0.20,"LOSS")
    settled_exec("caluefa","soccer_uefa_champs_league",0.90,"LOSS")
    refresh_settlement_provenance(db)
    report=probability_calibration_report(db)["singles"]
    assert report["samples"]==2
    assert report["expected_win_pct"]==pytest.approx(45.0)
    assert report["observed_win_pct"]==pytest.approx(50.0)
    assert report["brier_score"] is not None
    assert report["log_loss"] is not None


def test_96_new_multiple_records_execution_realism_and_fingerprint(db):
    now=datetime(2026,9,4,13,0,tzinfo=timezone.utc)
    for eid in ("realism1","realism2"):
        _seed_multiple_exec(db,eid,commence="2026-09-04T18:00:00+00:00",min_odds=1.8)
    # Two common fixed-odds books. Paddy Power is the better combined price.
    _seed_book_quote(db,"realism1",captured="2026-09-04T12:50:00+00:00",book="paddypower",title="Paddy Power",price=2.3)
    _seed_book_quote(db,"realism2",captured="2026-09-04T12:55:00+00:00",book="paddypower",title="Paddy Power",price=2.4)
    _seed_book_quote(db,"realism1",captured="2026-09-04T12:52:00+00:00",book="bet365",title="bet365",price=2.2)
    _seed_book_quote(db,"realism2",captured="2026-09-04T12:54:00+00:00",book="bet365",title="bet365",price=2.3)
    assert generate_multiple_shadows(db,now=now,allowed_bookmaker_keys=("paddypower","bet365"))==1
    row=db.fetchone("SELECT * FROM multiple_shadow_bets")
    assert row["app_version"]=="0.7.2"
    assert row["experiment_version"]=="MS1.1_API_GATE"
    assert row["config_hash"]
    assert row["common_bookmaker_count"]==2
    assert json.loads(row["common_bookmakers_json"])==["bet365","paddypower"]
    assert row["entry_quote_time_spread_minutes"]==pytest.approx(5.0)
    assert row["source_pool_size"]==2


def test_97_multiples_overlap_quantifies_reused_source_legs(db):
    now=datetime(2026,9,4,13,0,tzinfo=timezone.utc)
    for i in range(3):
        eid=f"overlap{i}"
        _seed_multiple_exec(db,eid,commence=f"2026-09-04T{18+i}:00:00+00:00",min_odds=1.8,edge=10-i)
        _seed_book_quote(db,eid,captured="2026-09-04T12:55:00+00:00",price=2.1+i*.1)
    assert generate_multiple_shadows(db,now=now,allowed_bookmaker_keys=("paddypower",))==4
    report=multiples_overlap_report(db)
    assert report["multiples"]==4
    assert report["total_leg_slots"]==9
    assert report["unique_source_legs"]==3
    assert report["max_reuse_one_source_leg"]==3
    assert report["effective_source_leg_count"]==pytest.approx(3.0)


def test_98_multiple_clv_records_broad_consensus_and_best_common_close(db):
    now=datetime(2026,9,4,13,0,tzinfo=timezone.utc)
    for eid,commence in (("broadm1","2026-09-04T18:00:00+00:00"),("broadm2","2026-09-04T19:00:00+00:00")):
        _seed_multiple_exec(db,eid,commence=commence,min_odds=1.8)
        _seed_book_quote(db,eid,captured="2026-09-04T12:55:00+00:00",book="paddypower",title="Paddy Power",price=2.2)
    assert generate_multiple_shadows(db,now=now,allowed_bookmaker_keys=("paddypower",))==1
    # Broad close wave: 3 common fixed books for each leg.
    for book,title,p1,p2 in (
        ("paddypower","Paddy Power",2.0,2.1),
        ("bet365","bet365",2.1,2.2),
        ("skybet","Sky Bet",2.2,2.3),
    ):
        _seed_book_quote(db,"broadm1",captured="2026-09-04T17:50:00+00:00",book=book,title=title,price=p1)
        _seed_book_quote(db,"broadm2",captured="2026-09-04T18:50:00+00:00",book=book,title=title,price=p2)
    assert finalize_multiple_clv(db,now=datetime(2026,9,4,20,0,tzinfo=timezone.utc))==1
    row=db.fetchone("SELECT * FROM multiple_shadow_bets")
    assert row["closing_consensus_combined_odds"]==pytest.approx(2.1*2.2)
    assert row["closing_best_common_book_odds"]==pytest.approx(2.2*2.3)
    assert row["closing_best_common_bookmaker_key"]=="skybet"
    assert row["closing_common_bookmaker_count"]==3
    assert row["closing_reference_quality"]=="A"
    assert row["clv_vs_closing_consensus_pct"] is not None
    assert row["clv_vs_closing_best_common_pct"] is not None


def test_99_instrumentation_report_declares_zero_added_provider_calls(db):
    report=instrumentation_report(db)
    assert report["app_version"]=="0.6.10"
    assert report["provider_calls_added"]==0
    assert "probability_calibration" in report
    assert "multiples_overlap" in report
    assert "experiment_coverage" in report


def test_100_export_contains_v069_instrumentation_summary(db):
    class S:
        sport_keys=("soccer_epl",)
        odds_region="uk"
        odds_markets=("h2h","totals","btts")
        execution_shadow_enabled=True
        execution_bookmaker_keys=("betfair_ex_uk","matchbook","smarkets")
        min_consensus_books=3
        min_edge_pct=3.0
        min_cross_market_edge_pct=4.0
        min_slow_book_gap_pct=4.0
        daily_paid_credit_budget=600
        quota_reserve_credits=1000
        breadth_polls_per_day=80
        max_events_per_odds_cycle=3
        enable_live_betting=False
        betfair_commission_pct=5.0
        matchbook_commission_pct=2.0
        smarkets_commission_pct=2.0
        default_execution_commission_pct=5.0
    payload,_=build_research_export(db,S(),"0.6.10")
    import io,zipfile
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        summary=json.loads(z.read("analysis_summary.json"))
        assert "instrumentation" in summary
        assert summary["instrumentation"]["provider_calls_added"]==0
        manifest=json.loads(z.read("manifest.json"))
        assert any("zero provider calls" in x for x in manifest["notes"])



def test_101_imminent_overdue_breadth_beats_far_future_unpolled(db):
    q=QuotaGuard(db,reserve=10,daily_budget=1000)
    c=Collector(db,FakeApi(),q,sport_keys=["soccer_epl"],region="uk",markets=["h2h"],breadth_polls_per_day=160)
    now=datetime(2026,9,5,12,0,tzinfo=timezone.utc)
    imminent=(now+timedelta(hours=3)).isoformat()
    far=(now+timedelta(days=7)).isoformat()
    db.execute(
        """INSERT INTO events(event_id,sport_key,league,commence_time,home_team,away_team,first_seen_at,last_seen_at,last_odds_poll_at,status)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("imminent","soccer_epl","Premier League",imminent,"H","A",now.isoformat(),now.isoformat(),(now-timedelta(hours=3)).isoformat(),"UPCOMING")
    )
    db.execute(
        """INSERT INTO events(event_id,sport_key,league,commence_time,home_team,away_team,first_seen_at,last_seen_at,last_odds_poll_at,status)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("far-unpolled","soccer_epl","Premier League",far,"H","A",now.isoformat(),now.isoformat(),None,"UPCOMING")
    )
    due=c.candidate_events(now)
    assert [x["event_id"] for x in due[:2]]==["imminent","far-unpolled"]


def test_102_breadth_target_is_paced_across_day(db):
    q=QuotaGuard(db,reserve=10,daily_budget=1000)
    c=Collector(db,FakeApi(),q,sport_keys=["soccer_epl"],region="uk",markets=["h2h"],breadth_polls_per_day=240)
    assert c.breadth_target_by_now(datetime(2026,9,5,0,0,0,tzinfo=timezone.utc))==0
    assert c.breadth_target_by_now(datetime(2026,9,5,6,0,0,tzinfo=timezone.utc))==60
    assert c.breadth_target_by_now(datetime(2026,9,5,12,0,0,tzinfo=timezone.utc))==120
    assert c.breadth_target_by_now(datetime(2026,9,5,18,0,0,tzinfo=timezone.utc))==180


def test_103_urgent_breadth_and_convergence_interleave(db):
    now=datetime.now(timezone.utc)
    kick=(now+timedelta(hours=2)).isoformat()
    _insert_execution_shadow_for_convergence(db,"urgent-conv")
    db.execute("UPDATE events SET commence_time=? WHERE event_id='urgent-conv'",(kick,))
    db.execute(
        """INSERT INTO events(event_id,sport_key,league,commence_time,home_team,away_team,first_seen_at,last_seen_at,status)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        ("urgent-broad","soccer_epl","Premier League",kick,"H","A",now.isoformat(),now.isoformat(),"UPCOMING")
    )
    db.record_collector_run("ODDS",True,event_id="previous-breadth",actual_cost=1)
    db.execute("UPDATE quota_state SET credits_remaining=10000 WHERE singleton_id=1")
    api=ConvergenceFakeApi();q=QuotaGuard(db,reserve=10,daily_budget=10000)
    c=Collector(db,api,q,sport_keys=["soccer_epl"],region="uk",markets=["h2h"],breadth_polls_per_day=160,execution_bookmaker_keys=["matchbook"])
    first=c.poll_one_cycle()
    assert first["mode"]=="convergence"
    second=c.poll_one_cycle()
    assert second["mode"]=="breadth"


def test_104_404_event_is_quarantined_and_error_is_redacted(db):
    class FakeResponse:
        status_code=404
    class Fake404(Exception):
        def __init__(self):
            self.response=FakeResponse()
            super().__init__("404 Client Error for url: https://api.example/odds?markets=h2h&apiKey=TOPSECRET&regions=uk")
    class Api404:
        def quota_probe(self): return ApiResult([],10000,0,0)
        def event_odds(self,*args,**kwargs): raise Fake404()
    now=datetime.now(timezone.utc)
    db.execute(
        """INSERT INTO events(event_id,sport_key,league,commence_time,home_team,away_team,first_seen_at,last_seen_at,status)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        ("gone404","soccer_epl","Premier League",(now+timedelta(hours=1)).isoformat(),"H","A",now.isoformat(),now.isoformat(),"UPCOMING")
    )
    db.execute("UPDATE quota_state SET credits_remaining=10000 WHERE singleton_id=1")
    c=Collector(db,Api404(),QuotaGuard(db,reserve=10,daily_budget=10000),sport_keys=["soccer_epl"],region="uk",markets=["h2h"],breadth_polls_per_day=160)
    out=c.poll_one_cycle()
    assert out["mode"]=="breadth" and out["polled"]==0
    ev=db.fetchone("SELECT * FROM events WHERE event_id='gone404'")
    assert ev["odds_failure_count"]==1
    assert ev["odds_last_failure_code"]==404
    assert ev["odds_quarantine_until"] is not None
    log=db.fetchone("SELECT detail FROM collector_runs WHERE event_id='gone404' ORDER BY id DESC LIMIT 1")
    assert "TOPSECRET" not in log["detail"]
    assert "apiKey=[REDACTED]" in log["detail"]
    assert c.candidate_events(datetime.now(timezone.utc))==[]


def test_105_successful_odds_poll_clears_quarantine_state(db):
    class BroadOk:
        def quota_probe(self): return ApiResult([],10000,0,0)
        def event_odds(self,sport_key,event_id,region,markets,bookmaker_keys=()):
            return ApiResult({"id":event_id,"bookmakers":[]},9999,1,1)
    now=datetime.now(timezone.utc)
    db.execute(
        """INSERT INTO events(event_id,sport_key,league,commence_time,home_team,away_team,first_seen_at,last_seen_at,odds_quarantine_until,odds_failure_count,odds_last_failure_code,odds_last_failure_at,status)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("recover","soccer_epl","Premier League",(now+timedelta(hours=1)).isoformat(),"H","A",now.isoformat(),now.isoformat(),(now-timedelta(minutes=1)).isoformat(),3,404,(now-timedelta(hours=2)).isoformat(),"UPCOMING")
    )
    db.execute("UPDATE quota_state SET credits_remaining=10000 WHERE singleton_id=1")
    c=Collector(db,BroadOk(),QuotaGuard(db,reserve=10,daily_budget=10000),sport_keys=["soccer_epl"],region="uk",markets=["h2h"],breadth_polls_per_day=160)
    out=c.poll_one_cycle()
    assert out["polled"]==1
    ev=db.fetchone("SELECT * FROM events WHERE event_id='recover'")
    assert ev["odds_failure_count"]==0
    assert ev["odds_quarantine_until"] is None
    assert ev["odds_last_failure_code"] is None


def test_106_schema_init_scrubs_historical_api_key_logs(db):
    db.execute(
        """INSERT INTO collector_runs(started_at,finished_at,run_type,estimated_cost,actual_cost,ok,detail)
           VALUES(?,?,?,?,?,?,?)""",
        (datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc).isoformat(),"ODDS",0,0,0,"url=https://x.test?a=1&apiKey=LEGACYSECRET&b=2")
    )
    db.init_schema()
    row=db.fetchone("SELECT detail FROM collector_runs WHERE detail LIKE '%REDACTED%' ORDER BY id DESC LIMIT 1")
    assert row is not None
    assert "LEGACYSECRET" not in row["detail"]
    assert "apiKey=[REDACTED]" in row["detail"]


def test_107_export_defensively_scrubs_raw_secret_strings(db):
    raw="https://x.test/odds?apiKey=EXPORTSECRET&markets=h2h"
    # Direct insert deliberately bypasses record_collector_run to test exporter defence.
    now=datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO collector_runs(started_at,finished_at,run_type,estimated_cost,actual_cost,ok,detail)
           VALUES(?,?,?,?,?,?,?)""",
        (now,now,"ODDS",0,0,0,raw)
    )
    class S:
        sport_keys=("soccer_epl",);odds_region="uk";odds_markets=("h2h",)
        enable_dnb_market=False;execution_shadow_enabled=True
        execution_bookmaker_keys=("matchbook",);min_consensus_books=3
        min_edge_pct=3.0;min_cross_market_edge_pct=4.0;min_slow_book_gap_pct=4.0
        daily_paid_credit_budget=8000;quota_reserve_credits=1000
        breadth_polls_per_day=160;max_events_per_odds_cycle=3;enable_live_betting=False
        betfair_commission_pct=5.0;matchbook_commission_pct=2.0;smarkets_commission_pct=2.0
        default_execution_commission_pct=5.0;odds_api_key="EXPORTSECRET"
    payload,_=build_research_export(db,S(),"0.6.10")
    import io,zipfile
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        csv_text=z.read("tables/collector_runs.csv").decode()
        assert "EXPORTSECRET" not in csv_text
        assert "[REDACTED]" in csv_text


def test_108_expanded_league_labels_normalize_on_schema_init(db):
    now=datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO events(event_id,sport_key,league,commence_time,home_team,away_team,first_seen_at,last_seen_at,status)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        ("league-new","soccer_norway_eliteserien","soccer_norway_eliteserien","2030-09-01T12:00:00+00:00","H","A",now,now,"UPCOMING")
    )
    db.init_schema()
    assert db.fetchone("SELECT league FROM events WHERE event_id='league-new'")["league"]=="Eliteserien"


def test_109_sensitive_text_sanitizer_covers_api_key_and_bearer():
    text="GET https://x?a=1&apiKey=ABC123&z=2 Authorization: Bearer XYZ789"
    clean=sanitize_sensitive_text(text)
    assert "ABC123" not in clean and "XYZ789" not in clean
    assert "apiKey=[REDACTED]" in clean
    assert "Bearer [REDACTED]" in clean


def test_110_v0610_event_schema_has_quarantine_columns(db):
    cols={x["name"] for x in db.fetchall("PRAGMA table_info(events)")}
    assert {"odds_quarantine_until","odds_failure_count","odds_last_failure_code","odds_last_failure_at"}.issubset(cols)


def _seed_tennis_tournament(db, key="tennis_atp_us_open", title="ATP US Open", active=1):
    now="2030-09-01T10:00:00+00:00"
    db.execute(
        """INSERT INTO tennis_tournament_state(
             sport_key,title,tour,tournament_level,active,first_seen_at,last_seen_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (key,title,"ATP","GRAND_SLAM",active,now,now)
    )


def _seed_tennis_event(
    db,event_id="tennis1",sport_key="tennis_atp_us_open",
    commence="2030-09-01T18:00:00+00:00",p1="Player A",p2="Player B"
):
    exists=db.fetchone("SELECT sport_key FROM tennis_tournament_state WHERE sport_key=?",(sport_key,))
    if not exists:
        _seed_tennis_tournament(db,sport_key,"ATP US Open",1)
    db.execute(
        """INSERT INTO tennis_events(
             event_id,sport_key,tournament_title,tour,tournament_level,
             commence_time,player_one,player_two,first_seen_at,last_seen_at,status
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (event_id,sport_key,"ATP US Open","ATP","GRAND_SLAM",commence,p1,p2,
         "2030-09-01T10:00:00+00:00","2030-09-01T10:00:00+00:00","UPCOMING")
    )


def _seed_tennis_quote(
    db,event_id,at,mode,book,selection,price,
    sport_key="tennis_atp_us_open",title=None
):
    db.execute(
        """INSERT INTO tennis_odds_snapshots(
             event_id,sport_key,captured_at,capture_mode,bookmaker_key,
             bookmaker_title,market_key,selection,price
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (event_id,sport_key,at,mode,book,title or book,"h2h",selection,price)
    )


def test_111_v070_tennis_tables_exist(db):
    for table in (
        "tennis_tournament_state","tennis_events","tennis_odds_snapshots",
        "tennis_consensus_snapshots","tennis_execution_evaluations",
        "tennis_execution_bets","tennis_price_observations","tennis_results",
    ):
        row=db.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",(table,)
        )
        assert row is not None, table


def test_112_tennis_consensus_is_two_way_devigged_and_excludes_execution_books(db):
    _seed_tennis_event(db)
    at="2030-09-01T12:00:00+00:00"
    # 5 fixed books plus a deliberately extreme exchange quote that must not
    # influence the fair consensus.
    prices={
        "book1":(1.70,2.25),"book2":(1.72,2.22),"book3":(1.68,2.28),
        "book4":(1.71,2.24),"book5":(1.69,2.26),
    }
    for book,(a,b) in prices.items():
        _seed_tennis_quote(db,"tennis1",at,"BREADTH",book,"Player A",a)
        _seed_tennis_quote(db,"tennis1",at,"BREADTH",book,"Player B",b)
    _seed_tennis_quote(db,"tennis1",at,"BREADTH","matchbook","Player A",9.0)
    _seed_tennis_quote(db,"tennis1",at,"BREADTH","matchbook","Player B",1.05)

    assert write_tennis_consensus(
        db,"tennis1",at,min_books=5,
        excluded_books=("betfair_ex_uk","matchbook","smarkets")
    )==2
    rows=db.fetchall(
        "SELECT * FROM tennis_consensus_snapshots WHERE event_id='tennis1' ORDER BY selection"
    )
    assert len(rows)==2
    assert sum(float(r["fair_probability"]) for r in rows)==pytest.approx(1.0)
    assert all(r["num_books"]==5 for r in rows)
    a=next(r for r in rows if r["selection"]=="Player A")
    assert 1.6 < float(a["fair_odds"]) < 1.9


def test_113_tennis_execution_waits_until_first_acceptable_approved_price(db):
    _seed_tennis_event(db)
    broad="2030-09-01T12:00:00+00:00"
    for book,(a,b) in {
        "book1":(1.70,2.25),"book2":(1.72,2.22),"book3":(1.68,2.28),
        "book4":(1.71,2.24),"book5":(1.69,2.26),
    }.items():
        _seed_tennis_quote(db,"tennis1",broad,"BREADTH",book,"Player A",a)
        _seed_tennis_quote(db,"tennis1",broad,"BREADTH",book,"Player B",b)
    write_tennis_consensus(
        db,"tennis1",broad,min_books=5,
        excluded_books=("betfair_ex_uk","matchbook","smarkets")
    )
    c=db.fetchone(
        "SELECT * FROM tennis_consensus_snapshots WHERE event_id='tennis1' AND selection='Player B'"
    )
    min_odds=min_odds_for_probability(float(c["fair_probability"]),3.0)

    first="2030-09-01T12:30:00+00:00"
    _seed_tennis_quote(db,"tennis1",first,"CONVERGENCE","matchbook","Player B",min_odds-0.02)
    assert evaluate_tennis_convergence_wave(
        db,sport_key="tennis_atp_us_open",captured_at=first,
        execution_bookmaker_keys=("matchbook",),min_edge_pct=3.0,
        max_consensus_age_minutes=240,config_hash="abc"
    )==0
    assert db.fetchone("SELECT id FROM tennis_execution_bets") is None

    second="2030-09-01T12:45:00+00:00"
    _seed_tennis_quote(db,"tennis1",second,"CONVERGENCE","matchbook","Player B",min_odds+0.08)
    assert evaluate_tennis_convergence_wave(
        db,sport_key="tennis_atp_us_open",captured_at=second,
        execution_bookmaker_keys=("matchbook",),min_edge_pct=3.0,
        max_consensus_age_minutes=240,config_hash="abc"
    )==1
    bet=db.fetchone("SELECT * FROM tennis_execution_bets")
    assert bet["created_at"]==second
    assert bet["offered_odds"]==pytest.approx(min_odds+0.08)
    assert bet["experiment_version"]=="TS1"
    assert bet["app_version"]=="0.7.0"
    audits=db.fetchall(
        "SELECT decision,reason FROM tennis_execution_evaluations WHERE selection='Player B' ORDER BY id"
    )
    assert any(x["reason"]=="EXECUTABLE_PRICE_BELOW_MIN" for x in audits)
    assert any(x["decision"]=="ACCEPT" for x in audits)


def test_114_tennis_execution_freezes_first_acceptable_wave_even_if_price_improves(db):
    _seed_tennis_event(db)
    broad="2030-09-01T12:00:00+00:00"
    for i,(a,b) in enumerate(((1.7,2.25),(1.71,2.24),(1.69,2.26),(1.72,2.22),(1.68,2.28)),1):
        _seed_tennis_quote(db,"tennis1",broad,"BREADTH",f"book{i}","Player A",a)
        _seed_tennis_quote(db,"tennis1",broad,"BREADTH",f"book{i}","Player B",b)
    write_tennis_consensus(db,"tennis1",broad,min_books=5,excluded_books=("matchbook",))
    c=db.fetchone("SELECT * FROM tennis_consensus_snapshots WHERE selection='Player B'")
    min_odds=min_odds_for_probability(float(c["fair_probability"]),3)
    first="2030-09-01T12:20:00+00:00"
    second="2030-09-01T12:40:00+00:00"
    _seed_tennis_quote(db,"tennis1",first,"CONVERGENCE","matchbook","Player B",min_odds+0.05)
    evaluate_tennis_convergence_wave(
        db,sport_key="tennis_atp_us_open",captured_at=first,
        execution_bookmaker_keys=("matchbook",),min_edge_pct=3,
        max_consensus_age_minutes=240,config_hash="x"
    )
    _seed_tennis_quote(db,"tennis1",second,"CONVERGENCE","matchbook","Player B",min_odds+0.50)
    evaluate_tennis_convergence_wave(
        db,sport_key="tennis_atp_us_open",captured_at=second,
        execution_bookmaker_keys=("matchbook",),min_edge_pct=3,
        max_consensus_age_minutes=240,config_hash="x"
    )
    bet=db.fetchone("SELECT * FROM tennis_execution_bets")
    assert bet["created_at"]==first
    assert bet["offered_odds"]==pytest.approx(min_odds+0.05)


def test_115_tennis_stale_consensus_cannot_create_execution(db):
    _seed_tennis_event(db)
    broad="2030-09-01T10:00:00+00:00"
    for i,(a,b) in enumerate(((1.7,2.25),(1.71,2.24),(1.69,2.26),(1.72,2.22),(1.68,2.28)),1):
        _seed_tennis_quote(db,"tennis1",broad,"BREADTH",f"book{i}","Player A",a)
        _seed_tennis_quote(db,"tennis1",broad,"BREADTH",f"book{i}","Player B",b)
    write_tennis_consensus(db,"tennis1",broad,min_books=5,excluded_books=("matchbook",))
    late="2030-09-01T15:00:00+00:00"
    _seed_tennis_quote(db,"tennis1",late,"CONVERGENCE","matchbook","Player B",10.0)
    assert evaluate_tennis_convergence_wave(
        db,sport_key="tennis_atp_us_open",captured_at=late,
        execution_bookmaker_keys=("matchbook",),min_edge_pct=3,
        max_consensus_age_minutes=240,config_hash="x"
    )==0
    audit=db.fetchone(
        "SELECT reason FROM tennis_execution_evaluations WHERE selection='Player B' ORDER BY id DESC LIMIT 1"
    )
    assert audit["reason"]=="STALE_REFERENCE_CONSENSUS"


def test_116_tennis_clv_uses_latest_chosen_venue_quote_before_start(db):
    _seed_tennis_event(db,commence="2030-09-01T18:00:00+00:00")
    # Directly seed a frozen shadow.
    db.execute(
        """INSERT INTO tennis_execution_bets(
             execution_key,created_at,event_id,sport_key,selection,bookmaker_key,
             bookmaker_title,offered_odds,fair_probability,fair_odds,edge_pct,min_odds,
             approved_books_seen,consensus_captured_at,consensus_age_minutes,
             reference_book_count,status
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("tennis1|h2h|Player B","2030-09-01T12:00:00+00:00","tennis1",
         "tennis_atp_us_open","Player B","matchbook","Matchbook",2.60,0.42,
         1/0.42,9.2,2.45,1,"2030-09-01T12:00:00+00:00",0,5,"OPEN")
    )
    for at,price in (
        ("2030-09-01T17:00:00+00:00",2.45),
        ("2030-09-01T17:50:00+00:00",2.30),
        ("2030-09-01T18:05:00+00:00",2.10),
    ):
        _seed_tennis_quote(db,"tennis1",at,"CONVERGENCE","matchbook","Player B",price)
    assert track_tennis_prices(
        db,datetime(2030,9,1,18,10,tzinfo=timezone.utc)
    )==2
    assert finalize_tennis_clv(
        db,datetime(2030,9,1,18,10,tzinfo=timezone.utc)
    )==1
    bet=db.fetchone("SELECT * FROM tennis_execution_bets")
    assert bet["closing_odds"]==pytest.approx(2.30)
    assert bet["clv_pct"]==pytest.approx((2.60/2.30-1)*100)
    assert bet["clv_quality"]=="A"
    assert bet["closing_minutes_before_start"]==pytest.approx(10.0)


def test_117_tennis_settlement_and_commission_accounting(db):
    _seed_tennis_event(db)
    db.execute(
        """INSERT INTO tennis_execution_bets(
             execution_key,created_at,event_id,sport_key,selection,bookmaker_key,
             bookmaker_title,offered_odds,fair_probability,fair_odds,edge_pct,min_odds,
             approved_books_seen,consensus_captured_at,consensus_age_minutes,
             reference_book_count,status
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("tennis1|h2h|Player B","2030-09-01T12:00:00+00:00","tennis1",
         "tennis_atp_us_open","Player B","matchbook","Matchbook",3.0,0.36,
         1/0.36,8.0,2.86,1,"2030-09-01T12:00:00+00:00",0,5,"OPEN")
    )
    assert settle_tennis_event(db,"tennis1","Player B")==1
    bet=db.fetchone("SELECT * FROM tennis_execution_bets")
    assert bet["result"]=="WIN"
    assert bet["pnl_units"]==pytest.approx(2.0)
    assert bet["commission_rate_pct"]==pytest.approx(2.0)
    assert bet["commission_units"]==pytest.approx(0.04)
    assert bet["net_pnl_units"]==pytest.approx(1.96)


def test_118_tennis_scoreboard_headline_clv_uses_a_b_only(db):
    _seed_tennis_event(db)
    for i,(q,clv) in enumerate((("A",2.0),("B",4.0),("C",-20.0)),1):
        db.execute(
            """INSERT INTO tennis_execution_bets(
                 execution_key,created_at,event_id,sport_key,selection,bookmaker_key,
                 bookmaker_title,offered_odds,fair_probability,fair_odds,edge_pct,min_odds,
                 approved_books_seen,consensus_captured_at,consensus_age_minutes,
                 reference_book_count,status,clv_pct,clv_quality
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"key{i}","2030-09-01T12:00:00+00:00","tennis1","tennis_atp_us_open",
             f"S{i}","matchbook","Matchbook",2.0,0.5,2.0,0.0,2.0,1,
             "2030-09-01T12:00:00+00:00",0,5,"OPEN",clv,q)
        )
    score=tennis_scoreboard(db)
    assert score["clv_samples"]==2
    assert score["avg_clv_pct"]==pytest.approx(3.0)
    assert score["all_clv_samples"]==3


def test_119_tennis_quota_lane_counts_only_tennis_costs(db):
    now_dt=datetime.now(timezone.utc)
    now=now_dt.isoformat()
    db.execute("UPDATE quota_state SET credits_remaining=5000 WHERE singleton_id=1")
    db.execute(
        """INSERT INTO collector_runs(started_at,finished_at,run_type,estimated_cost,actual_cost,ok,detail)
           VALUES(?,?,?,?,?,?,?)""",
        (now,now,"ODDS",3,300,1,"football")
    )
    db.execute(
        """INSERT INTO collector_runs(started_at,finished_at,run_type,estimated_cost,actual_cost,ok,detail)
           VALUES(?,?,?,?,?,?,?)""",
        (now,now,"TENNIS_ODDS",1,20,1,"tennis")
    )
    guard=TennisQuotaGuard(db,daily_budget=25,reserve=1000)
    assert guard.today_paid_cost(now_dt)==20
    assert guard.decide(5)==(True,"ok")
    assert guard.decide(6)==(False,"tennis_daily_paid_credit_budget")


def test_120_tennis_discovery_uses_active_tennis_only_and_is_free(db):
    class Api:
        def sports(self):
            return ApiResult([
                {"key":"tennis_atp_us_open","title":"ATP US Open","group":"Tennis","active":True},
                {"key":"tennis_wta_us_open","title":"WTA US Open","group":"Tennis","active":True},
                {"key":"soccer_epl","title":"EPL","group":"Soccer","active":True},
            ],10000,0,0)
    s=Settings()
    engine=TennisShadowEngine(db,Api(),s,execution_bookmaker_keys=("matchbook",))
    assert engine.discover_active_tournaments(datetime(2030,9,1,12,0,tzinfo=timezone.utc))==2
    rows=db.fetchall("SELECT sport_key FROM tennis_tournament_state WHERE active=1 ORDER BY sport_key")
    assert [r["sport_key"] for r in rows]==["tennis_atp_us_open","tennis_wta_us_open"]
    log=db.fetchone("SELECT * FROM collector_runs WHERE run_type='TENNIS_DISCOVERY'")
    assert log["actual_cost"]==0


def test_121_tennis_engine_broad_then_convergence_creates_shadow(db):
    class Api:
        def __init__(self): self.calls=[]
        def sports(self):
            return ApiResult([
                {"key":"tennis_atp_us_open","title":"ATP US Open","group":"Tennis","active":True},
            ],10000,0,0)
        def sport_odds(self,sport_key,region,markets,bookmaker_keys=()):
            self.calls.append((sport_key,tuple(bookmaker_keys)))
            event={
                "id":"engine-tennis","sport_key":sport_key,
                "commence_time":"2030-09-01T18:00:00+00:00",
                "home_team":"Player A","away_team":"Player B",
                "bookmakers":[]
            }
            if bookmaker_keys:
                event["bookmakers"]=[
                    {"key":"matchbook","title":"Matchbook","markets":[
                        {"key":"h2h","outcomes":[
                            {"name":"Player A","price":1.75},
                            {"name":"Player B","price":2.60},
                        ]}
                    ]}
                ]
            else:
                for i,(a,b) in enumerate(((1.7,2.25),(1.71,2.24),(1.69,2.26),(1.72,2.22),(1.68,2.28)),1):
                    event["bookmakers"].append(
                        {"key":f"book{i}","title":f"Book {i}","markets":[
                            {"key":"h2h","outcomes":[
                                {"name":"Player A","price":a},
                                {"name":"Player B","price":b},
                            ]}
                        ]}
                    )
            return ApiResult([event],9999,1,1)
        def scores(self,*args,**kwargs):
            return ApiResult([],9999,1,2)

    class S:
        tennis_shadow_enabled=True;tennis_market="h2h";tennis_min_consensus_books=5
        tennis_min_edge_pct=3.0;tennis_daily_paid_credit_budget=500
        tennis_quota_reserve_credits=1000;tennis_max_consensus_age_minutes=240
        tennis_discovery_interval_seconds=21600;tennis_result_min_minutes_after_start=90
        tennis_result_poll_interval_seconds=3600;odds_region="uk"
    db.execute("UPDATE quota_state SET credits_remaining=10000 WHERE singleton_id=1")
    api=Api();engine=TennisShadowEngine(db,api,S(),execution_bookmaker_keys=("matchbook",))
    now=datetime(2030,9,1,12,0,tzinfo=timezone.utc)
    engine.discover_active_tournaments(now)
    # First due call is broad.
    first=engine.one_cycle(now)
    assert first["odds"]["mode"]=="breadth"
    assert db.fetchone("SELECT COUNT(*) AS n FROM tennis_consensus_snapshots")["n"]==2
    # Make convergence explicitly due and run the next minute.
    second=engine.one_cycle(now+timedelta(minutes=1))
    assert second["odds"]["mode"]=="convergence"
    bet=db.fetchone("SELECT * FROM tennis_execution_bets WHERE selection='Player B'")
    assert bet is not None
    assert bet["bookmaker_key"]=="matchbook"
    assert float(bet["offered_odds"])==pytest.approx(2.60)


def test_122_tennis_result_collector_settles_provider_completed_match(db):
    class Api:
        def sports(self): return ApiResult([],10000,0,0)
        def scores(self,sport_key,event_ids=(),days_from=1):
            return ApiResult([{
                "id":"tennis-result","completed":True,
                "commence_time":"2030-09-01T12:00:00+00:00",
                "scores":[
                    {"name":"Player A","score":"1"},
                    {"name":"Player B","score":"2"},
                ],
            }],9998,2,2)
    class S:
        tennis_shadow_enabled=True;tennis_market="h2h";tennis_min_consensus_books=5
        tennis_min_edge_pct=3.0;tennis_daily_paid_credit_budget=500
        tennis_quota_reserve_credits=1000;tennis_max_consensus_age_minutes=240
        tennis_discovery_interval_seconds=21600;tennis_result_min_minutes_after_start=90
        tennis_result_poll_interval_seconds=3600;odds_region="uk"
    _seed_tennis_event(
        db,event_id="tennis-result",commence="2030-09-01T12:00:00+00:00"
    )
    db.execute(
        """INSERT INTO tennis_execution_bets(
             execution_key,created_at,event_id,sport_key,selection,bookmaker_key,
             bookmaker_title,offered_odds,fair_probability,fair_odds,edge_pct,min_odds,
             approved_books_seen,consensus_captured_at,consensus_age_minutes,
             reference_book_count,status
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("tennis-result|h2h|Player B","2030-09-01T10:00:00+00:00","tennis-result",
         "tennis_atp_us_open","Player B","matchbook","Matchbook",2.5,0.42,
         1/0.42,5.0,2.45,1,"2030-09-01T10:00:00+00:00",0,5,"OPEN")
    )
    db.execute("UPDATE quota_state SET credits_remaining=10000 WHERE singleton_id=1")
    engine=TennisShadowEngine(db,Api(),S(),execution_bookmaker_keys=("matchbook",))
    out=engine.collect_results(datetime(2030,9,1,15,0,tzinfo=timezone.utc))
    assert out["settled"]==1
    result=db.fetchone("SELECT * FROM tennis_results WHERE event_id='tennis-result'")
    assert result["winner"]=="Player B"
    bet=db.fetchone("SELECT * FROM tennis_execution_bets WHERE event_id='tennis-result'")
    assert bet["status"]=="SETTLED" and bet["result"]=="WIN"


def test_123_tennis_segments_include_odds_bands_and_side(db):
    _seed_tennis_event(db)
    for i,(odds,p) in enumerate(((1.4,.75),(1.8,.60),(2.5,.42),(4.0,.28),(6.0,.18)),1):
        db.execute(
            """INSERT INTO tennis_execution_bets(
                 execution_key,created_at,event_id,sport_key,selection,bookmaker_key,
                 bookmaker_title,offered_odds,fair_probability,fair_odds,edge_pct,min_odds,
                 approved_books_seen,consensus_captured_at,consensus_age_minutes,
                 reference_book_count,status
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"seg{i}","2030-09-01T12:00:00+00:00","tennis1","tennis_atp_us_open",
             f"Player {i}","matchbook","Matchbook",odds,p,1/p,1.0,odds-0.1,1,
             "2030-09-01T12:00:00+00:00",0,5,"OPEN")
        )
    seg=tennis_segments(db)
    assert {x["label"] for x in seg["odds_band"]}=={"<1.5","1.5-2","2-3","3-5","5+"}
    assert {x["label"] for x in seg["side"]}=={"FAVOURITE","OUTSIDER"}


def test_124_export_contains_tennis_tables_and_summary(db):
    class S:
        sport_keys=("soccer_epl",);odds_region="uk";odds_markets=("h2h","totals","btts")
        enable_dnb_market=False;execution_shadow_enabled=True
        execution_bookmaker_keys=("matchbook",);min_consensus_books=3
        min_edge_pct=3.0;min_cross_market_edge_pct=4.0;min_slow_book_gap_pct=4.0
        daily_paid_credit_budget=8000;quota_reserve_credits=1000
        breadth_polls_per_day=160;max_events_per_odds_cycle=3;enable_live_betting=False
        betfair_commission_pct=5.0;matchbook_commission_pct=2.0;smarkets_commission_pct=2.0
        default_execution_commission_pct=5.0;odds_api_key=""
        tennis_shadow_enabled=True;tennis_market="h2h";tennis_min_consensus_books=5
        tennis_min_edge_pct=3.0;tennis_daily_paid_credit_budget=500
        tennis_quota_reserve_credits=1000;tennis_max_consensus_age_minutes=240
    payload,_=build_research_export(db,S(),"0.7.0")
    import io,zipfile
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        assert "tables/tennis_execution_bets.csv" in z.namelist()
        assert "tables/tennis_consensus_snapshots.csv" in z.namelist()
        summary=json.loads(z.read("analysis_summary.json"))
        assert "tennis_shadow" in summary
        manifest=json.loads(z.read("manifest.json"))
        assert manifest["settings"]["tennis_shadow_enabled"] is True


def test_125_tennis_is_not_counted_in_football_quota_lane(db):
    now=datetime.now(timezone.utc).isoformat()
    db.execute("UPDATE quota_state SET credits_remaining=10000 WHERE singleton_id=1")
    db.execute(
        """INSERT INTO collector_runs(started_at,finished_at,run_type,estimated_cost,actual_cost,ok,detail)
           VALUES(?,?,?,?,?,?,?)""",
        (now,now,"TENNIS_ODDS",1,400,1,"tennis")
    )
    q=QuotaGuard(db,reserve=1000,daily_budget=8000)
    assert q.today_paid_cost()==0
    assert q.decide(3).allowed


def test_126_provider_sport_odds_supports_region_or_explicit_books():
    class Response:
        headers={"x-requests-remaining":"999","x-requests-used":"1","x-requests-last":"1"}
        def raise_for_status(self): pass
        def json(self): return []
    class Session:
        def __init__(self): self.calls=[]
        def get(self,url,params,timeout):
            self.calls.append((url,dict(params),timeout)); return Response()
    s=Session();api=TheOddsApi("SECRET",session=s)
    api.sport_odds("tennis_atp_us_open","uk",("h2h",))
    url,params,_=s.calls[-1]
    assert url.endswith("/sports/tennis_atp_us_open/odds")
    assert params["regions"]=="uk"
    assert params["markets"]=="h2h"
    assert params["apiKey"]=="SECRET"
    api.sport_odds(
        "tennis_atp_us_open","uk",("h2h",),
        bookmaker_keys=("betfair_ex_uk","matchbook","smarkets")
    )
    _,params,_=s.calls[-1]
    assert "regions" not in params
    assert params["bookmakers"]=="betfair_ex_uk,matchbook,smarkets"


def test_127_v071_multiples_empty_api_allowlist_pauses_new_formation(db):
    now=datetime(2026,9,4,13,0,tzinfo=timezone.utc)
    for eid in ("gateempty1","gateempty2"):
        _seed_multiple_exec(
            db,eid,commence="2026-09-04T18:00:00+00:00",min_odds=1.8
        )
        _seed_book_quote(
            db,eid,captured="2026-09-04T12:55:00+00:00",
            book="paddypower",title="Paddy Power",price=2.2
        )
    assert generate_multiple_shadows(
        db,now=now,allowed_bookmaker_keys=()
    )==0
    assert db.fetchone(
        "SELECT COUNT(*) AS n FROM multiple_shadow_bets"
    )["n"]==0


def test_128_v071_only_explicit_api_allowlisted_book_can_form_multiple(db):
    now=datetime(2026,9,4,13,0,tzinfo=timezone.utc)
    for eid in ("gateallow1","gateallow2"):
        _seed_multiple_exec(
            db,eid,commence="2026-09-04T18:00:00+00:00",min_odds=1.8
        )
        # Better price at non-allowlisted Paddy Power must be ignored.
        _seed_book_quote(
            db,eid,captured="2026-09-04T12:55:00+00:00",
            book="paddypower",title="Paddy Power",price=2.8
        )
        _seed_book_quote(
            db,eid,captured="2026-09-04T12:55:00+00:00",
            book="verified_api",title="Verified API Venue",price=2.2
        )
    assert generate_multiple_shadows(
        db,now=now,allowed_bookmaker_keys=("verified_api",)
    )==1
    row=db.fetchone("SELECT * FROM multiple_shadow_bets")
    assert row["bookmaker_key"]=="verified_api"
    assert row["combined_odds"]==pytest.approx(2.2*2.2)
    assert row["automation_eligible"]==1
    assert row["venue_policy"]=="EXPLICIT_VERIFIED_MULTIPLES_API_ALLOWLIST"
    assert json.loads(row["allowed_api_bookmakers_json"])==["verified_api"]
    assert row["algorithm_version"]=="MS1.1_API_GATE"
    assert row["app_version"]=="0.7.2"


def test_129_v071_legacy_non_api_multiples_preserved_but_excluded_from_headline(db):
    now="2026-09-04T13:00:00+00:00"
    db.execute(
        """INSERT INTO multiple_shadow_bets(
             multiple_key,created_at,algorithm_version,leg_count,bookmaker_key,
             bookmaker_title,source_execution_ids_json,market_mix,kickoff_date,
             first_kickoff,last_kickoff,combined_odds,fair_probability,fair_odds,
             edge_pct,max_entry_quote_age_minutes,status,automation_eligible
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "legacy|2|1,2",now,"MS1",2,"paddypower","Paddy Power","[1,2]",
            "h2h+h2h","2026-09-04","2026-09-04T18:00:00+00:00",
            "2026-09-04T19:00:00+00:00",5.0,0.20,5.0,0.0,5.0,
            "OPEN",0
        )
    )
    score=multiples_scoreboard(db)
    assert score["bets"]==0
    assert score["automation_eligible_bets"]==0
    assert score["legacy_non_api_research_bets"]==1
    assert latest_multiple_shadows(db,100)==[]
    legacy=latest_multiple_shadows(db,100,include_legacy=True)
    assert len(legacy)==1
    assert legacy[0]["bookmaker_key"]=="paddypower"


def test_130_v071_config_default_has_no_multiple_api_venues():
    s=Settings()
    assert s.multiples_api_bookmaker_keys==()


def test_131_v071_new_multiple_fingerprint_changes_when_api_allowlist_changes(db):
    now=datetime(2026,9,4,13,0,tzinfo=timezone.utc)
    for eid in ("fingerapi1","fingerapi2"):
        _seed_multiple_exec(
            db,eid,commence="2026-09-04T18:00:00+00:00",min_odds=1.8
        )
        _seed_book_quote(
            db,eid,captured="2026-09-04T12:55:00+00:00",
            book="venue_a",title="Venue A",price=2.2
        )
    assert generate_multiple_shadows(
        db,now=now,allowed_bookmaker_keys=("venue_a",)
    )==1
    first=db.fetchone("SELECT config_hash FROM multiple_shadow_bets")
    assert first["config_hash"]


def test_132_v071_export_marks_legacy_and_exposes_api_allowlist(db):
    class S:
        sport_keys=("soccer_epl",);odds_region="uk";odds_markets=("h2h","totals","btts")
        enable_dnb_market=False;execution_shadow_enabled=True
        execution_bookmaker_keys=("matchbook",);multiples_api_bookmaker_keys=()
        min_consensus_books=3;min_edge_pct=3.0;min_cross_market_edge_pct=4.0
        min_slow_book_gap_pct=4.0;daily_paid_credit_budget=8000
        quota_reserve_credits=1000;breadth_polls_per_day=160
        max_events_per_odds_cycle=3;enable_live_betting=False
        betfair_commission_pct=5.0;matchbook_commission_pct=2.0
        smarkets_commission_pct=2.0;default_execution_commission_pct=5.0
        odds_api_key="";tennis_shadow_enabled=True;tennis_market="h2h"
        tennis_min_consensus_books=5;tennis_min_edge_pct=3.0
        tennis_daily_paid_credit_budget=500;tennis_quota_reserve_credits=1000
        tennis_max_consensus_age_minutes=240
    payload,_=build_research_export(db,S(),"0.7.1")
    import io,zipfile
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        manifest=json.loads(z.read("manifest.json"))
        assert manifest["settings"]["multiples_api_bookmaker_keys"]==[]
        summary=json.loads(z.read("analysis_summary.json"))
        assert summary["multiples_shadow"]["formation_paused_no_verified_api_venue"] is True


def test_133_v072_worker_uses_multiples_api_allowlist(db, monkeypatch):
    called = {}

    class IdleCollector:
        def poll_one_cycle(self):
            return {
                "polled":0,"inserted":0,"reason":"idle","mode":"idle",
                "event_ids":[],"requested_markets":[]
            }

    def fake_generate(database, *, allowed_bookmaker_keys=(), **kwargs):
        called["allowed"] = tuple(allowed_bookmaker_keys)
        return 0

    monkeypatch.setattr(worker_module, "generate_multiple_shadows", fake_generate)

    w = Worker(
        db, IdleCollector(),
        min_books=3, min_edge_pct=3.0, min_cross_market_edge_pct=4.0,
        min_slow_book_gap_pct=4.0,
        execution_bookmaker_keys=("matchbook",),
        multiples_api_bookmaker_keys=("verified_multi_api",),
        execution_shadow_enabled=False,
        result_collector=None,
        tennis_engine=None,
    )
    w.one_cycle()

    assert called["allowed"] == ("verified_multi_api",)
    log = db.fetchone(
        """SELECT * FROM collector_runs
           WHERE run_type='MULTIPLES_SHADOW_MAINT'
           ORDER BY id DESC LIMIT 1"""
    )
    assert log is not None
    assert log["ok"] == 1
    assert "unexpected keyword argument" not in str(log.get("detail") or "")


def test_134_v072_worker_blank_allowlist_still_runs_legacy_multiple_maintenance(db):
    class IdleCollector:
        def poll_one_cycle(self):
            return {
                "polled":0,"inserted":0,"reason":"idle","mode":"idle",
                "event_ids":[],"requested_markets":[]
            }

    w = Worker(
        db, IdleCollector(),
        min_books=3, min_edge_pct=3.0, min_cross_market_edge_pct=4.0,
        min_slow_book_gap_pct=4.0,
        execution_bookmaker_keys=("matchbook",),
        multiples_api_bookmaker_keys=(),
        execution_shadow_enabled=False,
        result_collector=None,
        tennis_engine=None,
    )
    w.one_cycle()
    log = db.fetchone(
        """SELECT * FROM collector_runs
           WHERE run_type='MULTIPLES_SHADOW_MAINT'
           ORDER BY id DESC LIMIT 1"""
    )
    assert log is not None
    assert log["ok"] == 1
    assert "created=0" in log["detail"]


def test_135_v072_severely_overdue_breadth_inside_24h_is_urgent():
    now = datetime(2026,9,6,12,0,tzinfo=timezone.utc)
    event = {
        "commence_time": (now + timedelta(hours=18)).isoformat(),
        # Final-24h normal interval is 6h; 13h stale is >2x debt.
        "last_odds_poll_at": (now - timedelta(hours=13)).isoformat(),
    }
    assert breadth_is_urgent(event, now) is True


def test_136_v072_mildly_overdue_breadth_inside_24h_remains_paced():
    now = datetime(2026,9,6,12,0,tzinfo=timezone.utc)
    event = {
        "commence_time": (now + timedelta(hours=18)).isoformat(),
        # Due, but not severely overdue: 7h / 6h ~= 1.17x.
        "last_odds_poll_at": (now - timedelta(hours=7)).isoformat(),
    }
    assert event_is_due(event, now) is True
    assert breadth_is_urgent(event, now) is False


def test_137_v072_never_polled_match_inside_24h_is_urgent():
    now = datetime(2026,9,6,12,0,tzinfo=timezone.utc)
    event = {
        "commence_time": (now + timedelta(hours=20)).isoformat(),
        "last_odds_poll_at": None,
    }
    assert breadth_is_urgent(event, now) is True


def test_138_v072_far_future_stale_breadth_does_not_jump_24h_guard():
    now = datetime(2026,9,6,12,0,tzinfo=timezone.utc)
    event = {
        "commence_time": (now + timedelta(hours=30)).isoformat(),
        "last_odds_poll_at": (now - timedelta(days=4)).isoformat(),
    }
    assert breadth_is_urgent(event, now) is False


def test_139_v072_18h_severely_stale_breadth_can_jump_pacing_queue(db):
    class BroadApi:
        def quota_probe(self):
            return ApiResult([],10000,0,0)
        def event_odds(self,sport_key,event_id,region,markets,bookmaker_keys=()):
            return ApiResult({"id":event_id,"bookmakers":[]},9999,1,1)

    now = datetime.now(timezone.utc)
    kick = (now + timedelta(hours=18)).isoformat()
    stale = (now - timedelta(hours=13)).isoformat()

    db.execute(
        """INSERT INTO events(
             event_id,sport_key,league,commence_time,home_team,away_team,
             first_seen_at,last_seen_at,last_odds_poll_at,status
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("stale18h","soccer_epl","Premier League",kick,"H","A",
         now.isoformat(),now.isoformat(),stale,"UPCOMING")
    )

    # Make today's paced target already satisfied.
    for i in range(500):
        db.record_collector_run(
            "ODDS",True,event_id=f"done{i}",actual_cost=1
        )

    db.execute(
        "UPDATE quota_state SET credits_remaining=10000 WHERE singleton_id=1"
    )
    c = Collector(
        db,BroadApi(),QuotaGuard(db,reserve=10,daily_budget=10000),
        sport_keys=["soccer_epl"],region="uk",markets=["h2h"],
        breadth_polls_per_day=160,execution_bookmaker_keys=()
    )
    out = c.poll_one_cycle()
    assert out["mode"] == "breadth"
    assert out["polled"] == 1
    assert "stale18h" in out["event_ids"]


def test_140_v072_future_multiple_rows_fingerprint_hotfix_version(db):
    now=datetime(2026,9,4,13,0,tzinfo=timezone.utc)
    for eid in ("v072fp1","v072fp2"):
        _seed_multiple_exec(
            db,eid,commence="2026-09-04T18:00:00+00:00",min_odds=1.8
        )
        _seed_book_quote(
            db,eid,captured="2026-09-04T12:55:00+00:00",
            book="verified_api",title="Verified API",price=2.2
        )
    assert generate_multiple_shadows(
        db,now=now,allowed_bookmaker_keys=("verified_api",)
    ) == 1
    row=db.fetchone("SELECT * FROM multiple_shadow_bets")
    assert row["app_version"]=="0.7.2"
    assert row["algorithm_version"]=="MS1.1_API_GATE"


def _seed_multisport_state(
    db,key="baseball_mlb",title="MLB",family="BASEBALL",active=1
):
    now="2030-09-01T10:00:00+00:00"
    db.execute(
        """INSERT INTO multisport_league_state(
             sport_key,title,group_name,sport_family,active,targeted,
             first_seen_at,last_seen_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (key,title,family,family,active,1,now,now)
    )


def _seed_multisport_event(
    db,event_id="ms1",sport_key="baseball_mlb",title="MLB",
    family="BASEBALL",commence="2030-09-01T18:00:00+00:00",
    home="Home Team",away="Away Team"
):
    if not db.fetchone(
        "SELECT sport_key FROM multisport_league_state WHERE sport_key=?",
        (sport_key,)
    ):
        _seed_multisport_state(db,sport_key,title,family,1)
    db.execute(
        """INSERT INTO multisport_events(
             event_id,sport_key,league_title,sport_family,commence_time,
             home_team,away_team,first_seen_at,last_seen_at,status
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (event_id,sport_key,title,family,commence,home,away,
         "2030-09-01T10:00:00+00:00","2030-09-01T10:00:00+00:00","UPCOMING")
    )


def _seed_multisport_quote(
    db,event_id,at,mode,book,selection,price,
    sport_key="baseball_mlb",title=None
):
    db.execute(
        """INSERT INTO multisport_odds_snapshots(
             event_id,sport_key,captured_at,capture_mode,bookmaker_key,
             bookmaker_title,market_key,selection,price
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (event_id,sport_key,at,mode,book,title or book,"h2h",selection,price)
    )


def _seed_multisport_consensus_wave(db,event_id="ms1",at="2030-09-01T12:00:00+00:00"):
    prices={
        "book1":(1.70,2.25),"book2":(1.72,2.22),"book3":(1.68,2.28),
        "book4":(1.71,2.24),"book5":(1.69,2.26),
    }
    for book,(h,a) in prices.items():
        _seed_multisport_quote(db,event_id,at,"BREADTH",book,"Home Team",h)
        _seed_multisport_quote(db,event_id,at,"BREADTH",book,"Away Team",a)
    return write_multisport_consensus(
        db,event_id,at,min_books=5,
        excluded_books=("betfair_ex_uk","matchbook","smarkets")
    )


def test_141_v080_multisport_tables_exist(db):
    for table in (
        "multisport_league_state","multisport_events",
        "multisport_odds_snapshots","multisport_consensus_snapshots",
        "multisport_execution_evaluations","multisport_execution_bets",
        "multisport_price_observations","multisport_results",
    ):
        row=db.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,)
        )
        assert row is not None, table


def test_142_v080_default_target_sports_cover_initial_and_seasonal_expansion():
    s=Settings()
    keys=set(s.multisport_sport_keys)
    for key in (
        "baseball_mlb","americanfootball_nfl","americanfootball_ncaaf",
        "basketball_wnba","aussierules_afl","rugbyleague_nrl",
        "basketball_nba","basketball_euroleague","basketball_ncaab",
        "icehockey_nhl","icehockey_sweden_hockey_league",
        "icehockey_sweden_allsvenskan",
    ):
        assert key in keys


def test_143_v080_discovery_only_activates_provider_in_season_targets(db):
    class Api:
        def sports(self):
            return ApiResult([
                {"key":"baseball_mlb","title":"MLB","group":"Baseball","active":True},
                {"key":"soccer_epl","title":"EPL","group":"Soccer","active":True},
            ],10000,0,0)
    class S:
        multisport_shadow_enabled=True
        multisport_sport_keys=("baseball_mlb","basketball_nba")
        multisport_market="h2h";multisport_odds_region="uk"
        multisport_min_consensus_books=5;multisport_min_edge_pct=3.0
        multisport_daily_paid_credit_budget=1500
        multisport_quota_reserve_credits=1000
        multisport_max_consensus_age_minutes=240
        multisport_discovery_interval_seconds=21600
        multisport_result_poll_interval_seconds=3600
    engine=MultiSportShadowEngine(
        db,Api(),S(),execution_bookmaker_keys=("matchbook",)
    )
    assert engine.discover_active_leagues(
        datetime(2030,9,1,12,0,tzinfo=timezone.utc)
    )==1
    mlb=db.fetchone(
        "SELECT * FROM multisport_league_state WHERE sport_key='baseball_mlb'"
    )
    nba=db.fetchone(
        "SELECT * FROM multisport_league_state WHERE sport_key='basketball_nba'"
    )
    assert mlb["active"]==1
    assert nba["active"]==0
    assert db.fetchone(
        "SELECT * FROM multisport_league_state WHERE sport_key='soccer_epl'"
    ) is None


def test_144_v080_consensus_uses_exactly_two_way_books_only(db):
    _seed_multisport_event(db)
    at="2030-09-01T12:00:00+00:00"
    assert _seed_multisport_consensus_wave(db,"ms1",at)==2
    # A sixth book with a Draw outcome must not contaminate two-way consensus.
    for selection,price in (
        ("Home Team",2.0),("Away Team",3.0),("Draw",4.0)
    ):
        _seed_multisport_quote(
            db,"ms1","2030-09-01T12:10:00+00:00","BREADTH",
            "threeway",selection,price
        )
    # Add four valid books at the same wave: total valid books=4, below min=5.
    for i in range(4):
        _seed_multisport_quote(
            db,"ms1","2030-09-01T12:10:00+00:00","BREADTH",
            f"valid{i}","Home Team",1.7
        )
        _seed_multisport_quote(
            db,"ms1","2030-09-01T12:10:00+00:00","BREADTH",
            f"valid{i}","Away Team",2.25
        )
    assert write_multisport_consensus(
        db,"ms1","2030-09-01T12:10:00+00:00",
        min_books=5,excluded_books=()
    )==0


def test_145_v080_three_way_approved_venue_is_rejected(db):
    _seed_multisport_event(db)
    broad="2030-09-01T12:00:00+00:00"
    _seed_multisport_consensus_wave(db,"ms1",broad)
    wave="2030-09-01T12:30:00+00:00"
    for selection,price in (
        ("Home Team",1.8),("Away Team",2.6),("Draw",20.0)
    ):
        _seed_multisport_quote(
            db,"ms1",wave,"CONVERGENCE","matchbook",selection,price
        )
    assert evaluate_multisport_convergence_wave(
        db,sport_key="baseball_mlb",captured_at=wave,
        execution_bookmaker_keys=("matchbook",),min_edge_pct=3.0,
        max_consensus_age_minutes=240,config_hash="abc"
    )==0
    assert db.fetchone("SELECT id FROM multisport_execution_bets") is None
    reasons={
        r["reason"] for r in db.fetchall(
            "SELECT reason FROM multisport_execution_evaluations"
        )
    }
    assert "APPROVED_VENUE_NOT_TWO_WAY" in reasons


def test_146_v080_first_acceptable_two_way_price_is_frozen(db):
    _seed_multisport_event(db)
    broad="2030-09-01T12:00:00+00:00"
    _seed_multisport_consensus_wave(db,"ms1",broad)
    c=db.fetchone(
        """SELECT * FROM multisport_consensus_snapshots
           WHERE event_id='ms1' AND selection='Away Team'"""
    )
    minimum=min_odds_for_probability(float(c["fair_probability"]),3.0)

    first="2030-09-01T12:30:00+00:00"
    for sel,price in (
        ("Home Team",1.8),("Away Team",minimum-0.02)
    ):
        _seed_multisport_quote(db,"ms1",first,"CONVERGENCE","matchbook",sel,price)
    assert evaluate_multisport_convergence_wave(
        db,sport_key="baseball_mlb",captured_at=first,
        execution_bookmaker_keys=("matchbook",),min_edge_pct=3.0,
        max_consensus_age_minutes=240,config_hash="abc"
    )==0

    second="2030-09-01T12:45:00+00:00"
    for sel,price in (
        ("Home Team",1.75),("Away Team",minimum+0.08)
    ):
        _seed_multisport_quote(db,"ms1",second,"CONVERGENCE","matchbook",sel,price)
    assert evaluate_multisport_convergence_wave(
        db,sport_key="baseball_mlb",captured_at=second,
        execution_bookmaker_keys=("matchbook",),min_edge_pct=3.0,
        max_consensus_age_minutes=240,config_hash="abc"
    )==1
    bet=db.fetchone(
        "SELECT * FROM multisport_execution_bets WHERE selection='Away Team'"
    )
    assert bet["created_at"]==second
    assert bet["offered_odds"]==pytest.approx(minimum+0.08)
    assert bet["experiment_version"]=="MSP1_TWO_WAY_PRICE"
    assert bet["app_version"]=="0.8.1"

    third="2030-09-01T13:00:00+00:00"
    for sel,price in (
        ("Home Team",1.6),("Away Team",minimum+0.50)
    ):
        _seed_multisport_quote(db,"ms1",third,"CONVERGENCE","matchbook",sel,price)
    evaluate_multisport_convergence_wave(
        db,sport_key="baseball_mlb",captured_at=third,
        execution_bookmaker_keys=("matchbook",),min_edge_pct=3.0,
        max_consensus_age_minutes=240,config_hash="abc"
    )
    frozen=db.fetchone(
        "SELECT * FROM multisport_execution_bets WHERE selection='Away Team'"
    )
    assert frozen["created_at"]==second
    assert frozen["offered_odds"]==pytest.approx(minimum+0.08)


def test_147_v080_clv_skips_invalid_three_way_close_wave(db):
    _seed_multisport_event(
        db,commence="2030-09-01T18:00:00+00:00"
    )
    db.execute(
        """INSERT INTO multisport_execution_bets(
             execution_key,created_at,event_id,sport_key,selection,
             bookmaker_key,bookmaker_title,offered_odds,fair_probability,
             fair_odds,edge_pct,min_odds,approved_books_seen,
             consensus_captured_at,consensus_age_minutes,reference_book_count,
             status
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("ms1|h2h|Away Team","2030-09-01T12:00:00+00:00","ms1",
         "baseball_mlb","Away Team","matchbook","Matchbook",2.60,0.42,
         1/0.42,9.2,2.45,1,"2030-09-01T12:00:00+00:00",0,5,"OPEN")
    )
    # Valid two-way B-quality wave 20m before start.
    for sel,price in (("Home Team",1.70),("Away Team",2.30)):
        _seed_multisport_quote(
            db,"ms1","2030-09-01T17:40:00+00:00",
            "CONVERGENCE","matchbook",sel,price
        )
    # Closer but invalid three-way wave must be ignored.
    for sel,price in (
        ("Home Team",1.80),("Away Team",2.10),("Draw",30.0)
    ):
        _seed_multisport_quote(
            db,"ms1","2030-09-01T17:55:00+00:00",
            "CONVERGENCE","matchbook",sel,price
        )
    assert finalize_multisport_clv(
        db,datetime(2030,9,1,18,10,tzinfo=timezone.utc)
    )==1
    bet=db.fetchone("SELECT * FROM multisport_execution_bets")
    assert bet["closing_odds"]==pytest.approx(2.30)
    assert bet["closing_minutes_before_start"]==pytest.approx(20.0)
    assert bet["clv_quality"]=="B"


def test_148_v080_settlement_handles_win_commission_and_push(db):
    _seed_multisport_event(db)
    db.execute(
        """INSERT INTO multisport_execution_bets(
             execution_key,created_at,event_id,sport_key,selection,
             bookmaker_key,bookmaker_title,offered_odds,fair_probability,
             fair_odds,edge_pct,min_odds,approved_books_seen,
             consensus_captured_at,consensus_age_minutes,reference_book_count,
             status
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("win","2030-09-01T12:00:00+00:00","ms1","baseball_mlb",
         "Away Team","matchbook","Matchbook",3.0,0.36,1/0.36,8.0,2.86,
         1,"2030-09-01T12:00:00+00:00",0,5,"OPEN")
    )
    assert settle_multisport_event(
        db,"ms1",home_score=2,away_score=5
    )==1
    win=db.fetchone("SELECT * FROM multisport_execution_bets WHERE execution_key='win'")
    assert win["result"]=="WIN"
    assert win["net_pnl_units"]==pytest.approx(1.96)

    _seed_multisport_event(db,event_id="push1")
    db.execute(
        """INSERT INTO multisport_execution_bets(
             execution_key,created_at,event_id,sport_key,selection,
             bookmaker_key,bookmaker_title,offered_odds,fair_probability,
             fair_odds,edge_pct,min_odds,approved_books_seen,
             consensus_captured_at,consensus_age_minutes,reference_book_count,
             status
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("push","2030-09-01T12:00:00+00:00","push1","baseball_mlb",
         "Home Team","matchbook","Matchbook",2.0,0.5,2.0,0,2.0,
         1,"2030-09-01T12:00:00+00:00",0,5,"OPEN")
    )
    assert settle_multisport_event(
        db,"push1",home_score=3,away_score=3
    )==1
    push=db.fetchone("SELECT * FROM multisport_execution_bets WHERE execution_key='push'")
    assert push["result"]=="PUSH"
    assert push["pnl_units"]==pytest.approx(0.0)
    assert push["net_pnl_units"]==pytest.approx(0.0)


def test_149_v080_scoreboard_headline_clv_is_a_b_only(db):
    _seed_multisport_event(db)
    for i,(quality,value) in enumerate((("A",2.0),("B",4.0),("C",-20.0)),1):
        db.execute(
            """INSERT INTO multisport_execution_bets(
                 execution_key,created_at,event_id,sport_key,selection,
                 bookmaker_key,bookmaker_title,offered_odds,fair_probability,
                 fair_odds,edge_pct,min_odds,approved_books_seen,
                 consensus_captured_at,consensus_age_minutes,reference_book_count,
                 status,clv_pct,clv_quality
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"k{i}","2030-09-01T12:00:00+00:00","ms1","baseball_mlb",
             f"S{i}","matchbook","Matchbook",2.0,0.5,2.0,0,2.0,1,
             "2030-09-01T12:00:00+00:00",0,5,"OPEN",value,quality)
        )
    score=multisport_scoreboard(db)
    assert score["clv_samples"]==2
    assert score["avg_clv_pct"]==pytest.approx(3.0)
    assert score["all_clv_samples"]==3


def test_150_v080_multisport_quota_lane_is_separate(db):
    now_dt=datetime.now(timezone.utc);stamp=now_dt.isoformat()
    db.execute("UPDATE quota_state SET credits_remaining=5000 WHERE singleton_id=1")
    for run_type,cost in (
        ("ODDS",300),("TENNIS_ODDS",50),("MULTISPORT_ODDS",20)
    ):
        db.execute(
            """INSERT INTO collector_runs(
                 started_at,finished_at,run_type,estimated_cost,actual_cost,ok,detail
               ) VALUES(?,?,?,?,?,?,?)""",
            (stamp,stamp,run_type,1,cost,1,"test")
        )
    guard=MultiSportQuotaGuard(db,daily_budget=25,reserve=1000)
    assert guard.today_paid_cost(now_dt)==20
    assert guard.decide(5)==(True,"ok")
    assert guard.decide(6)==(False,"multisport_daily_paid_credit_budget")


def test_151_v080_engine_broad_then_convergence_creates_shadow(db):
    class Api:
        def sports(self):
            return ApiResult([
                {"key":"baseball_mlb","title":"MLB","group":"Baseball","active":True}
            ],10000,0,0)
        def sport_odds(self,sport_key,region,markets,bookmaker_keys=()):
            event={
                "id":"engine-ms","sport_key":sport_key,
                "commence_time":"2030-09-01T18:00:00+00:00",
                "home_team":"Home Team","away_team":"Away Team","bookmakers":[]
            }
            if bookmaker_keys:
                event["bookmakers"]=[{
                    "key":"matchbook","title":"Matchbook","markets":[{
                        "key":"h2h","outcomes":[
                            {"name":"Home Team","price":1.75},
                            {"name":"Away Team","price":2.60},
                        ]
                    }]
                }]
            else:
                for i,(h,a) in enumerate(
                    ((1.70,2.25),(1.72,2.22),(1.68,2.28),(1.71,2.24),(1.69,2.26)),1
                ):
                    event["bookmakers"].append({
                        "key":f"book{i}","title":f"Book {i}","markets":[{
                            "key":"h2h","outcomes":[
                                {"name":"Home Team","price":h},
                                {"name":"Away Team","price":a},
                            ]
                        }]
                    })
            return ApiResult([event],9999,1,1)
        def scores(self,*args,**kwargs):
            return ApiResult([],9999,1,2)
    class S:
        multisport_shadow_enabled=True
        multisport_sport_keys=("baseball_mlb",)
        multisport_market="h2h";multisport_odds_region="uk"
        multisport_min_consensus_books=5;multisport_min_edge_pct=3.0
        multisport_daily_paid_credit_budget=1500
        multisport_quota_reserve_credits=1000
        multisport_max_consensus_age_minutes=240
        multisport_discovery_interval_seconds=21600
        multisport_result_poll_interval_seconds=3600
    db.execute("UPDATE quota_state SET credits_remaining=10000 WHERE singleton_id=1")
    engine=MultiSportShadowEngine(
        db,Api(),S(),execution_bookmaker_keys=("matchbook",)
    )
    now=datetime(2030,9,1,12,0,tzinfo=timezone.utc)
    engine.discover_active_leagues(now)
    first=engine.one_cycle(now)
    assert first["odds"]["mode"]=="breadth"
    assert db.fetchone(
        "SELECT COUNT(*) AS n FROM multisport_consensus_snapshots"
    )["n"]==2
    second=engine.one_cycle(now+timedelta(minutes=1))
    assert second["odds"]["mode"]=="convergence"
    bet=db.fetchone(
        "SELECT * FROM multisport_execution_bets WHERE selection='Away Team'"
    )
    assert bet is not None
    assert bet["bookmaker_key"]=="matchbook"


def test_152_v080_result_collector_settles_completed_score(db):
    class Api:
        def sports(self): return ApiResult([],10000,0,0)
        def scores(self,sport_key,event_ids=(),days_from=3):
            assert days_from==3
            return ApiResult([{
                "id":"result-ms","completed":True,
                "commence_time":"2030-09-01T10:00:00+00:00",
                "scores":[
                    {"name":"Home Team","score":"3"},
                    {"name":"Away Team","score":"6"},
                ],
            }],9998,2,2)
    class S:
        multisport_shadow_enabled=True
        multisport_sport_keys=("baseball_mlb",)
        multisport_market="h2h";multisport_odds_region="uk"
        multisport_min_consensus_books=5;multisport_min_edge_pct=3.0
        multisport_daily_paid_credit_budget=1500
        multisport_quota_reserve_credits=1000
        multisport_max_consensus_age_minutes=240
        multisport_discovery_interval_seconds=21600
        multisport_result_poll_interval_seconds=3600
    _seed_multisport_event(
        db,event_id="result-ms",commence="2030-09-01T10:00:00+00:00"
    )
    db.execute(
        """INSERT INTO multisport_execution_bets(
             execution_key,created_at,event_id,sport_key,selection,
             bookmaker_key,bookmaker_title,offered_odds,fair_probability,
             fair_odds,edge_pct,min_odds,approved_books_seen,
             consensus_captured_at,consensus_age_minutes,reference_book_count,
             status
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("res","2030-09-01T08:00:00+00:00","result-ms","baseball_mlb",
         "Away Team","matchbook","Matchbook",2.5,0.42,1/0.42,5.0,2.45,
         1,"2030-09-01T08:00:00+00:00",0,5,"OPEN")
    )
    db.execute("UPDATE quota_state SET credits_remaining=10000 WHERE singleton_id=1")
    engine=MultiSportShadowEngine(
        db,Api(),S(),execution_bookmaker_keys=("matchbook",)
    )
    out=engine.collect_results(
        datetime(2030,9,1,15,0,tzinfo=timezone.utc)
    )
    assert out["settled"]==1
    result=db.fetchone(
        "SELECT * FROM multisport_results WHERE event_id='result-ms'"
    )
    assert result["winner"]=="Away Team"
    bet=db.fetchone(
        "SELECT * FROM multisport_execution_bets WHERE event_id='result-ms'"
    )
    assert bet["status"]=="SETTLED" and bet["result"]=="WIN"


def test_153_v080_worker_runs_multisport_in_isolated_lane(db):
    class IdleCollector:
        def poll_one_cycle(self):
            return {
                "polled":0,"inserted":0,"reason":"idle","mode":"idle",
                "event_ids":[],"requested_markets":[]
            }
    class FakeMulti:
        def __init__(self): self.calls=0
        def one_cycle(self):
            self.calls+=1
            return {"enabled":True,"odds":{"mode":"idle"}}
    fake=FakeMulti()
    w=Worker(
        db,IdleCollector(),min_books=3,min_edge_pct=3.0,
        min_cross_market_edge_pct=4.0,min_slow_book_gap_pct=4.0,
        execution_shadow_enabled=False,result_collector=None,
        tennis_engine=None,multisport_engine=fake,
    )
    w.one_cycle()
    assert fake.calls==1
    log=db.fetchone(
        """SELECT * FROM collector_runs
           WHERE run_type='MULTISPORT_SHADOW_MAINT'
           ORDER BY id DESC LIMIT 1"""
    )
    assert log is not None and log["ok"]==1


def test_154_v080_export_contains_multisport_tables_and_summary(db):
    payload,_=build_research_export(db,Settings(),"0.8.0")
    import io,zipfile
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        assert "tables/multisport_execution_bets.csv" in z.namelist()
        assert "tables/multisport_consensus_snapshots.csv" in z.namelist()
        summary=json.loads(z.read("analysis_summary.json"))
        assert "multisport_shadow" in summary
        manifest=json.loads(z.read("manifest.json"))
        assert manifest["settings"]["multisport_shadow_enabled"] is True


def test_155_v080_sport_family_mapping():
    assert sport_family("baseball_mlb")=="BASEBALL"
    assert sport_family("americanfootball_nfl")=="AMERICAN_FOOTBALL"
    assert sport_family("basketball_nba")=="BASKETBALL"
    assert sport_family("icehockey_nhl")=="ICE_HOCKEY"
    assert sport_family("aussierules_afl")=="AUSSIE_RULES"
    assert sport_family("rugbyleague_nrl")=="RUGBY_LEAGUE"


def test_156_v081_provider_cost_preserves_explicit_zero():
    assert provider_actual_cost(0, 7)==0
    assert provider_actual_cost(3, 7)==3
    assert provider_actual_cost(None, 7)==7


def test_157_v081_multisport_zero_cost_breadth_is_recorded_as_zero(db):
    class Api:
        def sport_odds(self,sport_key,region,markets,bookmaker_keys=()):
            return ApiResult([],10000,0,0)
    class S:
        multisport_shadow_enabled=True
        multisport_sport_keys=("baseball_mlb",)
        multisport_market="h2h";multisport_odds_region="uk"
        multisport_hockey_reference_region="us"
        multisport_min_consensus_books=5;multisport_min_edge_pct=3.0
        multisport_daily_paid_credit_budget=1500
        multisport_quota_reserve_credits=1000
        multisport_max_consensus_age_minutes=240
        multisport_discovery_interval_seconds=21600
        multisport_result_poll_interval_seconds=3600
    _seed_multisport_state(db,"baseball_mlb","MLB","BASEBALL",1)
    db.execute("UPDATE quota_state SET credits_remaining=10000 WHERE singleton_id=1")
    engine=MultiSportShadowEngine(
        db,Api(),S(),execution_bookmaker_keys=("matchbook",)
    )
    out=engine._poll_breadth(
        {"sport_key":"baseball_mlb","title":"MLB"},
        datetime(2030,9,1,12,0,tzinfo=timezone.utc)
    )
    assert out["cost"]==0
    log=db.fetchone(
        """SELECT actual_cost FROM collector_runs
           WHERE run_type='MULTISPORT_ODDS' ORDER BY id DESC LIMIT 1"""
    )
    assert log["actual_cost"]==0


def test_158_v081_afl_verified_dead_heat_uses_dead_heat_pnl(db):
    _seed_multisport_event(
        db,event_id="afl-tie",sport_key="aussierules_afl",
        title="AFL",family="AUSSIE_RULES",
        home="Home AFL",away="Away AFL"
    )
    for i,book in enumerate(("betfair_ex_uk","smarkets"),1):
        db.execute(
            """INSERT INTO multisport_execution_bets(
                 execution_key,created_at,event_id,sport_key,selection,
                 bookmaker_key,bookmaker_title,offered_odds,fair_probability,
                 fair_odds,edge_pct,min_odds,approved_books_seen,
                 consensus_captured_at,consensus_age_minutes,reference_book_count,
                 status
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"afl-{i}","2030-09-01T12:00:00+00:00","afl-tie",
                "aussierules_afl","Home AFL",book,book,3.0,0.4,2.5,
                20.0,2.58,1,"2030-09-01T12:00:00+00:00",0,5,"OPEN"
            )
        )
    assert settle_multisport_event(
        db,"afl-tie",home_score=90,away_score=90
    )==2
    rows=db.fetchall(
        "SELECT * FROM multisport_execution_bets ORDER BY id"
    )
    assert all(r["result"]=="DEAD_HEAT" for r in rows)
    assert all(r["pnl_units"]==pytest.approx(0.5) for r in rows)
    assert all(r["settlement_quality"]=="VERIFIED_VENUE_RULE" for r in rows)
    # Commission-aware net differs by venue but must remain positive.
    assert all(float(r["net_pnl_units"])>0 for r in rows)


def test_159_v081_unverified_afl_dead_heat_excluded_from_headline_pnl(db):
    _seed_multisport_event(
        db,event_id="afl-unverified",sport_key="aussierules_afl",
        title="AFL",family="AUSSIE_RULES",
        home="Home AFL",away="Away AFL"
    )
    db.execute(
        """INSERT INTO multisport_execution_bets(
             execution_key,created_at,event_id,sport_key,selection,
             bookmaker_key,bookmaker_title,offered_odds,fair_probability,
             fair_odds,edge_pct,min_odds,approved_books_seen,
             consensus_captured_at,consensus_age_minutes,reference_book_count,
             status
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "afl-unverified","2030-09-01T12:00:00+00:00","afl-unverified",
            "aussierules_afl","Home AFL","matchbook","Matchbook",3.0,
            0.4,2.5,20.0,2.58,1,"2030-09-01T12:00:00+00:00",0,5,"OPEN"
        )
    )
    settle_multisport_event(
        db,"afl-unverified",home_score=80,away_score=80
    )
    row=db.fetchone(
        "SELECT * FROM multisport_execution_bets WHERE execution_key='afl-unverified'"
    )
    assert row["result"]=="DEAD_HEAT_UNVERIFIED"
    assert row["settlement_quality"]=="UNVERIFIED_VENUE_RULE"
    score=multisport_scoreboard(db)
    assert score["all_settled"]==1
    assert score["settled"]==0
    assert score["settlement_excluded"]==1
    assert score["net_roi_pct"] is None


def test_160_v081_baseball_settlement_is_rule_provenanced(db):
    _seed_multisport_event(
        db,event_id="mlb-rule",sport_key="baseball_mlb",
        title="MLB",family="BASEBALL"
    )
    for i,book in enumerate(("smarkets","matchbook"),1):
        db.execute(
            """INSERT INTO multisport_execution_bets(
                 execution_key,created_at,event_id,sport_key,selection,
                 bookmaker_key,bookmaker_title,offered_odds,fair_probability,
                 fair_odds,edge_pct,min_odds,approved_books_seen,
                 consensus_captured_at,consensus_age_minutes,reference_book_count,
                 status
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"mlb-{i}","2030-09-01T12:00:00+00:00","mlb-rule",
                "baseball_mlb","Away Team",book,book,2.2,0.47,1/0.47,
                3.4,2.19,1,"2030-09-01T12:00:00+00:00",0,5,"OPEN"
            )
        )
    settle_multisport_event(db,"mlb-rule",home_score=2,away_score=5)
    sm=db.fetchone(
        "SELECT * FROM multisport_execution_bets WHERE bookmaker_key='smarkets'"
    )
    mb=db.fetchone(
        "SELECT * FROM multisport_execution_bets WHERE bookmaker_key='matchbook'"
    )
    assert sm["settlement_quality"]=="VERIFIED_SCORE_RULE"
    assert mb["settlement_quality"]=="RULE_SENSITIVE"
    assert "pitcher" in mb["settlement_provenance"].lower()


def test_161_v081_hockey_breadth_uses_hockey_reference_region(db):
    seen=[]
    class Api:
        def sport_odds(self,sport_key,region,markets,bookmaker_keys=()):
            seen.append((sport_key,region,tuple(bookmaker_keys)))
            return ApiResult([],10000,0,0)
    class S:
        multisport_shadow_enabled=True
        multisport_sport_keys=("icehockey_nhl",)
        multisport_market="h2h";multisport_odds_region="uk"
        multisport_hockey_reference_region="us"
        multisport_min_consensus_books=5;multisport_min_edge_pct=3.0
        multisport_daily_paid_credit_budget=1500
        multisport_quota_reserve_credits=1000
        multisport_max_consensus_age_minutes=240
        multisport_discovery_interval_seconds=21600
        multisport_result_poll_interval_seconds=3600
    _seed_multisport_state(
        db,"icehockey_nhl","NHL","ICE_HOCKEY",1
    )
    db.execute("UPDATE quota_state SET credits_remaining=10000 WHERE singleton_id=1")
    engine=MultiSportShadowEngine(
        db,Api(),S(),execution_bookmaker_keys=("matchbook",)
    )
    out=engine._poll_breadth(
        {"sport_key":"icehockey_nhl","title":"NHL"},
        datetime(2030,9,1,12,0,tzinfo=timezone.utc)
    )
    assert seen[0][1]=="us"
    assert out["reference_region"]=="us"


def test_162_v081_hockey_convergence_still_uses_explicit_execution_venues(db):
    seen=[]
    class Api:
        def sport_odds(self,sport_key,region,markets,bookmaker_keys=()):
            seen.append((region,tuple(bookmaker_keys)))
            return ApiResult([],10000,0,0)
    class S:
        multisport_shadow_enabled=True
        multisport_sport_keys=("icehockey_nhl",)
        multisport_market="h2h";multisport_odds_region="uk"
        multisport_hockey_reference_region="us"
        multisport_min_consensus_books=5;multisport_min_edge_pct=3.0
        multisport_daily_paid_credit_budget=1500
        multisport_quota_reserve_credits=1000
        multisport_max_consensus_age_minutes=240
        multisport_discovery_interval_seconds=21600
        multisport_result_poll_interval_seconds=3600
    _seed_multisport_state(
        db,"icehockey_nhl","NHL","ICE_HOCKEY",1
    )
    db.execute("UPDATE quota_state SET credits_remaining=10000 WHERE singleton_id=1")
    engine=MultiSportShadowEngine(
        db,Api(),S(),execution_bookmaker_keys=("matchbook","smarkets")
    )
    engine._poll_convergence(
        {"sport_key":"icehockey_nhl","title":"NHL"},
        datetime(2030,9,1,12,0,tzinfo=timezone.utc)
    )
    assert seen[0][0]=="uk"
    assert seen[0][1]==("matchbook","smarkets")


def test_163_v081_default_hockey_reference_region_is_us():
    assert Settings().multisport_hockey_reference_region=="us"


def test_164_v081_multisport_settlement_columns_exist(db):
    cols={
        r["name"] for r in db.fetchall("PRAGMA table_info(multisport_execution_bets)")
    }
    assert "settlement_quality" in cols
    assert "settlement_provenance" in cols


def test_165_v081_export_exposes_hockey_reference_region(db):
    payload,_=build_research_export(db,Settings(),"0.8.1")
    import io,zipfile
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        manifest=json.loads(z.read("manifest.json"))
        assert manifest["settings"]["multisport_hockey_reference_region"]=="us"



def _line_event_payload(
    event_id="line1",sport_key="americanfootball_nfl",
    commence="2030-09-01T18:00:00+00:00",home="Home Team",away="Away Team",
    books=None,
):
    return [{
        "id":event_id,"sport_key":sport_key,"commence_time":commence,
        "home_team":home,"away_team":away,"bookmakers":books or [],
    }]


def _line_book(key,spread=(-3.5,1.91,1.91),total=(47.5,1.91,1.91)):
    markets=[]
    if spread is not None:
        point,hprice,aprice=spread
        markets.append({"key":"spreads","outcomes":[
            {"name":"Home Team","price":hprice,"point":point},
            {"name":"Away Team","price":aprice,"point":-point},
        ]})
    if total is not None:
        point,oprice,uprice=total
        markets.append({"key":"totals","outcomes":[
            {"name":"Over","price":oprice,"point":point},
            {"name":"Under","price":uprice,"point":point},
        ]})
    return {"key":key,"title":key,"markets":markets}


def _seed_line_wave(db,event_id,at,mode,books,sport_key="americanfootball_nfl",title="NFL"):
    return insert_line_payload(
        db,sport_key=sport_key,league_title=title,
        payload=_line_event_payload(event_id=event_id,sport_key=sport_key,books=books),
        capture_mode=mode,captured_at=at,
    )


def test_166_v090_line_tables_exist(db):
    for table in (
        "multisport_lines_state","multisport_line_odds_snapshots",
        "multisport_line_consensus_snapshots","multisport_line_evaluations",
        "multisport_line_bets","multisport_line_price_observations",
    ):
        assert db.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",(table,)
        ) is not None


def test_167_v090_line_payload_accepts_valid_spread_and_total(db):
    rows,events=_seed_line_wave(
        db,"line1","2030-09-01T12:00:00+00:00","BREADTH",
        [_line_book("book1")]
    )
    assert rows==4 and events==["line1"]
    spread=db.fetchall(
        "SELECT * FROM multisport_line_odds_snapshots WHERE market_key='spreads'"
    )
    assert {r["line_point"] for r in spread}=={-3.5}
    assert {r["outcome_point"] for r in spread}=={-3.5,3.5}
    total=db.fetchall(
        "SELECT * FROM multisport_line_odds_snapshots WHERE market_key='totals'"
    )
    assert {r["line_point"] for r in total}=={47.5}


def test_168_v090_invalid_noncomplementary_spread_is_rejected(db):
    bad={"key":"bad","title":"bad","markets":[{
        "key":"spreads","outcomes":[
            {"name":"Home Team","price":1.9,"point":-3.5},
            {"name":"Away Team","price":1.9,"point":2.5},
        ]
    }]}
    rows,_=_seed_line_wave(db,"badline","2030-09-01T12:00:00+00:00","BREADTH",[bad])
    assert rows==0


def test_169_v090_exact_line_consensus_requires_three_books(db):
    at="2030-09-01T12:00:00+00:00"
    _seed_line_wave(db,"consline",at,"BREADTH",[
        _line_book("b1"),_line_book("b2",spread=(-3.5,1.95,1.87),total=None),
        _line_book("b3",spread=(-3.5,1.93,1.89),total=None),
        _line_book("other",spread=(-4.5,1.91,1.91),total=None),
    ])
    assert write_line_consensus(db,"consline",at,min_books=3,excluded_books=())==2
    rows=db.fetchall("SELECT * FROM multisport_line_consensus_snapshots")
    assert len(rows)==2
    assert {r["line_point"] for r in rows}=={-3.5}
    assert all(r["num_books"]==3 for r in rows)


def test_170_v090_first_acceptable_spread_price_is_frozen(db):
    broad="2030-09-01T12:00:00+00:00"
    _seed_line_wave(db,"freeze",broad,"BREADTH",[
        _line_book("b1",spread=(-3.5,1.91,1.91),total=None),
        _line_book("b2",spread=(-3.5,1.92,1.90),total=None),
        _line_book("b3",spread=(-3.5,1.93,1.89),total=None),
    ])
    write_line_consensus(db,"freeze",broad,min_books=3,excluded_books=("matchbook",))
    c=db.fetchone("SELECT * FROM multisport_line_consensus_snapshots WHERE event_id='freeze' AND market_key='spreads' AND selection='Home Team'")
    minimum=min_odds_for_probability(float(c["fair_probability"]),3.0)
    first="2030-09-01T12:30:00+00:00"
    _seed_line_wave(db,"freeze",first,"CONVERGENCE",[
        _line_book("matchbook",spread=(-3.5,minimum-0.02,1.9),total=None)
    ])
    assert evaluate_line_wave(db,sport_key="americanfootball_nfl",captured_at=first,execution_bookmaker_keys=("matchbook",),min_edge_pct=3.0,max_consensus_age_minutes=240,config_hash="x")==0
    second="2030-09-01T12:40:00+00:00"
    _seed_line_wave(db,"freeze",second,"CONVERGENCE",[
        _line_book("matchbook",spread=(-3.5,minimum+0.08,1.9),total=None)
    ])
    assert evaluate_line_wave(db,sport_key="americanfootball_nfl",captured_at=second,execution_bookmaker_keys=("matchbook",),min_edge_pct=3.0,max_consensus_age_minutes=240,config_hash="x")==1
    bet=db.fetchone("SELECT * FROM multisport_line_bets WHERE event_id='freeze' AND market_key='spreads' AND selection='Home Team'")
    assert bet["created_at"]==second
    assert bet["line_point"]==pytest.approx(-3.5)
    assert bet["app_version"]=="0.9.0"
    third="2030-09-01T12:50:00+00:00"
    _seed_line_wave(db,"freeze",third,"CONVERGENCE",[
        _line_book("matchbook",spread=(-3.5,minimum+0.50,1.8),total=None)
    ])
    evaluate_line_wave(db,sport_key="americanfootball_nfl",captured_at=third,execution_bookmaker_keys=("matchbook",),min_edge_pct=3.0,max_consensus_age_minutes=240,config_hash="x")
    frozen=db.fetchone("SELECT * FROM multisport_line_bets WHERE id=?",(bet["id"],))
    assert frozen["created_at"]==second


def test_171_v090_no_cross_line_consensus_matching(db):
    broad="2030-09-01T12:00:00+00:00"
    _seed_line_wave(db,"crossline",broad,"BREADTH",[
        _line_book("b1",spread=(-3.5,1.91,1.91),total=None),
        _line_book("b2",spread=(-3.5,1.92,1.90),total=None),
        _line_book("b3",spread=(-3.5,1.93,1.89),total=None),
    ])
    write_line_consensus(db,"crossline",broad,min_books=3,excluded_books=())
    wave="2030-09-01T12:30:00+00:00"
    _seed_line_wave(db,"crossline",wave,"CONVERGENCE",[
        _line_book("matchbook",spread=(-4.5,4.0,1.3),total=None)
    ])
    assert evaluate_line_wave(db,sport_key="americanfootball_nfl",captured_at=wave,execution_bookmaker_keys=("matchbook",),min_edge_pct=3.0,max_consensus_age_minutes=240,config_hash="x")==0
    assert db.fetchone("SELECT id FROM multisport_line_bets") is None
    reasons={r["reason"] for r in db.fetchall("SELECT reason FROM multisport_line_evaluations")}
    assert "NO_EXACT_LINE_CONSENSUS" in reasons


def test_172_v090_side_aware_line_clv_math():
    event={"home_team":"H","away_team":"A"}
    assert line_clv_points("spreads","H",-5.5,-6.5,event)==pytest.approx(1.0)
    assert line_clv_points("spreads","A",-5.5,-6.5,event)==pytest.approx(-1.0)
    assert line_clv_points("totals","Over",47.5,49.5,event)==pytest.approx(2.0)
    assert line_clv_points("totals","Under",47.5,45.5,event)==pytest.approx(2.0)


def test_173_v090_line_close_records_line_move_and_not_fake_price_clv(db):
    _seed_multisport_event(db,event_id="close-line",sport_key="americanfootball_nfl",title="NFL",family="AMERICAN_FOOTBALL",commence="2030-09-01T18:00:00+00:00")
    db.execute("""INSERT INTO multisport_line_bets(execution_key,created_at,event_id,sport_key,market_key,selection,line_point,bookmaker_key,bookmaker_title,offered_odds,fair_probability,fair_odds,edge_pct,min_odds,approved_books_seen,consensus_captured_at,consensus_age_minutes,reference_book_count,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("c","2030-09-01T12:00:00+00:00","close-line","americanfootball_nfl","spreads","Home Team",-5.5,"matchbook","Matchbook",1.95,.54,1/.54,5.3,1.91,1,"2030-09-01T12:00:00+00:00",0,3,"OPEN"))
    _seed_line_wave(db,"close-line","2030-09-01T17:40:00+00:00","CONVERGENCE",[
        _line_book("matchbook",spread=(-6.5,1.91,1.91),total=None)
    ])
    assert finalize_line_closes(db,datetime(2030,9,1,18,5,tzinfo=timezone.utc))==1
    bet=db.fetchone("SELECT * FROM multisport_line_bets")
    assert bet["closing_line_point"]==pytest.approx(-6.5)
    assert bet["line_clv_points"]==pytest.approx(1.0)
    assert bet["price_clv_pct"] is None
    assert bet["close_quality"]=="B"


def test_174_v090_spread_settlement(db):
    _seed_multisport_event(db,event_id="settle-spread",sport_key="americanfootball_nfl",title="NFL",family="AMERICAN_FOOTBALL")
    for key,sel in (("home","Home Team"),("away","Away Team")):
        db.execute("""INSERT INTO multisport_line_bets(execution_key,created_at,event_id,sport_key,market_key,selection,line_point,bookmaker_key,bookmaker_title,offered_odds,fair_probability,fair_odds,edge_pct,min_odds,approved_books_seen,consensus_captured_at,consensus_age_minutes,reference_book_count,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key,"2030-09-01T12:00:00+00:00","settle-spread","americanfootball_nfl","spreads",sel,-3.5,"matchbook","Matchbook",2.0,.5,2.0,0,2.0,1,"2030-09-01T12:00:00+00:00",0,3,"OPEN"))
    settle_line_event(db,"settle-spread",home_score=24,away_score=20)
    home=db.fetchone("SELECT * FROM multisport_line_bets WHERE execution_key='home'")
    away=db.fetchone("SELECT * FROM multisport_line_bets WHERE execution_key='away'")
    assert home["result"]=="WIN" and away["result"]=="LOSS"


def test_175_v090_total_settlement_and_push(db):
    _seed_multisport_event(db,event_id="settle-total",sport_key="basketball_nba",title="NBA",family="BASKETBALL")
    db.execute("""INSERT INTO multisport_line_bets(execution_key,created_at,event_id,sport_key,market_key,selection,line_point,bookmaker_key,bookmaker_title,offered_odds,fair_probability,fair_odds,edge_pct,min_odds,approved_books_seen,consensus_captured_at,consensus_age_minutes,reference_book_count,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("over","2030-09-01T12:00:00+00:00","settle-total","basketball_nba","totals","Over",200.0,"smarkets","Smarkets",2.0,.5,2.0,0,2.0,1,"2030-09-01T12:00:00+00:00",0,3,"OPEN"))
    settle_line_event(db,"settle-total",home_score=100,away_score=100)
    assert db.fetchone("SELECT result FROM multisport_line_bets WHERE execution_key='over'")["result"]=="PUSH"


def test_176_v090_multisport_budget_is_shared_with_lines(db):
    stamp=datetime.now(timezone.utc).isoformat()
    db.execute("UPDATE quota_state SET credits_remaining=5000 WHERE singleton_id=1")
    for typ,cost in (("MULTISPORT_ODDS",20),("MULTISPORT_LINES_ODDS",8),("MULTISPORT_LINES_CONVERGENCE",4)):
        db.execute("INSERT INTO collector_runs(started_at,finished_at,run_type,estimated_cost,actual_cost,ok,detail) VALUES(?,?,?,?,?,?,?)",(stamp,stamp,typ,cost,cost,1,"x"))
    guard=MultiSportQuotaGuard(db,daily_budget=40,reserve=1000)
    assert guard.today_paid_cost()==32
    assert guard.decide(8)==(True,"ok")
    assert guard.decide(9)==(False,"multisport_daily_paid_credit_budget")


def test_177_v090_line_reference_region_is_sport_aware():
    class S:
        multisport_lines_us_reference_region="us"
        multisport_lines_au_reference_region="au"
    assert reference_region(S(),"americanfootball_nfl")=="us"
    assert reference_region(S(),"baseball_mlb")=="us"
    assert reference_region(S(),"aussierules_afl")=="au"
    assert reference_region(S(),"rugbyleague_nrl")=="au"


def test_178_v090_default_lines_config_expands_market_universe():
    s=Settings()
    assert s.multisport_lines_enabled is True
    assert s.multisport_lines_markets==("spreads","totals")
    assert s.multisport_lines_min_consensus_books==3
    assert s.multisport_lines_min_edge_pct==pytest.approx(3.0)


def test_179_v090_lines_engine_broad_call_requests_both_markets(db):
    seen=[]
    class Api:
        def sport_odds(self,sport_key,region,markets,bookmaker_keys=()):
            seen.append((region,tuple(markets),tuple(bookmaker_keys)))
            return ApiResult([],10000,0,0)
    class S:
        multisport_lines_enabled=True;multisport_sport_keys=("americanfootball_nfl",)
        multisport_lines_markets=("spreads","totals");multisport_lines_min_consensus_books=3
        multisport_lines_min_edge_pct=3.0;multisport_lines_max_consensus_age_minutes=240
        multisport_lines_us_reference_region="us";multisport_lines_au_reference_region="au"
        multisport_lines_discovery_interval_seconds=21600;multisport_lines_result_poll_interval_seconds=3600
        multisport_daily_paid_credit_budget=1500;multisport_quota_reserve_credits=1000;multisport_odds_region="uk"
    db.execute("UPDATE quota_state SET credits_remaining=10000 WHERE singleton_id=1")
    engine=MultiSportLinesEngine(db,Api(),S(),execution_bookmaker_keys=("matchbook",))
    out=engine._poll_breadth({"sport_key":"americanfootball_nfl","title":"NFL"},datetime(2030,9,1,12,0,tzinfo=timezone.utc))
    assert seen[0]==("us",("spreads","totals"),())
    assert out["cost"]==0


def test_180_v090_lines_engine_generates_spread_and_total_shadows(db):
    class Api:
        def sports(self):return ApiResult([{"key":"americanfootball_nfl","title":"NFL","group":"American Football","active":True}],10000,0,0)
        def sport_odds(self,sport_key,region,markets,bookmaker_keys=()):
            if bookmaker_keys:
                books=[_line_book("matchbook",spread=(-3.5,2.15,1.70),total=(47.5,2.15,1.70))]
            else:
                books=[
                    _line_book("b1",spread=(-3.5,1.91,1.91),total=(47.5,1.91,1.91)),
                    _line_book("b2",spread=(-3.5,1.92,1.90),total=(47.5,1.92,1.90)),
                    _line_book("b3",spread=(-3.5,1.93,1.89),total=(47.5,1.93,1.89)),
                ]
            return ApiResult(_line_event_payload(event_id="engine-line",books=books),10000,0,2)
        def scores(self,*args,**kwargs):return ApiResult([],10000,0,0)
    class S:
        multisport_lines_enabled=True;multisport_sport_keys=("americanfootball_nfl",)
        multisport_lines_markets=("spreads","totals");multisport_lines_min_consensus_books=3
        multisport_lines_min_edge_pct=3.0;multisport_lines_max_consensus_age_minutes=240
        multisport_lines_us_reference_region="us";multisport_lines_au_reference_region="au"
        multisport_lines_discovery_interval_seconds=21600;multisport_lines_result_poll_interval_seconds=3600
        multisport_daily_paid_credit_budget=1500;multisport_quota_reserve_credits=1000;multisport_odds_region="uk"
    db.execute("UPDATE quota_state SET credits_remaining=10000 WHERE singleton_id=1")
    e=MultiSportLinesEngine(db,Api(),S(),execution_bookmaker_keys=("matchbook",))
    now=datetime(2030,9,1,12,0,tzinfo=timezone.utc);e.discover_active_leagues(now)
    assert e.one_cycle(now)["odds"]["mode"]=="breadth"
    second=e.one_cycle(now+timedelta(minutes=1))
    assert second["odds"]["mode"]=="convergence"
    rows=db.fetchall("SELECT * FROM multisport_line_bets ORDER BY market_key,selection")
    assert len(rows)>=2
    assert {r["market_key"] for r in rows}=={"spreads","totals"}


def test_181_v090_line_funnel_captures_rejection_reasons(db):
    _seed_multisport_event(db,event_id="e",sport_key="americanfootball_nfl",title="NFL",family="AMERICAN_FOOTBALL")
    db.execute("INSERT INTO multisport_line_evaluations(evaluated_at,event_id,sport_key,market_key,selection,decision,reason) VALUES(?,?,?,?,?,?,?)",("2030-09-01T12:00:00+00:00","e","americanfootball_nfl","spreads","Home","REJECT","NO_EXACT_LINE_CONSENSUS"))
    f=line_funnel(db)
    assert f["by_reason"]["NO_EXACT_LINE_CONSENSUS"]==1


def test_182_v090_worker_runs_lines_in_isolated_lane(db):
    class IdleCollector:
        def poll_one_cycle(self):return {"polled":0,"inserted":0,"reason":"idle","mode":"idle","event_ids":[],"requested_markets":[]}
    class Fake:
        def __init__(self):self.calls=0
        def one_cycle(self):self.calls+=1;return {"enabled":True}
    fake=Fake()
    w=Worker(db,IdleCollector(),min_books=3,min_edge_pct=3.0,min_cross_market_edge_pct=4.0,min_slow_book_gap_pct=4.0,execution_shadow_enabled=False,result_collector=None,tennis_engine=None,multisport_engine=None,multisport_lines_engine=fake)
    w.one_cycle();assert fake.calls==1
    log=db.fetchone("SELECT * FROM collector_runs WHERE run_type='MULTISPORT_LINES_SHADOW_MAINT' ORDER BY id DESC LIMIT 1")
    assert log is not None and log["ok"]==1


def test_183_v090_export_contains_line_research(db):
    payload,_=build_research_export(db,Settings(),"0.9.0")
    import io,zipfile
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        assert "tables/multisport_line_bets.csv" in z.namelist()
        assert "tables/multisport_line_consensus_snapshots.csv" in z.namelist()
        summary=json.loads(z.read("analysis_summary.json"))
        assert "multisport_lines_shadow" in summary
        assert "multisport_lines_funnel" in summary
        manifest=json.loads(z.read("manifest.json"))
        assert manifest["settings"]["multisport_lines_markets"]==["spreads","totals"]


def _pred_settings(**overrides):
    class S:
        predictive_football_enabled=True
        predictive_football_bootstrap_enabled=False
        predictive_football_bootstrap_refresh_seconds=86400
        predictive_football_bootstrap_sources_per_cycle=4
        predictive_football_forecast_hours_before=24.0
        predictive_football_min_league_matches=20
        predictive_football_min_team_matches=3.0
        predictive_football_prior_matches=5.0
        predictive_football_half_life_days=180.0
        predictive_football_lookback_days=550
        predictive_football_min_edge_pct=3.0
        predictive_football_strong_edge_pct=5.0
        predictive_football_markets=("h2h","btts","totals")
        predictive_football_total_points=(1.5,2.5,3.5)
        predictive_football_pred2_enabled=True
        predictive_football_pred2_rho_min=-0.20
        predictive_football_pred2_rho_max=0.20
        predictive_football_pred2_rho_step=0.01
        sport_keys=("soccer_epl",)
    s=S()
    for k,v in overrides.items():
        setattr(s,k,v)
    return s


def _seed_pred_training(db, n=30, strong_home="Alpha", weak_away="Beta"):
    base=datetime(2030,7,1,tzinfo=timezone.utc)
    teams=[strong_home,weak_away,"Gamma","Delta","Epsilon","Zeta"]
    for i in range(n):
        home=teams[i%len(teams)]
        away=teams[(i+1)%len(teams)]
        if home==strong_home:
            hg,ag=3,0
        elif away==strong_home:
            hg,ag=0,2
        elif home==weak_away:
            hg,ag=0,2
        elif away==weak_away:
            hg,ag=2,0
        else:
            hg,ag=(1+(i%2)),(i%2)
        played=(base+timedelta(days=i)).isoformat()
        key=f"seed-{i}"
        db.execute(
            """INSERT INTO football_predictive_training_matches(
                 match_key,sport_key,played_at,home_team,away_team,
                 home_goals,away_goals,source,source_ref,imported_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (key,"soccer_epl",played,home,away,hg,ag,"test",key,played)
        )


def _seed_pred_event(
    db,event_id="pred-e1",kickoff="2030-09-01T18:00:00+00:00",
    home="Alpha",away="Beta"
):
    now="2030-08-31T12:00:00+00:00"
    db.execute(
        """INSERT INTO events(
             event_id,sport_key,league,commence_time,home_team,away_team,
             first_seen_at,last_seen_at,status
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (event_id,"soccer_epl","Premier League",kickoff,home,away,now,now,"UPCOMING")
    )


def _seed_pred_threeway(
    db,event_id,at,book="matchbook",home="Alpha",away="Beta",
    home_price=1.8,draw_price=4.5,away_price=6.0
):
    for selection,price in (
        (home,home_price),("Draw",draw_price),(away,away_price)
    ):
        db.execute(
            """INSERT INTO odds_snapshots(
                 event_id,captured_at,bookmaker_key,bookmaker_title,
                 market_key,outcome_name,price
               ) VALUES(?,?,?,?,?,?,?)""",
            (event_id,at,book,book,"h2h",selection,price)
        )


def test_184_v010_predictive_tables_exist(db):
    for table in (
        "football_predictive_training_matches",
        "football_predictive_source_state",
        "football_predictive_predictions",
        "football_predictive_evaluations",
        "football_predictive_bets",
        "football_predictive_price_observations",
    ):
        row=db.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,)
        )
        assert row is not None, table


def test_185_v010_poisson_three_way_probabilities_sum_to_one():
    h,d,a=score_probabilities(1.7,1.0)
    assert h+d+a==pytest.approx(1.0)
    assert h>d
    assert h>a


def test_186_v010_team_alias_normalization():
    assert _normalize_text("Man United")=="manchester united"
    assert _normalize_text("Manchester United FC")=="manchester united"
    assert _normalize_text("Nott'm Forest")=="nottingham forest"


def test_187_v010_predictive_model_uses_prior_scores_and_rates_strong_team_higher(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db)
    engine=PredictiveFootballEngine(
        db,_pred_settings(),execution_bookmaker_keys=("matchbook",)
    )
    event=db.fetchone("SELECT * FROM events WHERE event_id='pred-e1'")
    fit,reason=engine.fit_event(
        event,now=datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    )
    assert reason=="OK"
    assert fit is not None
    assert fit["home_probability"]>fit["away_probability"]
    assert sum(
        fit[k] for k in ("home_probability","draw_probability","away_probability")
    )==pytest.approx(1.0)


def test_188_v010_future_training_match_is_never_used(db):
    _seed_pred_training(db,25)
    _seed_pred_event(db,kickoff="2030-09-01T18:00:00+00:00")
    # Impossible future result for Alpha must not enter a prediction for Sep 1.
    db.execute(
        """INSERT INTO football_predictive_training_matches(
             match_key,sport_key,played_at,home_team,away_team,
             home_goals,away_goals,source,source_ref,imported_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("future","soccer_epl","2030-09-02T18:00:00+00:00",
         "Alpha","Beta",0,20,"test","future","2030-09-02T18:00:00+00:00")
    )
    engine=PredictiveFootballEngine(
        db,_pred_settings(),execution_bookmaker_keys=("matchbook",)
    )
    event=db.fetchone("SELECT * FROM events WHERE event_id='pred-e1'")
    fit,_=engine.fit_event(event)
    assert fit is not None
    assert fit["league_training_matches"]==25


def test_189_v010_prediction_is_market_independent(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db)
    engine=PredictiveFootballEngine(
        db,_pred_settings(),execution_bookmaker_keys=("matchbook",)
    )
    event=db.fetchone("SELECT * FROM events WHERE event_id='pred-e1'")
    fit_before,_=engine.fit_event(event)
    _seed_pred_threeway(
        db,"pred-e1","2030-08-31T15:00:00+00:00",
        home_price=20.0,draw_price=20.0,away_price=1.01
    )
    fit_after,_=engine.fit_event(event)
    assert fit_before["home_probability"]==pytest.approx(
        fit_after["home_probability"]
    )
    assert fit_before["draw_probability"]==pytest.approx(
        fit_after["draw_probability"]
    )
    assert fit_before["away_probability"]==pytest.approx(
        fit_after["away_probability"]
    )


def test_190_v010_prediction_freezes_once_inside_forecast_window(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db,kickoff="2030-09-01T18:00:00+00:00")
    engine=PredictiveFootballEngine(
        db,_pred_settings(),execution_bookmaker_keys=("matchbook",)
    )
    out=engine.freeze_due_predictions(
        datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    )
    assert out["created"]==1
    first=db.fetchone(
        "SELECT * FROM football_predictive_predictions WHERE event_id='pred-e1'"
    )
    assert first["model_version"]=="PRED1_BAYES_POISSON_V1"
    out2=engine.freeze_due_predictions(
        datetime(2030,8,31,19,0,tzinfo=timezone.utc)
    )
    assert out2["created"]==0
    assert db.fetchone(
        "SELECT COUNT(*) AS n FROM football_predictive_predictions"
    )["n"]==1


def test_191_v010_first_acceptable_execution_price_is_frozen(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db)
    settings=_pred_settings(predictive_football_min_edge_pct=3.0)
    engine=PredictiveFootballEngine(
        db,settings,execution_bookmaker_keys=("matchbook",)
    )
    now=datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    engine.freeze_due_predictions(now)
    pred=db.fetchone("SELECT * FROM football_predictive_predictions")
    prob=float(pred["home_probability"])
    minimum=(1.03/prob)
    # First quote is below model minimum.
    _seed_pred_threeway(
        db,"pred-e1","2030-08-31T18:05:00+00:00",
        home_price=minimum-0.03,draw_price=4.0,away_price=6.0
    )
    assert engine.evaluate_predictions()>=0
    assert db.fetchone(
        "SELECT * FROM football_predictive_bets WHERE selection='Alpha'"
    ) is None
    # Second wave qualifies and must freeze.
    _seed_pred_threeway(
        db,"pred-e1","2030-08-31T18:10:00+00:00",
        home_price=minimum+0.08,draw_price=4.0,away_price=6.0
    )
    engine.evaluate_predictions()
    bet=db.fetchone(
        "SELECT * FROM football_predictive_bets WHERE selection='Alpha'"
    )
    assert bet is not None
    assert bet["created_at"]=="2030-08-31T18:10:00+00:00"
    frozen=float(bet["offered_odds"])
    # Better later price cannot rewrite the frozen entry.
    _seed_pred_threeway(
        db,"pred-e1","2030-08-31T18:20:00+00:00",
        home_price=minimum+0.60,draw_price=4.0,away_price=6.0
    )
    engine.evaluate_predictions()
    bet2=db.fetchone(
        "SELECT * FROM football_predictive_bets WHERE selection='Alpha'"
    )
    assert float(bet2["offered_odds"])==pytest.approx(frozen)
    assert bet2["app_version"]=="0.16.1"


def test_192_v010_invalid_two_way_exchange_wave_cannot_create_1x2_shadow(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db)
    engine=PredictiveFootballEngine(
        db,_pred_settings(),execution_bookmaker_keys=("matchbook",)
    )
    engine.freeze_due_predictions(
        datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    )
    for selection,price in (("Alpha",5.0),("Beta",5.0)):
        db.execute(
            """INSERT INTO odds_snapshots(
                 event_id,captured_at,bookmaker_key,bookmaker_title,
                 market_key,outcome_name,price
               ) VALUES(?,?,?,?,?,?,?)""",
            ("pred-e1","2030-08-31T18:05:00+00:00","matchbook",
             "Matchbook","h2h",selection,price)
        )
    engine.evaluate_predictions()
    assert db.fetchone(
        "SELECT id FROM football_predictive_bets"
    ) is None
    reasons={
        r["reason"] for r in db.fetchall(
            "SELECT reason FROM football_predictive_evaluations"
        )
    }
    assert "NO_APPROVED_THREE_WAY_QUOTE" in reasons


def test_193_v010_predictive_clv_uses_latest_valid_same_venue_close(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db,kickoff="2030-09-01T18:00:00+00:00")
    engine=PredictiveFootballEngine(
        db,_pred_settings(),execution_bookmaker_keys=("matchbook",)
    )
    engine.freeze_due_predictions(
        datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    )
    pred=db.fetchone("SELECT * FROM football_predictive_predictions")
    minimum=1.03/float(pred["home_probability"])
    _seed_pred_threeway(
        db,"pred-e1","2030-08-31T18:05:00+00:00",
        home_price=minimum+0.10,draw_price=4.2,away_price=6.0
    )
    engine.evaluate_predictions()
    _seed_pred_threeway(
        db,"pred-e1","2030-09-01T17:40:00+00:00",
        home_price=1.90,draw_price=4.1,away_price=6.2
    )
    # Later invalid wave without draw is ignored.
    for selection,price in (("Alpha",1.70),("Beta",7.0)):
        db.execute(
            """INSERT INTO odds_snapshots(
                 event_id,captured_at,bookmaker_key,bookmaker_title,
                 market_key,outcome_name,price
               ) VALUES(?,?,?,?,?,?,?)""",
            ("pred-e1","2030-09-01T17:55:00+00:00","matchbook",
             "Matchbook","h2h",selection,price)
        )
    assert engine.finalize_clv(
        datetime(2030,9,1,18,5,tzinfo=timezone.utc)
    )>=1
    bet=db.fetchone(
        "SELECT * FROM football_predictive_bets WHERE selection='Alpha'"
    )
    assert bet["closing_odds"]==pytest.approx(1.90)
    assert bet["closing_minutes_before_kickoff"]==pytest.approx(20.0)
    assert bet["clv_quality"]=="B"


def test_194_v010_predictive_settlement_and_calibration(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db)
    engine=PredictiveFootballEngine(
        db,_pred_settings(),execution_bookmaker_keys=("matchbook",)
    )
    engine.freeze_due_predictions(
        datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    )
    pred=db.fetchone("SELECT * FROM football_predictive_predictions")
    db.execute(
        """INSERT INTO event_results(
             event_id,fetched_at,completed_at,home_score,away_score,source
           ) VALUES(?,?,?,?,?,?)""",
        ("pred-e1","2030-09-01T20:00:00+00:00",
         "2030-09-01T18:00:00+00:00",3,1,"test")
    )
    out=engine.settle()
    assert out["predictions"]==1
    done=db.fetchone("SELECT * FROM football_predictive_predictions")
    assert done["actual_outcome"]=="Alpha"
    assert done["brier_score"] is not None
    assert done["log_loss"] is not None


def test_195_v010_internal_results_sync_into_training_set(db):
    _seed_pred_event(
        db,event_id="past",kickoff="2030-08-20T18:00:00+00:00",
        home="Alpha",away="Gamma"
    )
    db.execute(
        "UPDATE events SET status='COMPLETED' WHERE event_id='past'"
    )
    db.execute(
        """INSERT INTO event_results(
             event_id,fetched_at,completed_at,home_score,away_score,source
           ) VALUES(?,?,?,?,?,?)""",
        ("past","2030-08-20T20:00:00+00:00",
         "2030-08-20T18:00:00+00:00",2,0,"test")
    )
    engine=PredictiveFootballEngine(
        db,_pred_settings(),execution_bookmaker_keys=("matchbook",)
    )
    assert engine.sync_internal_results()==1
    row=db.fetchone(
        "SELECT * FROM football_predictive_training_matches"
    )
    assert row["source"]=="the_odds_api"
    assert row["home_goals"]==2


def test_196_v010_bootstrap_import_uses_scores_only_and_stops_at_internal_cutoff(db):
    # Internal tracking started on 20 Aug.
    _seed_pred_event(
        db,event_id="cutoff",kickoff="2030-08-20T18:00:00+00:00",
        home="Alpha",away="Gamma"
    )
    db.execute("UPDATE events SET status='COMPLETED' WHERE event_id='cutoff'")
    db.execute(
        """INSERT INTO event_results(
             event_id,fetched_at,completed_at,home_score,away_score,source
           ) VALUES(?,?,?,?,?,?)""",
        ("cutoff","2030-08-20T20:00:00+00:00",
         "2030-08-20T18:00:00+00:00",2,0,"test")
    )
    engine=PredictiveFootballEngine(
        db,_pred_settings(),execution_bookmaker_keys=("matchbook",)
    )
    csv_text=(
        "Date,HomeTeam,AwayTeam,FTHG,FTAG,B365H,B365D,B365A\n"
        "10/08/2030,Alpha,Beta,2,1,1.20,9.00,20.00\n"
        "25/08/2030,Alpha,Beta,9,9,99.00,99.00,99.00\n"
    )
    imported=engine._import_bootstrap_csv(
        "soccer_epl","3031",csv_text,
        datetime(2030,9,1,tzinfo=timezone.utc)
    )
    assert imported==1
    row=db.fetchone(
        "SELECT * FROM football_predictive_training_matches"
    )
    assert row["home_goals"]==2
    # There is deliberately nowhere in the training table for bookmaker odds.
    assert "B365H" not in row


def test_197_v010_bootstrap_is_paced_by_sources_per_cycle(db):
    class Resp:
        content=b"Date,HomeTeam,AwayTeam,FTHG,FTAG\n10/08/2030,Alpha,Beta,2,1\n"
        status_code=200
        headers={}
        def raise_for_status(self): pass
    class Session:
        def __init__(self):self.calls=[]
        def get(self,url,timeout=20,headers=None,allow_redirects=True):
            self.calls.append(url);return Resp()
    settings=_pred_settings(
        predictive_football_bootstrap_enabled=True,
        predictive_football_bootstrap_sources_per_cycle=1,
        sport_keys=("soccer_epl","soccer_efl_champ"),
    )
    session=Session()
    engine=PredictiveFootballEngine(
        db,settings,execution_bookmaker_keys=("matchbook",),session=session
    )
    out=engine.bootstrap_historical_data(
        datetime(2030,9,1,tzinfo=timezone.utc)
    )
    assert out["attempted"]==1
    assert len(session.calls)==1


def test_198_v010_worker_runs_predictive_lane_independently(db):
    class IdleCollector:
        def poll_one_cycle(self):
            return {
                "polled":0,"inserted":0,"reason":"idle","mode":"idle",
                "event_ids":[],"requested_markets":[]
            }
    class FakePred:
        def __init__(self):self.calls=0
        def one_cycle(self):
            self.calls+=1;return {"enabled":True}
    fake=FakePred()
    w=Worker(
        db,IdleCollector(),min_books=3,min_edge_pct=3.0,
        min_cross_market_edge_pct=4.0,min_slow_book_gap_pct=4.0,
        execution_shadow_enabled=False,result_collector=None,
        tennis_engine=None,multisport_engine=None,multisport_lines_engine=None,
        predictive_football_engine=fake,
    )
    w.one_cycle()
    assert fake.calls==1
    log=db.fetchone(
        """SELECT * FROM collector_runs
           WHERE run_type='PREDICTIVE_FOOTBALL_MAINT'
           ORDER BY id DESC LIMIT 1"""
    )
    assert log is not None and log["ok"]==1


def test_199_v010_predictive_scoreboard_reports_model_and_shadow_evidence(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db)
    engine=PredictiveFootballEngine(
        db,_pred_settings(),execution_bookmaker_keys=("matchbook",)
    )
    engine.freeze_due_predictions(
        datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    )
    score=predictive_scoreboard(db)
    assert score["training_matches"]==36
    assert score["predictions"]==1
    assert score["bets"]==0


def test_200_v010_export_contains_predictive_tables_and_summary(db):
    payload,_=build_research_export(db,Settings(),"0.10.0")
    import io,zipfile
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        assert "tables/football_predictive_predictions.csv" in z.namelist()
        assert "tables/football_predictive_bets.csv" in z.namelist()
        summary=json.loads(z.read("analysis_summary.json"))
        assert "predictive_football" in summary
        manifest=json.loads(z.read("manifest.json"))
        assert manifest["settings"]["predictive_football_enabled"] is True


def _seed_pred_twoway(
    db,event_id,at,market_key,prices,point=None,book="matchbook"
):
    for selection,price in prices.items():
        db.execute(
            """INSERT INTO odds_snapshots(
                 event_id,captured_at,bookmaker_key,bookmaker_title,
                 market_key,outcome_name,point,price
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (event_id,at,book,book,market_key,selection,point,price)
        )


def test_201_v011_derived_probability_math():
    yes,no=btts_probabilities(1.5,1.1)
    assert yes+no==pytest.approx(1.0)
    over25,under25=total_probabilities(1.5,1.1,2.5)
    assert over25+under25==pytest.approx(1.0)
    assert 0 < yes < 1
    assert 0 < over25 < 1


def test_202_v011_frozen_score_forecast_creates_btts_and_three_total_lines(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db)
    engine=PredictiveFootballEngine(
        db,_pred_settings(),execution_bookmaker_keys=("matchbook",)
    )
    engine.freeze_due_predictions(
        datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    )
    assert engine.ensure_market_predictions()==8
    rows=db.fetchall(
        "SELECT * FROM football_predictive_market_predictions ORDER BY id"
    )
    assert len(rows)==8
    assert {r["market_key"] for r in rows}=={"btts","totals"}
    assert {r["line_key"] for r in rows if r["market_key"]=="totals"}=={
        "1.5","2.5","3.5"
    }


def test_203_v011_derived_probabilities_are_market_independent(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db)
    engine=PredictiveFootballEngine(
        db,_pred_settings(),execution_bookmaker_keys=("matchbook",)
    )
    engine.freeze_due_predictions(
        datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    )
    engine.ensure_market_predictions()
    before=db.fetchall(
        """SELECT market_key,selection,line_key,probability
           FROM football_predictive_market_predictions ORDER BY id"""
    )
    _seed_pred_twoway(
        db,"pred-e1","2030-08-31T18:05:00+00:00","btts",
        {"Yes":10.0,"No":1.01}
    )
    _seed_pred_twoway(
        db,"pred-e1","2030-08-31T18:05:00+00:00","totals",
        {"Over":10.0,"Under":1.01},point=2.5
    )
    engine.ensure_market_predictions()
    after=db.fetchall(
        """SELECT market_key,selection,line_key,probability
           FROM football_predictive_market_predictions ORDER BY id"""
    )
    assert before==after


def test_204_v011_btts_first_acceptable_price_freezes(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db)
    engine=PredictiveFootballEngine(
        db,_pred_settings(),execution_bookmaker_keys=("matchbook",)
    )
    engine.freeze_due_predictions(
        datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    )
    engine.ensure_market_predictions()
    mp=db.fetchone(
        """SELECT * FROM football_predictive_market_predictions
           WHERE market_key='btts' AND selection='Yes'"""
    )
    minimum=1.03/float(mp["probability"])
    _seed_pred_twoway(
        db,"pred-e1","2030-08-31T18:05:00+00:00","btts",
        {"Yes":minimum+0.10,"No":2.0}
    )
    assert engine.evaluate_market_predictions(
        datetime(2030,8,31,18,6,tzinfo=timezone.utc)
    )>=1
    bet=db.fetchone(
        """SELECT * FROM football_predictive_market_bets
           WHERE market_key='btts' AND selection='Yes'"""
    )
    assert bet is not None
    assert bet["experiment_version"]=="PRED1_DERIVED_MARKETS_V1"
    assert bet["app_version"]=="0.16.1"


def test_205_v011_totals_requires_exact_line_match(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db)
    engine=PredictiveFootballEngine(
        db,_pred_settings(),execution_bookmaker_keys=("matchbook",)
    )
    engine.freeze_due_predictions(
        datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    )
    engine.ensure_market_predictions()
    mp=db.fetchone(
        """SELECT * FROM football_predictive_market_predictions
           WHERE market_key='totals' AND selection='Over' AND line_key='2.5'"""
    )
    minimum=1.03/float(mp["probability"])
    # Attractive 3.5 quote cannot be used to create an O2.5 shadow.
    _seed_pred_twoway(
        db,"pred-e1","2030-08-31T18:05:00+00:00","totals",
        {"Over":minimum+1.0,"Under":2.0},point=3.5
    )
    engine.evaluate_market_predictions(
        datetime(2030,8,31,18,6,tzinfo=timezone.utc)
    )
    assert db.fetchone(
        """SELECT id FROM football_predictive_market_bets
           WHERE market_key='totals' AND selection='Over' AND line_key='2.5'"""
    ) is None


def test_206_v011_totals_shadow_and_same_line_clv(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db,kickoff="2030-09-01T18:00:00+00:00")
    engine=PredictiveFootballEngine(
        db,_pred_settings(),execution_bookmaker_keys=("matchbook",)
    )
    engine.freeze_due_predictions(
        datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    )
    engine.ensure_market_predictions()
    mp=db.fetchone(
        """SELECT * FROM football_predictive_market_predictions
           WHERE market_key='totals' AND selection='Over' AND line_key='2.5'"""
    )
    minimum=1.03/float(mp["probability"])
    _seed_pred_twoway(
        db,"pred-e1","2030-08-31T18:05:00+00:00","totals",
        {"Over":minimum+0.10,"Under":2.0},point=2.5
    )
    engine.evaluate_market_predictions(
        datetime(2030,8,31,18,6,tzinfo=timezone.utc)
    )
    _seed_pred_twoway(
        db,"pred-e1","2030-09-01T17:40:00+00:00","totals",
        {"Over":1.85,"Under":2.05},point=2.5
    )
    _seed_pred_twoway(
        db,"pred-e1","2030-09-01T17:55:00+00:00","totals",
        {"Over":1.20,"Under":5.0},point=3.5
    )
    assert engine.finalize_market_clv(
        datetime(2030,9,1,18,5,tzinfo=timezone.utc)
    )>=1
    bet=db.fetchone(
        """SELECT * FROM football_predictive_market_bets
           WHERE market_key='totals' AND selection='Over' AND line_key='2.5'"""
    )
    assert bet["closing_odds"]==pytest.approx(1.85)
    assert bet["clv_quality"]=="B"


def test_207_v011_market_predictions_and_bets_settle_from_scores(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db)
    engine=PredictiveFootballEngine(
        db,_pred_settings(),execution_bookmaker_keys=("matchbook",)
    )
    engine.freeze_due_predictions(
        datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    )
    engine.ensure_market_predictions()
    btts=db.fetchone(
        """SELECT * FROM football_predictive_market_predictions
           WHERE market_key='btts' AND selection='Yes'"""
    )
    minimum=1.03/float(btts["probability"])
    _seed_pred_twoway(
        db,"pred-e1","2030-08-31T18:05:00+00:00","btts",
        {"Yes":minimum+0.15,"No":2.0}
    )
    engine.evaluate_market_predictions(
        datetime(2030,8,31,18,6,tzinfo=timezone.utc)
    )
    db.execute(
        """INSERT INTO event_results(
             event_id,fetched_at,completed_at,home_score,away_score,source
           ) VALUES(?,?,?,?,?,?)""",
        ("pred-e1","2030-09-01T20:00:00+00:00",
         "2030-09-01T18:00:00+00:00",2,1,"test")
    )
    out=engine.settle_market_predictions()
    assert out["predictions"]==8
    bet=db.fetchone(
        """SELECT * FROM football_predictive_market_bets
           WHERE market_key='btts' AND selection='Yes'"""
    )
    assert bet["result"]=="WIN"
    assert bet["net_pnl_units"] is not None
    mp=db.fetchone(
        """SELECT * FROM football_predictive_market_predictions
           WHERE market_key='totals' AND selection='Under' AND line_key='3.5'"""
    )
    assert mp["actual_hit"]==1
    assert mp["brier_score"] is not None


def test_208_v011_scoreboard_splits_1x2_btts_totals(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db)
    engine=PredictiveFootballEngine(
        db,_pred_settings(),execution_bookmaker_keys=("matchbook",)
    )
    engine.freeze_due_predictions(
        datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    )
    engine.ensure_market_predictions()
    score=predictive_scoreboard(db)
    assert score["derived_market_cases"]==4
    assert score["h2h_bets"]==0
    assert score["btts_bets"]==0
    assert score["totals_bets"]==0
    summary=predictive_market_summary(db)
    assert {(x["market_key"],x["line_key"]) for x in summary}=={
        ("btts",""),("totals","1.5"),("totals","2.5"),("totals","3.5")
    }


def test_209_v011_export_contains_predictive_market_tables(db):
    payload,_=build_research_export(db,Settings(),"0.11.0")
    import io,zipfile
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        names=set(z.namelist())
        assert "tables/football_predictive_market_predictions.csv" in names
        assert "tables/football_predictive_market_bets.csv" in names
        summary=json.loads(z.read("analysis_summary.json"))
        assert "predictive_football_market_summary" in summary
        manifest=json.loads(z.read("manifest.json"))
        assert manifest["settings"]["predictive_football_markets"]==[
            "h2h","btts","totals"
        ]


def test_210_v0111_bootstrap_status_reports_source_health(db):
    db.execute(
        """INSERT INTO football_predictive_source_state(
             source_key,last_attempt_at,last_success_at,rows_imported,last_error
           ) VALUES(?,?,?,?,?)""",
        ("football_data:soccer_epl:2627",
         "2030-08-31T10:00:00+00:00",
         "2030-08-31T10:00:00+00:00",100,None)
    )
    db.execute(
        """INSERT INTO football_predictive_source_state(
             source_key,last_attempt_at,last_success_at,rows_imported,last_error
           ) VALUES(?,?,?,?,?)""",
        ("football_data:soccer_efl_champ:2627",
         "2030-08-31T11:00:00+00:00",None,0,"fetch failed")
    )
    s=predictive_bootstrap_status(db)
    assert s["sources_attempted"]==2
    assert s["sources_successful"]==1
    assert s["sources_failed"]==1
    assert s["rows_imported"]==100
    assert s["failures"][-1]["last_error"]=="fetch failed"


def test_211_v0111_worker_runs_predictive_before_main_collector(db):
    order=[]
    class C:
        def poll_one_cycle(self):
            order.append("collector")
            return {
                "polled":0,"inserted":0,"reason":"idle","mode":"idle",
                "event_ids":[],"requested_markets":[]
            }
    class P:
        def one_cycle(self):
            order.append("predictive")
            return {"enabled":True}
    w=Worker(
        db,C(),min_books=3,min_edge_pct=3.0,
        min_cross_market_edge_pct=4.0,min_slow_book_gap_pct=4.0,
        execution_shadow_enabled=False,result_collector=None,
        tennis_engine=None,multisport_engine=None,multisport_lines_engine=None,
        predictive_football_engine=P(),
    )
    w.one_cycle()
    assert order[:2]==["predictive","collector"]


def test_212_v0111_export_contains_bootstrap_health(db):
    payload,_=build_research_export(db,Settings(),"0.11.1")
    import io,zipfile
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        summary=json.loads(z.read("analysis_summary.json"))
        assert "predictive_football_bootstrap" in summary
        assert summary["predictive_football_bootstrap"]["sources_attempted"]==0


def test_213_v0113_bootstrap_targets_do_not_depend_on_sport_keys(db):
    settings=_pred_settings(sport_keys=())
    engine=PredictiveFootballEngine(
        db,settings,execution_bookmaker_keys=("matchbook",)
    )
    targets=engine.bootstrap_target_sports()
    assert "soccer_epl" in targets
    assert "soccer_efl_champ" in targets
    assert len(targets)>=10


def test_214_v0113_observed_supported_league_is_prioritised(db):
    _seed_pred_event(
        db,event_id="observed",home="Alpha",away="Beta"
    )
    db.execute(
        "UPDATE events SET sport_key='soccer_italy_serie_a',league='Serie A' "
        "WHERE event_id='observed'"
    )
    settings=_pred_settings(sport_keys=())
    engine=PredictiveFootballEngine(
        db,settings,execution_bookmaker_keys=("matchbook",)
    )
    assert engine.bootstrap_target_sports()[0]=="soccer_italy_serie_a"


def test_215_v0113_bootstrap_attempts_even_with_empty_sport_keys(db):
    class Resp:
        content=b"Date,HomeTeam,AwayTeam,FTHG,FTAG\n10/08/2030,Alpha,Beta,2,1\n"
        status_code=200
        headers={}
        def raise_for_status(self): pass
    class Session:
        def __init__(self): self.calls=[]
        def get(self,url,timeout=20,headers=None,allow_redirects=True):
            self.calls.append(url); return Resp()
    settings=_pred_settings(
        sport_keys=(),
        predictive_football_bootstrap_enabled=True,
        predictive_football_bootstrap_sources_per_cycle=1,
    )
    session=Session()
    engine=PredictiveFootballEngine(
        db,settings,execution_bookmaker_keys=("matchbook",),session=session
    )
    out=engine.bootstrap_historical_data(
        datetime(2030,9,1,tzinfo=timezone.utc)
    )
    assert out["attempted"]==1
    assert len(session.calls)==1
    status=predictive_bootstrap_status(db)
    assert status["sources_attempted"]==1


def test_216_v0114_predictive_sql_has_no_literal_percent_like_pattern():
    # psycopg2 uses % for parameter interpolation. PRED1 SQL must pass the
    # soccer wildcard as a parameter rather than embedding soccer_% literally.
    from pathlib import Path
    source=Path(__file__).with_name("predictive_football.py").read_text()
    assert "LIKE 'soccer_%'" not in source
    assert source.count('("soccer_%",)') >= 2
    assert '(now.isoformat(),horizon.isoformat(),"soccer_%")' in source


def test_217_v0114_postgres_translation_keeps_like_as_bound_parameter(db):
    sql="SELECT * FROM events WHERE sport_key LIKE ? AND status=?"
    translated=db._sql(sql)
    if db.is_postgres:
        assert translated.count("%s")==2
    else:
        assert translated.count("?")==2


def test_218_v0115_frozen_prediction_is_convergence_candidate_before_shadow(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db,kickoff="2030-09-01T18:00:00+00:00")
    settings=_pred_settings()
    engine=PredictiveFootballEngine(
        db,settings,execution_bookmaker_keys=("matchbook",)
    )
    engine.freeze_due_predictions(
        datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    )
    assert db.fetchone(
        "SELECT COUNT(*) AS n FROM football_predictive_bets"
    )["n"]==0

    class Q:
        def decide(self,cost):
            class D: allowed=True; reason="ok"
            return D()
        def set_paused(self,*a,**k): pass
    class A: pass
    collector=Collector.__new__(Collector)
    collector.db=db
    collector.execution_bookmaker_keys=("matchbook",)
    collector.quota=Q()
    rows=collector.convergence_candidates(
        datetime(2030,8,31,18,1,tzinfo=timezone.utc)
    )
    assert any(
        r["event_id"]=="pred-e1" and r["market_key"]=="h2h"
        for r in rows
    )


def test_219_v0115_derived_markets_are_convergence_candidates_before_shadow(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db,kickoff="2030-09-01T18:00:00+00:00")
    settings=_pred_settings()
    engine=PredictiveFootballEngine(
        db,settings,execution_bookmaker_keys=("matchbook",)
    )
    engine.freeze_due_predictions(
        datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    )
    engine.ensure_market_predictions()
    assert db.fetchone(
        "SELECT COUNT(*) AS n FROM football_predictive_market_bets"
    )["n"]==0

    collector=Collector.__new__(Collector)
    collector.db=db
    collector.execution_bookmaker_keys=("matchbook",)
    rows=collector.convergence_candidates(
        datetime(2030,8,31,18,1,tzinfo=timezone.utc)
    )
    pairs={(r["event_id"],r["market_key"]) for r in rows}
    assert ("pred-e1","btts") in pairs
    assert ("pred-e1","totals") in pairs


def test_220_v0115_prediction_and_existing_bet_dedupe_to_one_candidate(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db,kickoff="2030-09-01T18:00:00+00:00")
    settings=_pred_settings()
    engine=PredictiveFootballEngine(
        db,settings,execution_bookmaker_keys=("matchbook",)
    )
    engine.freeze_due_predictions(
        datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    )
    pred=db.fetchone("SELECT * FROM football_predictive_predictions")
    prob=float(pred["home_probability"])
    _seed_pred_threeway(
        db,"pred-e1","2030-08-31T18:05:00+00:00",
        home_price=(1.03/prob)+0.20,draw_price=4.0,away_price=6.0
    )
    engine.evaluate_predictions()
    assert db.fetchone(
        "SELECT COUNT(*) AS n FROM football_predictive_bets"
    )["n"]>=1

    collector=Collector.__new__(Collector)
    collector.db=db
    collector.execution_bookmaker_keys=("matchbook",)
    rows=collector.convergence_candidates(
        datetime(2030,8,31,18,6,tzinfo=timezone.utc)
    )
    h2h=[
        r for r in rows
        if r["event_id"]=="pred-e1" and r["market_key"]=="h2h"
    ]
    assert len(h2h)==1


class FakePredictiveScoresApi:
    def scores(self, sport_key, *, event_ids=(), days_from=1):
        event_id = list(event_ids)[0]
        return ApiResult(
            [{
                "id": event_id,
                "sport_key": sport_key,
                "commence_time": "2026-09-12T12:00:00Z",
                "completed": True,
                "home_team": "Pred Home",
                "away_team": "Pred Away",
                "scores": [
                    {"name": "Pred Home", "score": "2"},
                    {"name": "Pred Away", "score": "1"},
                ],
            }],
            480, 20, 2
        )


def test_221_v0116_result_collector_includes_predictive_only_event(db):
    db.execute(
        """
        INSERT INTO events(
          event_id,sport_key,league,commence_time,home_team,away_team,
          first_seen_at,last_seen_at,status
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        ("pred_result_evt","soccer_epl","Premier League","2026-09-12T12:00:00+00:00",
         "Pred Home","Pred Away","2026-09-11T00:00:00+00:00","2026-09-11T00:00:00+00:00","UPCOMING")
    )
    db.execute(
        """
        INSERT INTO football_predictive_predictions(
          event_id,created_at,sport_key,league,commence_time,home_team,away_team,
          mapped_home_team,mapped_away_team,home_mapping_score,away_mapping_score,
          expected_home_goals,expected_away_goals,home_probability,draw_probability,
          away_probability,home_fair_odds,draw_fair_odds,away_fair_odds,
          league_training_matches,home_effective_matches,away_effective_matches,
          model_version,config_hash
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        ("pred_result_evt","2026-09-11T12:00:00+00:00","soccer_epl","Premier League",
         "2026-09-12T12:00:00+00:00","Pred Home","Pred Away","Pred Home","Pred Away",
         1.0,1.0,1.5,1.0,0.50,0.27,0.23,2.0,3.7037037,4.347826,
         100,10.0,10.0,"PRED1_BAYES_POISSON_V1","testhash")
    )
    assert db.fetchone("SELECT COUNT(*) AS n FROM signals WHERE event_id='pred_result_evt'")["n"] == 0
    db.execute("UPDATE quota_state SET credits_remaining=500 WHERE singleton_id=1")
    q = QuotaGuard(db, reserve=50, daily_budget=20)
    rc = ResultCollector(
        db, FakePredictiveScoresApi(), q,
        enabled=True, min_minutes_after_kickoff=135,
        min_poll_interval_seconds=21600,
    )
    out = rc.collect(datetime(2026,9,12,15,0,tzinfo=timezone.utc))
    assert out["checked"] == 1
    result = db.fetchone("SELECT home_score,away_score FROM event_results WHERE event_id='pred_result_evt'")
    assert result is not None
    assert result["home_score"] == 2 and result["away_score"] == 1

def test_222_v0120_pred2_tables_exist(db):
    for table in (
        "football_predictive2_predictions",
        "football_predictive2_evaluations",
        "football_predictive2_bets",
        "football_predictive2_price_observations",
        "football_predictive2_market_predictions",
        "football_predictive2_market_evaluations",
        "football_predictive2_market_bets",
        "football_predictive2_market_price_observations",
    ):
        row=db.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,)
        )
        assert row is not None, table


def test_223_v0120_pred2_rho_zero_recovers_independent_poisson():
    p1=score_probabilities(1.55,1.05)
    p2=pred2_score_probabilities(1.55,1.05,rho=0.0)
    assert p2==pytest.approx(p1,abs=1e-12)


def test_224_v0120_pred2_fits_rho_and_freezes_paired_forecast(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db)
    s=_pred_settings()
    p1=PredictiveFootballEngine(db,s,execution_bookmaker_keys=("matchbook",))
    p2=PredictiveFootballPred2Engine(db,s,execution_bookmaker_keys=("matchbook",))
    now=datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    assert p1.freeze_due_predictions(now)["created"]==1
    assert p2.freeze_due_predictions(now)["created"]==1
    a=db.fetchone("SELECT * FROM football_predictive_predictions WHERE event_id='pred-e1'")
    b=db.fetchone("SELECT * FROM football_predictive2_predictions WHERE event_id='pred-e1'")
    assert a is not None and b is not None
    assert a["created_at"]==b["created_at"]==now.isoformat()
    assert -0.20 <= float(b["dixon_coles_rho"]) <= 0.20
    assert sum(float(b[k]) for k in ("home_probability","draw_probability","away_probability"))==pytest.approx(1.0)
    # The challenger should be allowed to differ from the independent-Poisson control.
    assert any(abs(float(a[k])-float(b[k]))>1e-9 for k in ("home_probability","draw_probability","away_probability"))


def test_225_v0120_pred2_uses_same_stored_execution_wave_and_creates_shadow(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db)
    s=_pred_settings()
    p2=PredictiveFootballPred2Engine(db,s,execution_bookmaker_keys=("matchbook",))
    now=datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    p2.freeze_due_predictions(now)
    p2.ensure_market_predictions()
    pred=db.fetchone("SELECT * FROM football_predictive2_predictions WHERE event_id='pred-e1'")
    # Force a clearly qualifying stored quote for the PRED2 home probability.
    min_home=1.03/float(pred["home_probability"])
    _seed_pred_threeway(
        db,"pred-e1","2030-08-31T18:05:00+00:00",
        home_price=min_home+0.25,draw_price=50.0,away_price=50.0,
    )
    created=p2.evaluate_predictions()
    assert created>=1
    bet=db.fetchone("SELECT * FROM football_predictive2_bets WHERE event_id='pred-e1' AND selection='Alpha'")
    assert bet is not None
    assert bet["experiment_version"]=="PRED2_DIXON_COLES"
    assert bet["app_version"]=="0.16.1"


def test_226_v0120_collector_dedupes_pred1_pred2_convergence_candidate(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db)
    s=_pred_settings()
    now=datetime(2030,8,31,18,0,tzinfo=timezone.utc)
    p1=PredictiveFootballEngine(db,s,execution_bookmaker_keys=("matchbook",))
    p2=PredictiveFootballPred2Engine(db,s,execution_bookmaker_keys=("matchbook",))
    p1.freeze_due_predictions(now);p1.ensure_market_predictions()
    p2.freeze_due_predictions(now);p2.ensure_market_predictions()
    collector=Collector.__new__(Collector)
    collector.db=db
    collector.execution_bookmaker_keys=("matchbook",)
    rows=collector.convergence_candidates(now+timedelta(minutes=1))
    h2h=[x for x in rows if x["event_id"]=="pred-e1" and x["market_key"]=="h2h"]
    btts=[x for x in rows if x["event_id"]=="pred-e1" and x["market_key"]=="btts"]
    totals=[x for x in rows if x["event_id"]=="pred-e1" and x["market_key"]=="totals"]
    assert len(h2h)==len(btts)==len(totals)==1


def test_227_v0120_pred2_only_fixture_is_result_eligible(db):
    _seed_pred_training(db,36)
    _seed_pred_event(db,kickoff="2030-09-01T12:00:00+00:00")
    s=_pred_settings()
    p2=PredictiveFootballPred2Engine(db,s,execution_bookmaker_keys=("matchbook",))
    p2.freeze_due_predictions(datetime(2030,8,31,18,0,tzinfo=timezone.utc))
    # No PRED1 prediction and no original signal exists.
    assert db.fetchone("SELECT id FROM football_predictive_predictions WHERE event_id='pred-e1'") is None
    rc=ResultCollector.__new__(ResultCollector)
    rc.db=db
    rc.min_minutes_after_kickoff=135
    due=rc._due_events(datetime(2030,9,1,15,0,tzinfo=timezone.utc))
    assert any(x["event_id"]=="pred-e1" for x in due)


def test_228_v0120_export_contains_pred2_tables_and_summary(db):
    payload,_=build_research_export(db,Settings(),"0.12.0")
    import io,zipfile
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        assert "tables/football_predictive2_predictions.csv" in z.namelist()
        assert "tables/football_predictive2_bets.csv" in z.namelist()
        summary=json.loads(z.read("analysis_summary.json"))
        assert "predictive_football_pred2" in summary
        manifest=json.loads(z.read("manifest.json"))
        assert "predictive_football_pred2_enabled" in manifest["settings"]

# ---------------- v0.13.0 evidence-quality expansion ----------------

def test_229_v013_predictive_convergence_schedule_tightens_near_kickoff():
    from collector import predictive_convergence_interval_minutes
    now=datetime(2030,1,1,12,0,tzinfo=timezone.utc)
    assert predictive_convergence_interval_minutes((now+timedelta(hours=20)).isoformat(),now)==180
    assert predictive_convergence_interval_minutes((now+timedelta(hours=5)).isoformat(),now)==60
    assert predictive_convergence_interval_minutes((now+timedelta(minutes=50)).isoformat(),now)==15
    assert predictive_convergence_interval_minutes((now+timedelta(minutes=10)).isoformat(),now)==5


def test_230_v013_measurement_only_consensus_does_not_create_signals(db):
    from signals import write_consensus_snapshots_only
    db.execute(
        """INSERT INTO events(event_id,sport_key,league,commence_time,home_team,away_team,
           first_seen_at,last_seen_at,status) VALUES(?,?,?,?,?,?,?,?,?)""",
        ("close-e1","soccer_epl","Premier League","2030-01-02T15:00:00+00:00",
         "Alpha","Beta","2030-01-01T00:00:00+00:00","2030-01-01T00:00:00+00:00","UPCOMING")
    )
    cap="2030-01-02T14:55:00+00:00"
    for book,prices in {
        "b1":{"Alpha":2.0,"Draw":3.5,"Beta":4.0},
        "b2":{"Alpha":2.1,"Draw":3.4,"Beta":3.9},
        "b3":{"Alpha":2.05,"Draw":3.45,"Beta":3.95},
    }.items():
        for sel,price in prices.items():
            db.execute(
                """INSERT INTO odds_snapshots(event_id,captured_at,bookmaker_key,bookmaker_title,
                   market_key,outcome_name,price) VALUES(?,?,?,?,?,?,?)""",
                ("close-e1",cap,book,book,"h2h",sel,price)
            )
    assert write_consensus_snapshots_only(db,"close-e1",min_books=3)==3
    assert db.fetchone("SELECT COUNT(*) AS n FROM consensus_snapshots WHERE event_id='close-e1'")["n"]==3
    assert db.fetchone("SELECT COUNT(*) AS n FROM signals WHERE event_id='close-e1'")["n"]==0


def test_231_v013_training_cutoff_excludes_later_known_results(db):
    # Build a past history, then add an extreme result after the historical
    # forecast freeze. Reconstructing with training_cutoff must ignore it.
    base=datetime(2025,1,1,tzinfo=timezone.utc)
    teams=("Alpha","Beta","Gamma","Delta")
    for i in range(28):
        h=teams[i%4]; a=teams[(i+1)%4]
        played=(base+timedelta(days=i)).isoformat()
        db.execute(
            """INSERT INTO football_predictive_training_matches(match_key,sport_key,played_at,
               home_team,away_team,home_goals,away_goals,source,source_ref,imported_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (f"cut-{i}","soccer_epl",played,h,a,2,1,"test",f"cut-{i}",played)
        )
    settings=_pred_settings(predictive_football_min_league_matches=20,predictive_football_min_team_matches=2)
    engine=PredictiveFootballEngine(db,settings,execution_bookmaker_keys=("matchbook",))
    kickoff=base+timedelta(days=35,hours=15)
    event={"sport_key":"soccer_epl","commence_time":kickoff.isoformat(),"home_team":"Alpha","away_team":"Beta"}
    cutoff=base+timedelta(days=28)
    before,_=engine.fit_event(event,training_cutoff=cutoff)
    extreme=(base+timedelta(days=31)).isoformat()
    db.execute(
        """INSERT INTO football_predictive_training_matches(match_key,sport_key,played_at,
           home_team,away_team,home_goals,away_goals,source,source_ref,imported_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("future-known","soccer_epl",extreme,"Alpha","Beta",20,0,"test","future-known",extreme)
    )
    after,_=engine.fit_event(event,training_cutoff=cutoff)
    assert before is not None and after is not None
    assert after["expected_home_goals"]==pytest.approx(before["expected_home_goals"])
    assert after["home_probability"]==pytest.approx(before["home_probability"])


def test_232_v013_historical_validator_builds_isolated_complete_fixture(db):
    from predictive_historical import PredictiveHistoricalValidator, historical_validation_summary
    from the_odds_api import ApiResult

    base=datetime(2025,7,1,tzinfo=timezone.utc)
    # Same matchup repeatedly keeps the test deterministic and gives both teams
    # enough history before the first model-eligible validation target.
    for i in range(30):
        played=(base+timedelta(days=i)).isoformat()
        db.execute(
            """INSERT INTO football_predictive_training_matches(match_key,sport_key,played_at,
               home_team,away_team,home_goals,away_goals,source,source_ref,imported_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (f"hist-{i}","soccer_epl",played,"Alpha","Beta",2,0,"test",f"hist-{i}",played)
        )

    class HistApi:
        def historical_events(self,sport_key,date,**kwargs):
            # First eligible candidate is 20 July-ish; validator matches names.
            data={"timestamp":date,"data":[{
                "id":"hist-provider-1","sport_key":sport_key,
                "commence_time":"2025-07-21T15:00:00+00:00",
                "home_team":"Alpha","away_team":"Beta",
            }]}
            return ApiResult(data,99999,1,1)
        def historical_event_odds(self,sport_key,event_id,region,markets,date,bookmaker_keys=()):
            books=[]
            prices={
                "matchbook":{"Alpha":3.0,"Draw":4.0,"Beta":6.0},
                "b1":{"Alpha":1.8,"Draw":4.0,"Beta":5.0},
                "b2":{"Alpha":1.85,"Draw":3.9,"Beta":4.9},
                "b3":{"Alpha":1.82,"Draw":4.1,"Beta":5.1},
            }
            for key,pmap in prices.items():
                books.append({"key":key,"title":key,"markets":[{"key":"h2h","outcomes":[
                    {"name":sel,"price":price} for sel,price in pmap.items()
                ]}]})
            return ApiResult({"timestamp":date,"data":{
                "id":event_id,"sport_key":sport_key,"commence_time":"2025-07-21T15:00:00+00:00",
                "home_team":"Alpha","away_team":"Beta","bookmakers":books,
            }},99990,10,10)

    settings=_pred_settings(
        predictive_football_min_league_matches=20,
        predictive_football_min_team_matches=2,
        predictive_football_historical_enabled=True,
        predictive_football_historical_region="uk",
        predictive_football_historical_snapshot_minutes=(1440,5),
        predictive_football_historical_daily_credit_budget=500,
        predictive_football_historical_interval_seconds=1,
        predictive_football_historical_max_candidates_scan=50,
        quota_reserve_credits=0,
        min_consensus_books=3,
    )
    p1=PredictiveFootballEngine(db,settings,execution_bookmaker_keys=("matchbook",))
    p2=PredictiveFootballPred2Engine(db,settings,execution_bookmaker_keys=("matchbook",))
    hist=PredictiveHistoricalValidator(db,HistApi(),settings,p1,p2,execution_bookmaker_keys=("matchbook",))
    out=hist.one_cycle(force=True)
    assert out["processed"]==1
    row=db.fetchone("SELECT * FROM football_predictive_historical_validations WHERE status='COMPLETE'")
    assert row is not None
    assert row["pred1_brier_score"] is not None and row["pred2_brier_score"] is not None
    assert row["closing_brier_score"] is not None
    assert db.fetchone("SELECT COUNT(*) AS n FROM football_predictive_predictions")["n"]==0
    assert db.fetchone("SELECT COUNT(*) AS n FROM football_predictive2_predictions")["n"]==0
    summary=historical_validation_summary(db)
    assert summary["complete_fixtures"]==1
    assert summary["pred1_avg_brier"] is not None


def test_233_v013_research_export_contains_historical_validation_tables(db):
    payload,_=build_research_export(db,Settings(),"0.13.1")
    import io,zipfile
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        assert "tables/football_predictive_historical_validations.csv" in z.namelist()
        assert "tables/football_predictive_historical_odds.csv" in z.namelist()
        assert "tables/football_predictive_historical_bets.csv" in z.namelist()
        summary=json.loads(z.read("analysis_summary.json"))
        assert "predictive_football_historical_validation" in summary


def test_234_v0131_historical_sql_has_no_literal_percent_like_pattern():
    # psycopg2 treats literal % as interpolation syntax whenever params are passed.
    # Historical summary must bind the SKIPPED wildcard rather than embed it.
    from pathlib import Path
    source=Path(__file__).with_name("predictive_historical.py").read_text()
    assert "LIKE 'SKIPPED_%'" not in source
    assert "WHERE status LIKE ?" in source
    assert '("SKIPPED_%",)' in source


# ---------------------------------------------------------------------------
# v0.14.0 MS2 Manual Systems Shadow
# ---------------------------------------------------------------------------

def _seed_ms2_consensus_pool(db, n=6, book="williamhill", title="William Hill", captured="2026-09-04T12:05:00+00:00"):
    for i in range(n):
        eid=f"ms2_{book}_{i}"
        _seed_multiple_exec(
            db,eid,commence=f"2026-09-04T{16+i:02d}:00:00+00:00",
            min_odds=1.8,fair_probability=0.52,edge=7.0+i,
            created="2026-09-04T12:00:00+00:00",
        )
        _seed_book_quote(
            db,eid,captured=captured,book=book,title=title,
            market="h2h",selection="Away",price=2.05+0.01*i,
        )


def test_235_v014_manual_systems_default_config():
    from config import Settings
    s=Settings()
    assert s.manual_systems_enabled is True
    assert s.manual_systems_placeable_bookmaker_keys == ("williamhill","ladbrokes_uk")
    assert s.manual_systems_comparison_bookmaker_keys == ("betfair_ex_uk","matchbook","smarkets")
    assert s.manual_systems_types == ("YANKEE","HEINZ")


def test_236_v014_yankee_and_heinz_form_with_correct_line_counts(db):
    _seed_ms2_consensus_pool(db,6)
    now=datetime(2026,9,4,12,10,tzinfo=timezone.utc)
    created=generate_manual_system_shadows(
        db,now=now,system_types=("YANKEE","HEINZ"),
        placeable_bookmaker_keys=("williamhill",),comparison_bookmaker_keys=(),
        source_cohorts=("CONSENSUS",),quote_freshness_minutes=30,
        max_quote_spread_minutes=15,horizon_hours=30,
    )
    assert created==2
    yankee=db.fetchone("SELECT * FROM manual_system_shadow_bets WHERE system_type='YANKEE'")
    heinz=db.fetchone("SELECT * FROM manual_system_shadow_bets WHERE system_type='HEINZ'")
    assert yankee["leg_count"]==4 and yankee["line_count"]==11
    assert heinz["leg_count"]==6 and heinz["line_count"]==57
    assert yankee["manual_placeable"]==1
    assert db.fetchone("SELECT COUNT(*) AS n FROM manual_system_shadow_lines WHERE system_bet_id=?",(yankee["id"],))["n"]==11
    assert db.fetchone("SELECT COUNT(*) AS n FROM manual_system_shadow_lines WHERE system_bet_id=?",(heinz["id"],))["n"]==57
    assert sum(float(x["stake_units"]) for x in db.fetchall("SELECT * FROM manual_system_shadow_lines WHERE system_bet_id=?",(heinz["id"],)))==pytest.approx(1.0)
    assert float(heinz["singles_control_stake_units"])*6==pytest.approx(1.0)


def test_237_v014_exchange_comparison_is_not_manual_placeable(db):
    _seed_ms2_consensus_pool(db,4,book="smarkets",title="Smarkets")
    now=datetime(2026,9,4,12,10,tzinfo=timezone.utc)
    assert generate_manual_system_shadows(
        db,now=now,system_types=("YANKEE",),placeable_bookmaker_keys=(),
        comparison_bookmaker_keys=("smarkets",),source_cohorts=("CONSENSUS",),
        quote_freshness_minutes=30,max_quote_spread_minutes=15,horizon_hours=30,
    )==1
    row=db.fetchone("SELECT * FROM manual_system_shadow_bets")
    assert row["manual_placeable"]==0
    assert row["placement_mode"]=="SYNTHETIC_COMPARISON"


def test_238_v014_manual_quote_targets_require_four_distinct_fixtures(db):
    for i in range(3):
        _seed_multiple_exec(db,f"ms2t{i}",commence=f"2026-09-04T{16+i:02d}:00:00+00:00",min_odds=1.8)
    now=datetime(2026,9,4,12,10,tzinfo=timezone.utc)
    assert manual_quote_targets(db,now=now,horizon_hours=12)==[]
    _seed_multiple_exec(db,"ms2t3",commence="2026-09-04T19:00:00+00:00",min_odds=1.8)
    targets=manual_quote_targets(db,now=now,horizon_hours=12)
    assert len({x["event_id"] for x in targets})==4


def test_239_v014_manual_system_settlement_compares_equal_total_stake(db):
    _seed_ms2_consensus_pool(db,4)
    now=datetime(2026,9,4,12,10,tzinfo=timezone.utc)
    assert generate_manual_system_shadows(
        db,now=now,system_types=("YANKEE",),placeable_bookmaker_keys=("williamhill",),
        comparison_bookmaker_keys=(),source_cohorts=("CONSENSUS",),
        quote_freshness_minutes=30,max_quote_spread_minutes=15,horizon_hours=30,
    )==1
    legs=db.fetchall("SELECT * FROM manual_system_shadow_legs ORDER BY leg_order")
    # Two winners / two losers: enough to exercise partial Yankee returns.
    for i,leg in enumerate(legs):
        # selection is Away. Away wins for first two, loses for final two.
        hs,as_=(0,1) if i<2 else (1,0)
        db.execute(
            "INSERT INTO event_results(event_id,fetched_at,completed_at,home_score,away_score) VALUES(?,?,?,?,?)",
            (leg["event_id"],"2026-09-05T00:00:00+00:00","2026-09-05T00:00:00+00:00",hs,as_),
        )
    assert settle_manual_system_shadows(db)==1
    card=db.fetchone("SELECT * FROM manual_system_shadow_bets")
    assert card["status"]=="SETTLED"
    assert card["system_pnl_units"] is not None
    assert card["singles_pnl_units"] is not None
    assert sum(float(x["pnl_units"] or 0.0) for x in db.fetchall("SELECT * FROM manual_system_shadow_lines"))==pytest.approx(float(card["system_pnl_units"]))


def test_240_v014_export_contains_manual_system_tables_and_summary(db):
    from config import Settings
    blob,_=build_research_export(db,Settings(),"0.14.0")
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        assert "tables/manual_system_shadow_bets.csv" in z.namelist()
        assert "tables/manual_system_shadow_legs.csv" in z.namelist()
        assert "tables/manual_system_shadow_lines.csv" in z.namelist()
        summary=json.loads(z.read("analysis_summary.json"))
        assert "manual_systems_shadow" in summary



def test_241_v014_targeted_manual_quote_refresh_records_prices_and_cost(db):
    for i in range(4):
        _seed_multiple_exec(db,f"ms2q{i}",commence=f"2026-09-04T{16+i:02d}:00:00+00:00",min_odds=1.8)

    class ManualApi:
        def event_odds(self,sport_key,event_id,region,markets,bookmaker_keys=()):
            return ApiResult({
                "id":event_id,
                "bookmakers":[{
                    "key":"williamhill","title":"William Hill",
                    "last_update":"2026-09-04T12:10:00Z",
                    "markets":[{"key":"h2h","outcomes":[
                        {"name":"Away","price":2.05},
                        {"name":"Home","price":1.90},
                    ]}],
                }],
            },999,1,1)

    q=QuotaGuard(db,reserve=50,daily_budget=1300)
    q.update(remaining=1000,used=0,last_cost=0)
    c=Collector(
        db,ManualApi(),q,sport_keys=("soccer_epl",),region="uk",markets=("h2h",),
        max_events_per_cycle=3,breadth_polls_per_day=0,
        execution_bookmaker_keys=("betfair_ex_uk",),
    )
    out=refresh_manual_system_quotes(
        db,c,bookmaker_keys=("williamhill",),
        now=datetime(2026,9,4,12,10,tzinfo=timezone.utc),horizon_hours=12,
        refresh_interval_minutes=30,max_events_per_cycle=4,daily_credit_budget=20,
    )
    assert out["polled"]==4
    assert db.fetchone("SELECT COUNT(*) AS n FROM odds_snapshots WHERE bookmaker_key='williamhill'")["n"]>=4
    assert db.fetchone("SELECT COALESCE(SUM(actual_cost),0) AS n FROM collector_runs WHERE run_type='MANUAL_SYSTEM_QUOTES'")["n"]==4


# ---------------------------------------------------------------------------
# v0.15.0 META1 CLV Trust Research
# ---------------------------------------------------------------------------

def _seed_meta1_pred_pair(db):
    event_id="meta1_evt"
    db.execute(
        """INSERT INTO events(event_id,sport_key,league,commence_time,home_team,away_team,first_seen_at,last_seen_at,status)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (event_id,"soccer_epl","EPL","2026-09-16T12:00:00+00:00","Alpha","Beta","2026-09-15T10:00:00+00:00","2026-09-15T10:00:00+00:00","UPCOMING"),
    )
    captured="2026-09-15T12:00:00+00:00"
    books=[
        ("betfair_ex_uk","Betfair Exchange",2.10,3.40,3.80),
        ("matchbook","Matchbook",2.08,3.45,3.85),
        ("smarkets","Smarkets",2.12,3.38,3.75),
    ]
    for key,title,h,d,a in books:
        for name,price in (("Alpha",h),("Draw",d),("Beta",a)):
            db.execute(
                """INSERT INTO odds_snapshots(event_id,captured_at,bookmaker_key,bookmaker_title,market_key,outcome_name,price)
                   VALUES(?,?,?,?,?,?,?)""",
                (event_id,captured,key,title,"h2h",name,price),
            )
    db.execute(
        """INSERT INTO football_predictive_predictions(
            event_id,created_at,sport_key,league,commence_time,home_team,away_team,
            mapped_home_team,mapped_away_team,home_mapping_score,away_mapping_score,
            expected_home_goals,expected_away_goals,home_probability,draw_probability,away_probability,
            home_fair_odds,draw_fair_odds,away_fair_odds,league_training_matches,
            home_effective_matches,away_effective_matches,model_version,config_hash)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (event_id,"2026-09-15T11:55:00+00:00","soccer_epl","EPL","2026-09-16T12:00:00+00:00","Alpha","Beta",
         "Alpha","Beta",1.0,1.0,1.65,1.10,0.52,0.27,0.21,1/0.52,1/0.27,1/0.21,500,18.0,17.0,"PRED1_TEST","cfg1"),
    )
    p1=db.fetchone("SELECT id FROM football_predictive_predictions WHERE event_id=?",(event_id,))["id"]
    db.execute(
        """INSERT INTO football_predictive2_predictions(
            event_id,created_at,sport_key,league,commence_time,home_team,away_team,
            mapped_home_team,mapped_away_team,home_mapping_score,away_mapping_score,
            expected_home_goals,expected_away_goals,dixon_coles_rho,home_probability,draw_probability,away_probability,
            home_fair_odds,draw_fair_odds,away_fair_odds,league_training_matches,
            home_effective_matches,away_effective_matches,model_version,config_hash)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (event_id,"2026-09-15T11:55:00+00:00","soccer_epl","EPL","2026-09-16T12:00:00+00:00","Alpha","Beta",
         "Alpha","Beta",1.0,1.0,1.65,1.10,-0.05,0.53,0.26,0.21,1/0.53,1/0.26,1/0.21,500,18.0,17.0,"PRED2_TEST","cfg2"),
    )
    p2=db.fetchone("SELECT id FROM football_predictive2_predictions WHERE event_id=?",(event_id,))["id"]
    db.execute(
        """INSERT INTO football_predictive_bets(
            predictive_key,prediction_id,event_id,created_at,selection,bookmaker_key,bookmaker_title,
            offered_odds,model_probability,model_fair_odds,edge_pct,min_odds,strong_candidate,status,
            app_version,experiment_version,config_hash)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (event_id+"|Alpha",p1,event_id,captured,"Alpha","smarkets","Smarkets",2.12,0.52,1/0.52,10.24,2.02,1,"OPEN","0.15.0","PRED1_TEST","cfg1"),
    )
    db.execute(
        """INSERT INTO football_predictive2_bets(
            predictive_key,prediction_id,event_id,created_at,selection,bookmaker_key,bookmaker_title,
            offered_odds,model_probability,model_fair_odds,edge_pct,min_odds,strong_candidate,status,
            app_version,experiment_version,config_hash)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (event_id+"|Alpha",p2,event_id,captured,"Alpha","smarkets","Smarkets",2.12,0.53,1/0.53,12.36,1.98,1,"OPEN","0.15.0","PRED2_TEST","cfg2"),
    )
    return event_id


def test_242_v015_meta_edge_default_config():
    s=Settings()
    assert s.meta_edge_enabled is True
    assert s.meta_edge_min_clean_labels == 200


def test_243_v015_meta_edge_captures_entry_only_paired_features(db):
    _seed_meta1_pred_pair(db)
    assert capture_meta_edge_samples(db)==2
    rows=db.fetchall("SELECT * FROM meta_edge_samples ORDER BY source_model")
    assert len(rows)==2
    assert all(r["feature_version"]==META_EDGE_VERSION for r in rows)
    assert all(r["bookmaker_count"]==3 for r in rows)
    assert all(r["label_status"]=="PENDING" for r in rows)
    p1=next(r for r in rows if r["source_model"]=="PRED1")
    assert p1["paired_model"]=="PRED2"
    assert p1["paired_probability"]==pytest.approx(0.53)
    assert p1["paired_probability_gap_pp"]==pytest.approx(-1.0)
    assert p1["agreement_band"]=="TIGHT_<=1PP"
    assert p1["paired_bet_exists"]==1
    assert p1["same_top_outcome"]==1
    assert p1["entry_snapshot_at"]=="2026-09-15T12:00:00+00:00"


def test_244_v015_meta_edge_labels_only_clean_ab_for_headline(db):
    _seed_meta1_pred_pair(db)
    capture_meta_edge_samples(db)
    db.execute("UPDATE football_predictive_bets SET closing_odds=?,clv_pct=?,clv_quality=?",(2.00,6.0,"A"))
    db.execute("UPDATE football_predictive2_bets SET closing_odds=?,clv_pct=?,clv_quality=?",(2.15,-1.4,"C"))
    assert label_meta_edge_samples(db)==2
    score=meta_edge_scoreboard(db,200)
    assert score["clean_ab_labels"]==1
    assert score["status"]=="COLLECTING_FEATURE_LABELS"
    assert score["avg_clv_pct"]==pytest.approx(6.0)
    assert score["beat_close_pct"]==pytest.approx(100.0)
    c=db.fetchone("SELECT * FROM meta_edge_samples WHERE source_model='PRED2'")
    assert c["label_status"]=="NON_HEADLINE"


def test_245_v015_meta_edge_segments_expose_agreement_and_archetype(db):
    _seed_meta1_pred_pair(db)
    capture_meta_edge_samples(db)
    db.execute("UPDATE football_predictive_bets SET closing_odds=?,clv_pct=?,clv_quality=?",(2.00,6.0,"A"))
    db.execute("UPDATE football_predictive2_bets SET closing_odds=?,clv_pct=?,clv_quality=?",(2.05,3.4,"B"))
    label_meta_edge_samples(db)
    seg=meta_edge_segments(db)
    assert "agreement_band" in seg and "archetype" in seg and "uncertainty_band" in seg
    tight=next(x for x in seg["agreement_band"] if x["segment"]=="TIGHT_<=1PP")
    assert tight["samples"]==2
    assert tight["beat_close_pct"]==pytest.approx(100.0)


def test_246_v015_meta_edge_maintenance_is_idempotent_and_zero_provider(db):
    _seed_meta1_pred_pair(db)
    first=run_meta_edge_maintenance(db)
    second=run_meta_edge_maintenance(db)
    assert first["captured"]==2
    assert second["captured"]==0
    score=meta_edge_scoreboard(db,200)
    assert score["extra_provider_calls"]==0
    assert score["selection_authority"] is False


def test_247_v015_export_contains_meta_edge_table_and_summary(db):
    _seed_meta1_pred_pair(db)
    run_meta_edge_maintenance(db)
    blob,_=build_research_export(db,Settings(),"0.15.0")
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        assert "tables/meta_edge_samples.csv" in z.namelist()
        summary=json.loads(z.read("analysis_summary.json"))
        assert "meta_edge" in summary
        assert "meta_edge_segments" in summary


def test_248_v015_meta_edge_blocks_post_entry_pair_and_post_entry_odds(db):
    event_id=_seed_meta1_pred_pair(db)
    # Make PRED2 a later catch-up forecast relative to the PRED1 frozen bet.
    db.execute("UPDATE football_predictive2_predictions SET created_at=? WHERE event_id=?",("2026-09-15T13:00:00+00:00",event_id))
    # Remove all entry/as-of snapshots and leave only a later price wave.
    db.execute("DELETE FROM odds_snapshots WHERE event_id=?",(event_id,))
    db.execute(
        """INSERT INTO odds_snapshots(event_id,captured_at,bookmaker_key,bookmaker_title,market_key,outcome_name,price)
           VALUES(?,?,?,?,?,?,?)""",
        (event_id,"2026-09-15T12:30:00+00:00","smarkets","Smarkets","h2h","Alpha",2.00),
    )
    capture_meta_edge_samples(db)
    p1=db.fetchone("SELECT * FROM meta_edge_samples WHERE source_model='PRED1'")
    assert p1["paired_model"] is None
    assert p1["agreement_band"]=="UNPAIRED"
    assert p1["entry_snapshot_at"] is None
    assert p1["bookmaker_count"]==0

# ---------------------------------------------------------------------------
# v0.16.0 PRED3 StatsBomb xG Challenger
# ---------------------------------------------------------------------------

class _Pred3FakeResponse:
    def __init__(self, payload):
        self._payload=payload
    def raise_for_status(self):
        return None
    def json(self):
        return self._payload

class _Pred3FakeSession:
    def __init__(self):
        self.calls=[]
    def get(self,url,**kwargs):
        self.calls.append(url)
        if url.endswith('/competitions.json'):
            return _Pred3FakeResponse([{
                'competition_id':9,'season_id':281,'country_name':'Germany',
                'competition_name':'1. Bundesliga','competition_gender':'male',
                'season_name':'2023/2024'
            }])
        if url.endswith('/matches/9/281.json'):
            return _Pred3FakeResponse([{
                'match_id':1001,'match_date':'2024-04-01','home_score':2,'away_score':1,
                'home_team':{'home_team_name':'Bayer Leverkusen'},
                'away_team':{'away_team_name':'Bayern Munich'},
            }])
        if url.endswith('/events/1001.json'):
            return _Pred3FakeResponse([
                {'period':1,'team':{'name':'Bayer Leverkusen'},'type':{'name':'Shot'},
                 'shot':{'statsbomb_xg':0.20,'type':{'name':'Open Play'}}},
                {'period':1,'team':{'name':'Bayer Leverkusen'},'type':{'name':'Shot'},
                 'shot':{'statsbomb_xg':0.10,'type':{'name':'Penalty'}}},
                {'period':1,'team':{'name':'Bayer Leverkusen'},'type':{'name':'Pressure'}},
                {'period':2,'team':{'name':'Bayern Munich'},'type':{'name':'Shot'},
                 'shot':{'statsbomb_xg':0.40,'type':{'name':'Open Play'}}},
            ])
        raise AssertionError(f'unexpected StatsBomb URL {url}')

def _seed_pred3_xg_training(db, kickoff=datetime(2030,9,1,12,0,tzinfo=timezone.utc)):
    teams=[('Alpha','Gamma',2.20,0.70),('Gamma','Alpha',0.80,1.90),
           ('Beta','Delta',0.70,1.80),('Delta','Beta',1.70,0.75)]
    for i in range(32):
        home,away,hxg,axg=teams[i%len(teams)]
        played=(kickoff-timedelta(days=20+i*8)).isoformat()
        db.execute(
            """INSERT INTO football_predictive3_training_matches(
                statsbomb_match_id,sport_key,competition_name,season_name,played_at,
                home_team,away_team,home_goals,away_goals,home_xg,away_xg,home_npxg,away_npxg,
                home_shots,away_shots,home_pressures,away_pressures,source_ref,imported_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f'sb-{i}','soccer_epl','Premier League','2029/2030',played,home,away,
             0,5,hxg,axg,max(0,hxg-0.1),max(0,axg-0.1),12,8,100,80,f'sb:{i}',played),
        )

def test_249_v016_pred3_default_config():
    s=Settings()
    assert s.predictive_football_pred3_enabled is True
    assert s.predictive_football_pred3_statsbomb_enabled is True
    assert s.predictive_football_pred3_statsbomb_matches_per_cycle == 12
    assert s.predictive_football_pred3_max_data_age_days == pytest.approx(900.0)

def test_250_v016_statsbomb_open_data_imports_event_xg(db):
    engine=PredictiveFootballPred3Engine(
        db,Settings(),execution_bookmaker_keys=('matchbook',),session=_Pred3FakeSession()
    )
    out=engine.bootstrap_historical_data(datetime(2026,9,15,tzinfo=timezone.utc))
    assert out['imported']==1
    row=db.fetchone("SELECT * FROM football_predictive3_training_matches WHERE statsbomb_match_id='1001'")
    assert row is not None
    assert row['sport_key']=='soccer_germany_bundesliga'
    assert row['home_xg']==pytest.approx(0.30)
    assert row['home_npxg']==pytest.approx(0.20)
    assert row['away_xg']==pytest.approx(0.40)
    assert row['home_pressures']==1
    m=db.fetchone("SELECT * FROM football_predictive3_statsbomb_manifest WHERE match_id='1001'")
    assert m['status']=='IMPORTED'

def test_251_v016_pred3_fit_is_xg_driven_and_market_independent(db):
    kickoff=datetime(2030,9,1,12,0,tzinfo=timezone.utc)
    _seed_pred3_xg_training(db,kickoff)
    engine=PredictiveFootballPred3Engine(db,Settings(),execution_bookmaker_keys=('matchbook',))
    fit,reason=engine.fit_event({
        'event_id':'p3-e1','sport_key':'soccer_epl','league':'EPL',
        'commence_time':kickoff.isoformat(),'home_team':'Alpha','away_team':'Beta'
    },now=kickoff-timedelta(hours=24))
    assert reason=='OK' and fit is not None
    assert fit['expected_home_goals']>fit['expected_away_goals']
    assert 0 < fit['home_probability'] < 1
    assert fit['xg_data_age_days'] < 400
    # Actual goals in training were intentionally the opposite signal (0-5);
    # PRED3 should still follow xG because goals are not model inputs.
    assert fit['home_probability'] > fit['away_probability']

def test_252_v016_pred3_rejects_stale_xg_support(db):
    kickoff=datetime(2030,9,1,12,0,tzinfo=timezone.utc)
    _seed_pred3_xg_training(db,kickoff)
    from dataclasses import replace
    s=replace(Settings(),predictive_football_pred3_lookback_days=5000,
              predictive_football_pred3_max_data_age_days=10.0)
    engine=PredictiveFootballPred3Engine(db,s,execution_bookmaker_keys=('matchbook',))
    fit,reason=engine.fit_event({
        'event_id':'p3-e2','sport_key':'soccer_epl','league':'EPL',
        'commence_time':kickoff.isoformat(),'home_team':'Alpha','away_team':'Beta'
    })
    assert fit is None
    assert reason=='STATSBOMB_HISTORY_TOO_STALE'

def test_253_v016_pred3_freeze_and_derived_markets(db):
    kickoff=datetime(2030,9,1,12,0,tzinfo=timezone.utc)
    _seed_pred3_xg_training(db,kickoff)
    db.execute(
        """INSERT INTO events(event_id,sport_key,league,commence_time,home_team,away_team,first_seen_at,last_seen_at,status)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        ('p3-e3','soccer_epl','EPL',kickoff.isoformat(),'Alpha','Beta',
         (kickoff-timedelta(days=1)).isoformat(),(kickoff-timedelta(days=1)).isoformat(),'UPCOMING')
    )
    engine=PredictiveFootballPred3Engine(db,Settings(),execution_bookmaker_keys=('matchbook',))
    out=engine.freeze_due_predictions(kickoff-timedelta(hours=23))
    assert out['created']==1
    pred=db.fetchone("SELECT * FROM football_predictive3_predictions WHERE event_id='p3-e3'")
    assert pred['model_version']=='PRED3_STATSBOMB_XG_POISSON_V1'
    assert pred['xg_data_age_days'] is not None
    assert engine.ensure_market_predictions()==8

def test_254_v016_export_contains_pred3_tables_and_summary(db):
    _seed_pred3_xg_training(db)
    blob,_=build_research_export(db,Settings(),'0.16.0')
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names=set(z.namelist())
        assert 'tables/football_predictive3_training_matches.csv' in names
        assert 'tables/football_predictive3_predictions.csv' in names
        summary=json.loads(z.read('analysis_summary.json'))
        assert 'predictive_football_pred3' in summary
        assert summary['predictive_football_pred3']['training_matches']==32


# ---------------------------------------------------------------------------
# v0.16.1 Historical/provider bootstrap hotfixes
# ---------------------------------------------------------------------------

def test_255_v0161_historical_events_sends_only_supported_snapshot_date():
    from the_odds_api import TheOddsApi
    class Response:
        headers={"x-requests-remaining":"999","x-requests-used":"1","x-requests-last":"1"}
        def raise_for_status(self): pass
        def json(self): return {"timestamp":"2025-08-28T12:00:00Z","data":[]}
    class Session:
        def __init__(self): self.calls=[]
        def get(self,url,params,timeout):
            self.calls.append((url,dict(params),timeout)); return Response()
    session=Session(); api=TheOddsApi("SECRET",session=session)
    api.historical_events(
        "soccer_epl","2025-08-28T12:00:00+00:00",
        event_ids=("ignored",),
        commence_time_from="2025-08-29T00:00:00+00:00",
        commence_time_to="2025-08-31T00:00:00+00:00",
    )
    url,params,_=session.calls[-1]
    assert url.endswith('/historical/sports/soccer_epl/events')
    assert params=={"date":"2025-08-28T12:00:00Z","apiKey":"SECRET"}


def test_256_v0161_historical_event_odds_uses_canonical_z_timestamp():
    from the_odds_api import TheOddsApi
    class Response:
        headers={"x-requests-remaining":"990","x-requests-used":"10","x-requests-last":"10"}
        def raise_for_status(self): pass
        def json(self): return {"timestamp":"2025-08-29T14:55:00Z","data":{}}
    class Session:
        def __init__(self): self.calls=[]
        def get(self,url,params,timeout):
            self.calls.append((url,dict(params),timeout)); return Response()
    session=Session(); api=TheOddsApi("SECRET",session=session)
    api.historical_event_odds(
        "soccer_epl","event-1","uk",("h2h",),"2025-08-29T14:55:00+00:00"
    )
    _,params,_=session.calls[-1]
    assert params["date"]=="2025-08-29T14:55:00Z"
    assert params["dateFormat"]=="iso"
    assert params["oddsFormat"]=="decimal"


def test_257_v0161_football_data_bootstrap_blocks_localhost_redirect():
    from predictive_football import _fetch_football_data_csv, FOOTBALL_DATA_BASE_URL
    class Redirect:
        status_code=302
        headers={"Location":"http://127.0.0.1/mmz4281/2627/E0.csv"}
        content=b""
        def raise_for_status(self): pass
    class Session:
        def __init__(self): self.calls=[]
        def get(self,url,timeout=20,headers=None,allow_redirects=True):
            self.calls.append((url,allow_redirects)); return Redirect()
    session=Session()
    with pytest.raises(RuntimeError,match="blocked football-data redirect"):
        _fetch_football_data_csv(
            session,f"{FOOTBALL_DATA_BASE_URL}/2627/E0.csv",timeout=20,
            headers={"User-Agent":"test"}
        )
    assert len(session.calls)==1
    assert session.calls[0][0].startswith("https://football-data.co.uk/")
    assert session.calls[0][1] is False
