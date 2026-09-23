import io
import json
import zipfile
from pathlib import Path

import pytest

from config import Settings
from db import Database
from exporter import build_research_export
from meta_edge_model import (
    META_MODEL_VERSION,
    create_frozen_meta_model,
    latest_meta_model_scores,
    meta_model_status,
    run_meta_model_maintenance,
)


@pytest.fixture
def db(tmp_path):
    d = Database("", str(tmp_path / "lab_v018.sqlite"))
    d.init_schema()
    d.execute(
        """INSERT INTO events(event_id,sport_key,league,commence_time,home_team,away_team,first_seen_at,last_seen_at,status)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        ("meta2_evt","soccer_epl","EPL","2026-10-01T12:00:00+00:00","Alpha","Beta",
         "2026-09-01T00:00:00+00:00","2026-09-01T00:00:00+00:00","UPCOMING"),
    )
    return d


def _insert_meta_sample(db, i, *, labeled=True, captured_day=None):
    # Deliberately learnable synthetic pattern: positive close survival is
    # associated with a stronger entry-vs-median signal and model probability.
    positive = 1 if (i % 3 == 0 or i % 7 == 0) else 0
    clv = 4.0 if positive else -2.5
    odds = 2.0 + (i % 6) * 0.7
    day = captured_day if captured_day is not None else 1 + i // 30
    captured = f"2026-09-{day:02d}T{(i % 24):02d}:00:00+00:00"
    label_status = "CLEAN_AB" if labeled else "PENDING"
    values = (
        f"meta2|{i}","META1_CLV_TRUST_FEATURES_V1","PRED2" if i % 2 else "PRED1","test_bets",i+1,i+1,
        "meta2_evt",captured,None,"2026-10-01T12:00:00+00:00","soccer_epl","EPL","h2h","Alpha",None,"h2h",
        "smarkets",odds,0.20 + 0.10 * positive,3.0,10.0 + 20.0 * positive,1,24.0,
        "2-3" if odds < 3.0 else ("3-5" if odds < 5.0 else "5-8"),"10-20%",3,odds-0.1,odds,2.0,
        5.0 * positive,5.0,"PRED2",0.21,1.0,"TIGHT_<=1PP",1,1,0.70,"MEDIUM",2.6,500,20.0,
        "FAV|NORMAL_GOALS",label_status,
        clv if labeled else None,"A" if labeled else None,positive if labeled else None,odds-0.2 if labeled else None,
        ("WIN" if positive else "LOSS") if labeled else None,(1.0 if positive else -1.0) if labeled else None,
        "2026-10-02T00:00:00+00:00" if labeled else None,captured,captured,
    )
    db.execute(
        """INSERT INTO meta_edge_samples(
            sample_key,feature_version,source_model,source_bet_table,source_bet_id,source_prediction_id,event_id,
            captured_at,entry_snapshot_at,commence_time,sport_key,league,market_key,selection,point,line_key,bookmaker_key,
            offered_odds,model_probability,model_fair_odds,edge_pct,strong_candidate,hours_to_kickoff,odds_band,edge_band,
            bookmaker_count,median_market_odds,best_market_odds,price_dispersion_pct,chosen_vs_median_pct,mean_overround_pct,
            paired_model,paired_probability,paired_probability_gap_pp,agreement_band,paired_bet_exists,same_top_outcome,
            uncertainty_score,uncertainty_band,expected_total_goals,league_training_matches,training_support_matches,archetype,
            label_status,clv_pct,clv_quality,beat_close,closing_odds,result,net_pnl_units,labeled_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        values,
    )


def test_263_v018_meta2_default_is_shadow_only_enabled():
    s = Settings()
    assert s.meta_edge_model_enabled is True
    assert s.meta_edge_min_clean_labels == 200


def test_264_v018_meta2_waits_below_clean_label_gate(db):
    for i in range(50):
        _insert_meta_sample(db, i)
    out = run_meta_model_maintenance(db, min_clean_labels=200, enabled=True)
    assert out["created"] is False
    assert out["reason"] == "insufficient_labels"
    status = meta_model_status(db)
    assert status["active"] is False
    assert status["selection_authority"] is False


def test_265_v018_meta2_freezes_time_holdout_and_scores_future_only(db):
    for i in range(210):
        _insert_meta_sample(db, i)
    out = run_meta_model_maintenance(db, min_clean_labels=200, enabled=True)
    assert out["created"] is True
    status = meta_model_status(db)
    assert status["model_version"] == META_MODEL_VERSION
    assert status["research_only"] is True
    assert status["selection_authority"] is False
    assert status["clean_labels_at_freeze"] == 210
    assert status["train_labels"] == 168
    assert status["holdout_labels"] == 42
    assert status["metrics"]["holdout_brier"] < status["metrics"]["baseline_brier"]
    holdout_scores = latest_meta_model_scores(db, 1000)
    assert len([x for x in holdout_scores if x["phase"] == "HOLDOUT"]) == 42
    assert len([x for x in holdout_scores if x["phase"] == "FORWARD"]) == 0

    # New entry-time sample after the frozen training cutoff is annotated even
    # before its CLV label exists; no training data is silently refreshed.
    _insert_meta_sample(db, 210, labeled=False, captured_day=12)
    out2 = run_meta_model_maintenance(db, min_clean_labels=200, enabled=True)
    assert out2["created"] is False
    assert out2["reason"] == "already_frozen"
    assert out2["scored"] == 1
    status2 = meta_model_status(db)
    assert status2["clean_labels_at_freeze"] == 210
    assert status2["forward_scores"] == 1
    latest = latest_meta_model_scores(db, 1)[0]
    assert latest["phase"] == "FORWARD"
    assert 0.0 <= latest["prob_beat_close"] <= 1.0
    assert latest["trust_band"] in {"LOW","NEUTRAL","ABOVE_AVERAGE","HIGH"}


def test_266_v018_export_includes_meta2_tables_and_summary(db):
    blob, _ = build_research_export(db, Settings(), "0.18.0")
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        assert "tables/meta_edge_model_runs.csv" in z.namelist()
        assert "tables/meta_edge_model_scores.csv" in z.namelist()
        summary = json.loads(z.read("analysis_summary.json"))
        assert "meta_edge_model" in summary
        assert summary["meta_edge_model"]["selection_authority"] is False
