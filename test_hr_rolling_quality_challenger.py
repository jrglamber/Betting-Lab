"""Forward-only paired ranking tests. All prices and events are synthetic.

No external providers or bookmaker execution. These specifically test that
ranking cannot smuggle a later price or a different market into the comparison.
"""
from datetime import datetime, timedelta, timezone
from math import isclose

from db import Database
import hr_portfolio_shadow as baseline
import hr_rolling_quality_challenger as q


def _time(base=None):
    return base or datetime.now(timezone.utc)


def _candidate(event, source, created, kickoff, minimum=2.0):
    return {
        "event_id": event, "source_lane": source,
        "source_table": "multisport_execution_bets", "source_id": int(event[1:]),
        "source_created_at": created.isoformat(), "first_seen_at": created.isoformat(),
        "commence_time": kickoff.isoformat(), "sport_key": "americanfootball_ncaaf" if "NCAAF" in source else "soccer_epl",
        "market_key": "h2h", "selection": event, "point": None,
        "min_odds": minimum, "home_team": event, "away_team": "Away",
    }


def _entry(event, source, created, kickoff, capture, price, minimum=2.0):
    return (_candidate(event, source, created, kickoff, minimum),
            {"captured_at": capture.isoformat(), "price": price,
             "bookmaker_key": "williamhill"})


def test_relative_price_not_just_high_raw_odds():
    now = _time()
    c = now-timedelta(minutes=2)
    a = _entry("E1", "FOOTBALL", c, now+timedelta(hours=2), c, 3.0, 3.0)
    b = _entry("E2", "NCAAF_3_TO_4_99_EXPERIMENTAL", c, now+timedelta(hours=2), c, 2.15, 2.0)
    s_a = q._price_score(*a, now, 1)
    s_b = q._price_score(*b, now, 1)
    assert s_b["score"] > s_a["score"], "Price margin vs source minimum, not bigger raw odds"


def test_fresher_quote_wins_with_identical_price_ratio():
    now = _time()
    a = _entry("E1", "FOOTBALL", now-timedelta(minutes=9),
               now+timedelta(hours=2), now-timedelta(minutes=9), 2.1)
    b = _entry("E2", "FOOTBALL", now-timedelta(minutes=1),
               now+timedelta(hours=2), now-timedelta(minutes=1), 2.1)
    assert q._price_score(*b, now, 1)["score"] > q._price_score(*a, now, 1)["score"]


def test_past_kickoff_or_future_quote_excluded():
    now = _time()
    past = _entry("E1", "FOOTBALL", now-timedelta(minutes=5),
                  now-timedelta(minutes=1), now-timedelta(seconds=10), 2.2)
    future = _entry("E2", "FOOTBALL", now-timedelta(minutes=5),
                    now+timedelta(hours=3), now+timedelta(minutes=1), 2.2)
    valid = _entry("E3", "FOOTBALL", now-timedelta(minutes=5),
                   now+timedelta(hours=3), now-timedelta(seconds=10), 2.2)
    data = [past, future, valid]
    ranked = q._rank_pool(data, {"williamhill": data}, now,
                          now-timedelta(minutes=5), now, now-timedelta(minutes=10))
    assert [r["candidate"]["event_id"] for r in ranked] == ["E3"]


def test_same_event_never_selected_twice_and_ncaaf_not_forced():
    now = _time()
    created = now-timedelta(minutes=5)
    ts = now-timedelta(seconds=10)
    entries = [
        _entry("E1", "NCAAF_3_TO_4_99_EXPERIMENTAL", created, now+timedelta(hours=1), ts, 3.0, 3.0),
        _entry("E1", "FOOTBALL", created, now+timedelta(hours=1), ts, 2.0),
        _entry("E2", "FOOTBALL", created, now+timedelta(hours=1), ts, 2.3),
        _entry("E3", "FOOTBALL", created, now+timedelta(hours=1), ts, 2.25),
    ]
    ranked = q._rank_pool(entries, {"williamhill": entries}, now,
                          now-timedelta(minutes=5), now, now-timedelta(minutes=10))
    chosen = q._select(ranked, 2)
    assert len(chosen) == 2
    assert len({x["candidate"]["event_id"] for x in chosen}) == 2
    assert all("NCAAF" not in x["candidate"]["source_lane"] for x in chosen)


