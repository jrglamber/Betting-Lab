"""HR2 pocket singles must not triple-count repeated Yankee/Trixie/Double legs."""
from datetime import datetime, timezone
from db import Database
import hit_rate_multiples_lab as hr
import hr2_singles_pocket_audit as audit


def _card(db, idx, fmt, mode="SAME_BOOK_VERIFIED", policy=audit.POLICY_VERSION):
    db.execute("""INSERT INTO hit_rate_cards(
        card_key,policy_version,experiment_key,system_type,created_at,
        status,assembly_mode,reference_stake_gbp
    ) VALUES(?,?,?,?,?,?,?,?)""", (
        f"card-{idx}", policy, "HR2", fmt,
        f"2026-11-05T12:0{idx}:00+00:00", "OPEN", mode, 10.0,
    ))
    return db.fetchone("SELECT id FROM hit_rate_cards WHERE card_key=?", (f"card-{idx}",))["id"]


def _leg(db, card, order, event, lane, price, result, sport="soccer_epl"):
    db.execute("""INSERT INTO hit_rate_legs(
        card_id,leg_order,candidate_id,source_lane,source_table,source_id,
        event_id,sport_key,market_key,selection,point,entry_odds,bookmaker_key,
        quote_captured_at,result,commence_time,home_team,away_team
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        card,order,999,lane,"test",1,event,sport,"h2h",f"{event}-home",
        None,price,"williamhill","2026-11-05T11:59:00+00:00",result,
        "2026-11-06T13:00:00+00:00",f"{event}-home",f"{event}-away",
    ))


def test_unique_pocket_singles_and_first_frozen_price(tmp_path):
    db = Database(db_path=str(tmp_path / "pocket.sqlite"))
    hr._ensure_schema(db)
    c1 = _card(db, 1, "YANKEE")
    c2 = _card(db, 2, "DOUBLE")
    c3 = _card(db, 3, "TRIXIE")
    c4 = _card(db, 4, "YANKEE", mode="CROSS_BOOK_SYNTHETIC")
    c5 = _card(db, 5, "HEINZ", policy="HRM_2026-10-12_V1")

    _leg(db,c1,1,"a","FOOTBALL_H2H_2_50_2_99",2.6,"WIN")
    _leg(db,c1,2,"b","FOOTBALL_H2H_2_50_2_99",2.7,"LOSS")
    _leg(db,c1,3,"c","NCAAF_H2H_3_00_4_99",3.6,"WIN","americanfootball_ncaaf")
    _leg(db,c2,1,"a","FOOTBALL_H2H_2_50_2_99",2.9,"WIN")  # no best-price hindsight
    _leg(db,c2,2,"d","TENNIS_H2H_1_80_2_99",2.1,None,"tennis_atp")
    _leg(db,c3,1,"a","FOOTBALL_H2H_2_50_2_99",2.7,"WIN")  # count once, 3 formats
    _leg(db,c3,2,"e","NFL_H2H_1_50_1_99",1.8,"VOID","americanfootball_nfl")
    _leg(db,c4,1,"z","NCAAF_H2H_3_00_4_99",4.0,"WIN","americanfootball_ncaaf")
    _leg(db,c5,1,"old","NCAAF_H2H_3_00_4_99",4.0,"WIN","americanfootball_ncaaf")

    out = audit.report(db)
    pockets = {x["lane"]:x for x in out["pockets"]}
    assert out["raw_multiple_leg_appearances"] == 7  # excludes synthetic and old policy
    assert out["duplicate_multiple_leg_appearances_collapsed"] == 2
    assert out["unique_quoted"] == 5
    assert out["unique_settled"] == 4
    assert out["unique_unsettled"] == 1
    f = pockets["FOOTBALL_H2H_2_50_2_99"]
    assert f["unique_quoted_selections"] == 2
    assert f["wins"] == 1 and f["losses"] == 1
    assert f["hit_rate_pct"] == 50.0
    assert f["singles_roi_pct"] == 30.0  # 2.60 WIN -> +1.60, other LOSS -> -1
    assert f["formats_observed"] == ["DOUBLE", "TRIXIE", "YANKEE"]
    assert pockets["NCAAF_H2H_3_00_4_99"]["singles_roi_pct"] == 260.0
    assert pockets["NFL_H2H_1_50_1_99"]["voids_or_pushes"] == 1
    assert pockets["NFL_H2H_1_50_1_99"]["hit_rate_pct"] is None
    assert pockets["TENNIS_H2H_1_80_2_99"]["singles_roi_pct"] is None
    assert out["no_api_requests"] is True


def test_cross_pocket_duplicate_is_reported_not_double_counted(tmp_path):
    db = Database(db_path=str(tmp_path / "overlap.sqlite"))
    hr._ensure_schema(db)
    c1 = _card(db, 1, "YANKEE")
    c2 = _card(db, 2, "DOUBLE")
    _leg(db,c1,1,"same","FOOTBALL_H2H_2_50_2_99",2.6,"WIN")
    _leg(db,c2,1,"same","FOOTBALL_OTHER_SAME_MARKET",2.8,"WIN")
    r = audit.report(db)
    assert r["unique_quoted"] == 1
    assert r["across_pocket_duplicate_appearances"] == 1
    assert len(r["pockets"]) == 1
