"""Regression for duplicate/opposite candidate rows seen on 4 Nov 2026.

The research feed may contain several market options from one fixture.
Show them grouped, never let one fixture count twice in a new multiple,
and never send totals without a precise handicap/goal line.
"""
from datetime import datetime, timedelta, timezone

from db import Database
import hr_market_integrity as integrity
import hr_portfolio_shadow as portfolio
import hr_portfolio_queues as queue
import hr_portfolio_presentation as view
import hr_rolling_quality_challenger as challenger
import hr_portfolio_telegram as alerts


NOW = datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc)


def _football(event_id, selection, point, *, market="totals", created=1,
              home="Cardiff City", away="West Ham United", fixture_hour=19):
    return {
        "event_id": event_id, "sport_key": "soccer_epl",
        "home_team": home, "away_team": away,
        "commence_time": NOW.replace(hour=fixture_hour,minute=45).isoformat(),
        "source_lane": "FOOTBALL_TOTALS_1_80_2_99",
        "source_table": "execution_shadow_bets",
        "source_id": int(created), "source_created_at": (NOW-timedelta(minutes=created)).isoformat(),
        "first_seen_at": (NOW-timedelta(minutes=created)).isoformat(),
        "market_key": market, "selection": selection, "point": point,
        "status": "QUEUED", "source_odds": 2.0, "min_odds": 1.85,
    }


def _quote(odds=2.04):
    return {"price": odds, "captured_at": (NOW-timedelta(minutes=1)).isoformat()}


def _render(queue_data):
    return view.render_portfolio({
        "portfolio":[], "formats":[], "lane_metrics":[], "lane_policy":[],
        "latest_groups":[], "recent_ncaaf_mix":{},
    }, queue=queue_data)


def test_distinct_totals_lines_are_not_false_duplicates():
    over25 = _football("A", "Over", 2.5)
    over35 = _football("A", "Over", 3.5)
    under25 = _football("A", "Under", 2.5)
    assert integrity.selection_key(over25) != integrity.selection_key(over35)
    assert integrity.selection_key(over25) != integrity.selection_key(under25)
    assert integrity.market_label(over25) == "Over 2.5"
    assert integrity.market_label(over35) == "Over 3.5"
    assert integrity.opposing_options([over25, under25])
    assert not integrity.opposing_options([over25, _football("A","Under",3.5)])


def test_identical_options_from_different_upstream_event_ids_collapse(tmp_path):
    db=Database(db_path=str(tmp_path/"dups.sqlite"))
    over = _football("feed-1", "Over", 2.5, created=4)
    same = _football("feed-2", "Over", 2.5, created=2)
    under = _football("feed-1", "Under", 2.5, created=3)
    above = _football("feed-1", "Over", 3.5, created=1)
    missing = _football("feed-1", "Over", None, created=5)
    candidates=[over,same,under,above,missing]
    eligible={"FOOTBALL_TOTALS_1_80_2_99"}
    result=queue.capture(db,NOW,candidates,eligible,{b:[] for b in portfolio.MANUAL_BOOKS})
    assert result["updated"]
    p=queue.read(db,view="portfolio")
    assert p["source_candidates"]==4
    assert p["duplicate_raw_rows_collapsed"]==1
    assert p["missing_line_candidates"]==1
    html=_render(p)
    assert html.count("Cardiff City v West Ham United")==1
    assert "Over 2.5" in html and "Under 2.5" in html
    assert "Over 3.5" in html
    assert "LINE MISSING" in html
    assert "OPPOSING PREDICTIONS" in html
    assert "Missing totals/handicap line" in html


def test_same_fixture_can_have_one_leg_only_even_with_different_event_ids():
    cardiff=_football("feed-1","Over",2.5,created=7)
    duplicate=_football("feed-2","Over",2.5,created=5)
    opposite=_football("feed-2","Under",2.5,created=3)
    oxford=_football("feed-3","Over",2.5,created=2,
                     home="Oxford United",away="Reading")
    picks=portfolio._pick_unique([
        (x,_quote()) for x in (cardiff,duplicate,opposite,oxford)
    ])
    assert len(picks)==2
    assert len({integrity.fixture_key(x[0]) for x in picks})==2
    assert {x[0]["home_team"] for x in picks}=={"Cardiff City","Oxford United"}


def test_totals_missing_point_cannot_be_a_new_portfolio_leg():
    ambiguous=_football("A","Over",None,created=6)
    valid=_football("B","Over",2.5,created=4,home="Oxford United",away="Reading")
    assert not integrity.market_actionable(ambiguous)
    assert portfolio._pick_unique([(ambiguous,_quote()),(valid,_quote())])==[]


def test_quality_challenger_does_not_rank_duplicate_choice_twice_or_select_same_fixture():
    over=_football("feed-1","Over",2.5,created=7)
    duplicate=_football("feed-2","Over",2.5,created=6)
    under=_football("feed-2","Under",2.5,created=5)
    oxford=_football("feed-3","Over",3.5,created=4,
                     home="Oxford United",away="Reading")
    missing=_football("feed-4","Under",None,created=3)
    items=[(x,_quote()) for x in (over,duplicate,under,oxford,missing)]
    ranked=challenger._rank_pool(items,{"williamhill":items},NOW,
                                 NOW-timedelta(minutes=5),NOW,
                                 NOW-timedelta(days=1))
    assert len(ranked)==3
    selections=challenger._select(ranked,2)
    assert len(selections)==2
    assert len({integrity.fixture_key(r["candidate"]) for r in selections})==2


def test_alert_blocked_if_same_fixture_twice_or_missing_point():
    a=_football("feed-1","Over",2.5,created=6)
    b=_football("feed-2","Under",2.5,created=5)
    group={
        "policy_version":portfolio.VERSION,"cohort":"UNIFIED",
        "status":"OPEN","bookmaker_key":"williamhill","selection_count":2,
    }
    def leg(c):
        return {**c,"bookmaker_key":"williamhill","entry_odds":2.1,
                "quote_captured_at":_quote()["captured_at"]}
    ok,reason=alerts._valid(group,[leg(a),leg(b)],NOW)
    assert not ok and reason=="repeat_fixture"
    x=_football("other","Over",None,created=3,
                home="Oxford United",away="Reading")
    ok,reason=alerts._valid(group,[leg(a),leg(x)],NOW)
    assert not ok and reason=="market_line_missing"


def test_quote_provenance_used_after_duplicate_feeds_merged(tmp_path):
    db=Database(db_path=str(tmp_path/"quotes.sqlite"))
    a=_football("id-1","Over",2.5,created=6)
    b=_football("id-2","Over",2.5,created=2)
    a["min_odds"]=1.85
    b["min_odds"]=2.05
    opts={"williamhill":[(b,_quote(2.12))], "ladbrokes_uk":[]}
    queue.capture(db,NOW,[a,b],{"FOOTBALL_TOTALS_1_80_2_99"},opts)
    snapshot=queue.read(db,view="portfolio")
    assert snapshot["quoted_candidates"]==1
    assert len(snapshot["rows"])==1
    row=snapshot["rows"][0]
    assert row["event_id"]=="id-2"
    assert row["min_odds"]==2.05
    assert row["quote_price"]==2.12