def test_all_ncaaf_is_allowed():
    now = _time()
    ts = now-timedelta(seconds=5)
    items = [_entry(f"E{i}", "NCAAF_3_TO_4_99_EXPERIMENTAL",
                    now-timedelta(minutes=1), now+timedelta(hours=4),
                    ts, 3.5, 3.0) for i in range(1, 5)]
    ranked = q._rank_pool(items, {"williamhill": items}, now,
                          now-timedelta(minutes=5), now, now-timedelta(minutes=10))
    chosen = q._select(ranked, 4)
    assert len(chosen) == 4
    assert all(x["candidate"]["source_lane"].startswith("NCAAF") for x in chosen)


def test_coverage_only_counts_quotes_in_same_frozen_window():
    now = _time()
    ts = now-timedelta(seconds=10)
    items = [_entry("E1", "FOOTBALL", now-timedelta(minutes=1),
                    now+timedelta(hours=3), ts, 2.1)]
    stale = [_entry("E1", "FOOTBALL", now-timedelta(minutes=1),
                    now+timedelta(hours=3), now-timedelta(minutes=12), 2.15)]
    ranked = q._rank_pool(items, {"williamhill": items, "ladbrokes_uk": stale},
                          now, now-timedelta(minutes=5), now, now-timedelta(minutes=10))
    assert ranked[0]["detail"]["book_count"] == 1


def _db(tmp_path):
    database = Database(db_path=str(tmp_path/"quality.sqlite"))
    baseline._schema(database)
    q._schema(database)
    return database


