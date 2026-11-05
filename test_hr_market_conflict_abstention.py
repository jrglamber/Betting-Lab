"""Direct same-line market-opposition abstention, forward only.

Do not mistake two odds-band candidates on the same fixture for two
independently validated choices. Nonconflicting alternate lines remain.
"""
from datetime import datetime, timedelta, timezone
from db import Database
import hr_market_integrity as integrity
import hr_portfolio_shadow as portfolio
import hr_portfolio_queues as queues
import hr_portfolio_presentation as ui

NOW=datetime(2026,11,4,17,0,tzinfo=timezone.utc)


def leg(event, home, away, side="Over", point=2.5, minute=15):
    return {
        "event_id":event,"source_lane":"FOOTBALL_TOTALS_1_80_2_99",
        "source_table":"execution_shadow_bets","source_id":minute,
        "sport_key":"soccer_epl","market_key":"totals",
        "selection":side,"point":point,"min_odds":1.85,"source_odds":2.0,
        "source_created_at":(NOW-timedelta(minutes=minute)).isoformat(),
        "first_seen_at":(NOW-timedelta(minutes=minute)).isoformat(),
        "commence_time":(NOW+timedelta(hours=2)).isoformat(),
        "home_team":home,"away_team":away,"status":"QUEUED",
    }


def quote():
    return {"price":2.06,"captured_at":(NOW-timedelta(seconds=40)).isoformat()}


def test_direct_same_market_line_conflict_blocks_whole_fixture():
    over=leg("provider1","Cardiff City","West Ham United","Over",2.5)
    under=leg("provider2","Cardiff City","West Ham United","Under",2.5)
    oxford=leg("provider3","Oxford United","Reading","Over",2.5)
    conflicts=integrity.conflicted_fixture_keys([over,under,oxford])
    assert conflicts=={integrity.fixture_key(over)}
    assert integrity.fixture_key(under) in conflicts
    assert integrity.fixture_key(oxford) not in conflicts


def test_two_lines_that_can_both_win_do_not_trigger_false_conflict():
    over=leg("a","Cardiff City","West Ham United","Over",2.5)
    under=leg("b","Cardiff City","West Ham United","Under",3.5)
    assert not integrity.conflicted_fixture_keys([over,under])


def test_missing_totals_line_is_blocked_independently_not_counted_as_conflict():
    over=leg("a","Cardiff City","West Ham United","Over",None)
    under=leg("b","Cardiff City","West Ham United","Under",None)
    assert not integrity.conflicted_fixture_keys([over,under])
    assert not integrity.market_actionable(over)


def test_portfolio_forms_only_unconflicted_games_and_cache_explains_abstention(monkeypatch,tmp_path):
    db=Database(db_path=str(tmp_path/"conflicted.sqlite"))
    portfolio._schema(db)
    state=portfolio._state(db)
    over=leg("a","Cardiff City","West Ham United","Over",2.5)
    under=leg("b","Cardiff City","West Ham United","Under",2.5)
    oxford=leg("c","Oxford United","Reading","Over",2.5,minute=13)
    watford=leg("d","Watford","Stoke City","Over",2.5,minute=12)
    source=[over,under,oxford,watford]
    monkeypatch.setattr(portfolio,"_base_candidates",lambda db,now: source)
    def _books(db,candidates,now,start):
        return {
            "williamhill":[(c,quote()) for c in candidates],
            "ladbrokes_uk":[],
        }
    monkeypatch.setattr(portfolio,"_book_options",_books)
    result=portfolio._form(db,NOW,state)
    assert result["formed"]==1
    assert result["conflicting_fixtures_abstained"]==1
    groups=db.fetchall("SELECT id,selection_count FROM hr_portfolio_groups")
    assert len(groups)==1 and groups[0]["selection_count"]==2
    frozen=db.fetchall("SELECT event_id,home_team FROM hr_portfolio_legs ORDER BY leg_order")
    assert {x["home_team"] for x in frozen}=={"Oxford United","Watford"}
    q=queues.read(db,view="portfolio")
    assert q["conflicting_fixtures_abstained"]==1
    assert q["conflicting_candidate_options"]==2
    statuses={x["event_id"]:x["queue_status"] for x in q["waiting_rows"]}
    assert statuses["a"]=="DIRECT_OPPOSITION_ABSTAIN"
    assert statuses["b"]=="DIRECT_OPPOSITION_ABSTAIN"
    page=ui.render_portfolio({
        "portfolio":[],"formats":[],"lane_metrics":[],"lane_policy":[],
        "latest_groups":[],"recent_ncaaf_mix":{},
    },queue=q)
    assert "Conflicted fixtures blocked" in page
    assert "PORTFOLIO ABSTAINS" in page
    assert "Over 2.5" in page and "Under 2.5" in page