def _baseline_group(db, now, base_entries, bookmaker="williamhill"):
    # A preexisting chronological baseline card built at the same book/price.
    created = now.isoformat()
    n = len(base_entries)
    db.execute("""INSERT INTO hr_portfolio_groups(
        group_key,created_at,policy_version,cohort,bookmaker_key,
        selection_count,status,reference_stake_gbp
    ) VALUES(?,?,?,?,?,?,?,?)""", (
        "baseline-synthetic-group", created, baseline.VERSION,
        "UNIFIED", bookmaker, n, "OPEN", 10.0,
    ))
    group = db.fetchone("SELECT * FROM hr_portfolio_groups WHERE group_key=?",
                        ("baseline-synthetic-group",))
    for i, (candidate, quote) in enumerate(base_entries, 1):
        db.execute("""INSERT INTO hr_portfolio_legs(
            group_id,leg_order,source_lane,source_table,source_id,event_id,sport_key,
            market_key,selection,point,entry_odds,bookmaker_key,quote_captured_at,
            home_team,away_team,commence_time
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            group["id"], i, candidate["source_lane"], candidate["source_table"],
            candidate["source_id"], candidate["event_id"], candidate["sport_key"],
            candidate["market_key"], candidate["selection"], None,
            quote["price"], bookmaker, quote["captured_at"],
            candidate["home_team"], candidate["away_team"],
            candidate["commence_time"],
        ))
    for fmt, share in baseline._allocations(n).items():
        db.execute("""INSERT INTO hr_portfolio_formats(
            group_id,format,allocation_pct,status
        ) VALUES(?,?,?,?)""", (group["id"], fmt, share, "OPEN"))
    return group


def test_quality_pair_changes_selection_but_keeps_same_book_stake_and_time(tmp_path):
    db = _db(tmp_path)
    now = _time()
    ts = now-timedelta(seconds=10)
    made = now-timedelta(minutes=3)
    base_entries = [
        _entry("E1", "FOOTBALL", made, now+timedelta(hours=1), ts, 2.0),
        _entry("E2", "FOOTBALL", made+timedelta(seconds=1), now+timedelta(hours=1), ts, 2.0),
    ]
    better = _entry("E3", "NCAAF_3_TO_4_99_EXPERIMENTAL",
                    made+timedelta(seconds=2), now+timedelta(hours=1), ts, 3.45, 3.0)
    g = _baseline_group(db, now, base_entries)
    options = {"williamhill": base_entries+[better]}
    assert q._pair(db, g, options, now-timedelta(minutes=1), now) == "formed"
    frozen = db.fetchall("SELECT * FROM hr_quality_legs WHERE baseline_group_id=? ORDER BY leg_order",
                         (g["id"],))
    assert len(frozen) == 2
    assert set(x["event_id"] for x in frozen) != {"E1", "E2"}
    assert all(x["bookmaker_key"] == "williamhill" for x in frozen)
    assert all(x["quote_captured_at"] == ts.isoformat() for x in frozen)
    assert db.fetchone("SELECT COUNT(*) AS n FROM hr_quality_candidate_audit")["n"] == 3
    pair = db.fetchone("SELECT * FROM hr_quality_pairs")
    assert pair["candidate_count"] == 3
    assert pair["overlap_count"] == 1
    assert pair["quality_ncaaf_count"] == 1
    assert isclose(sum(float(x["allocation_pct"]) for x in db.fetchall(
        "SELECT allocation_pct FROM hr_quality_formats")), 1.0)


def test_older_quote_cannot_enter_new_forward_challenger(tmp_path):
    db = _db(tmp_path)
    now = _time()
    made = now-timedelta(minutes=3)
    ts = now-timedelta(minutes=2)
    entries = [
        _entry("E1", "FOOTBALL", made, now+timedelta(hours=2), ts, 2.15),
        _entry("E2", "FOOTBALL", made, now+timedelta(hours=2), ts, 2.1),
    ]
    g = _baseline_group(db, now, entries)
    assert q._pair(db, g, {"williamhill": entries},
                   now-timedelta(minutes=1), now) == "skipped"
    pair = db.fetchone("SELECT status,skip_reason FROM hr_quality_pairs")
    assert pair["status"] == "SKIPPED"
    assert pair["skip_reason"] == "PRE_ACTIVATION_OR_FUTURE_QUOTE"


def test_pair_settlement_compares_only_equal_weight_matched_groups(tmp_path):
    db = _db(tmp_path)
    now = _time()
    made = now-timedelta(minutes=1)
    ts = now-timedelta(seconds=5)
    entries = [
        _entry("E1", "FOOTBALL", made, now+timedelta(hours=2), ts, 2.15),
        _entry("E2", "FOOTBALL", made, now+timedelta(hours=2), ts, 2.1),
    ]
    g = _baseline_group(db, now, entries)
    assert q._pair(db, g, {"williamhill": entries},
                   now-timedelta(minutes=2), now) == "formed"
    # Both arms: two winning legs. A 2.15 x 2.10 double returns 4.515u.
    db.execute("UPDATE hr_quality_legs SET result='WIN'")
    db.execute("UPDATE hr_portfolio_formats SET status='SETTLED',pnl_units=3.515 WHERE format='DOUBLE'")
    db.execute("UPDATE hr_portfolio_formats SET status='SETTLED',pnl_units=1.125 WHERE format='SINGLES'")
    assert q._settle(db) == 1
    pair = db.fetchone("SELECT * FROM hr_quality_pairs")
    assert pair["status"] == "SETTLED"
    assert isclose(pair["baseline_pnl_units"], 3.515)
    assert isclose(pair["quality_pnl_units"], 3.515)
    assert isclose(pair["delta_pnl_units"], 0.0)
    assert q._settle(db) == 0, "No duplicate settlement"


def test_quality_settlement_waits_for_baseline_accounting(tmp_path):
    db = _db(tmp_path)
    now = _time()
    made = now-timedelta(minutes=1)
    ts = now-timedelta(seconds=5)
    entries = [
        _entry("E1", "FOOTBALL", made, now+timedelta(hours=2), ts, 2.1),
        _entry("E2", "FOOTBALL", made, now+timedelta(hours=2), ts, 2.1),
    ]
    g = _baseline_group(db, now, entries)
    assert q._pair(db, g, {"williamhill": entries},
                   now-timedelta(minutes=2), now) == "formed"
    db.execute("UPDATE hr_quality_legs SET result='WIN'")
    assert q._settle(db) == 0
    assert db.fetchone("SELECT status FROM hr_quality_pairs")["status"] == "OPEN"
