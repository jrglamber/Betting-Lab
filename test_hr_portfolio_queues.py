"""Bounded, read-only HR queue snapshots and separate FIFO/quality presentation."""
from datetime import datetime, timedelta, timezone

from db import Database
import hr_portfolio_queues as queues
import hr_portfolio_presentation as ui


def _at():
    return datetime(2026, 11, 4, 13, 45, tzinfo=timezone.utc)


def _c(event, lane, when, *, minimum=2.0):
    now = _at()
    return {
        "source_lane": lane, "source_table": "multisport_execution_bets",
        "source_id": event, "source_odds": minimum,
        "first_seen_at": when.isoformat(), "source_created_at": when.isoformat(),
        "event_id": event, "market_key": "h2h", "selection": event,
        "point": None, "sport_key": "soccer_epl", "status": "QUEUED",
        "home_team": event, "away_team": "Away",
        "commence_time": (now+timedelta(hours=2)).isoformat(),
        "min_odds": minimum,
    }


def _q(price, minutes_ago):
    return {"price": price,
            "captured_at": (_at()-timedelta(minutes=minutes_ago)).isoformat()}


def _pool():
    now=_at()
    early=_c("E1", "HR2_FOOTBALL", now-timedelta(minutes=20))
    late=_c("E2", "NCAAF_3_TO_4_99_EXPERIMENTAL",
            now-timedelta(minutes=4), minimum=3.0)
    waiting=_c("E3", "HR2_FOOTBALL", now-timedelta(minutes=3))
    watch=_c("E4", "SHADOW_WATCH", now-timedelta(minutes=2))
    quotes={
        "williamhill": [(early,_q(2.01,2)),(late,_q(3.24,1))],
        "ladbrokes_uk": [(late,_q(3.18,1))],
    }
    return [early,late,waiting,watch], {"HR2_FOOTBALL","NCAAF_3_TO_4_99_EXPERIMENTAL"}, quotes


def test_one_snapshot_feeds_both_views_without_any_provider_access(tmp_path):
    db=Database(db_path=str(tmp_path/"queue.sqlite"))
    candidates,eligible,quotes=_pool()
    result=queues.capture(db,_at(),candidates,eligible,quotes)
    assert result["updated"]
    assert result["source_candidates"]==4
    port=queues.read(db,view="portfolio")
    rank=queues.read(db,view="quality")
    assert port["snapshot_at"]==rank["snapshot_at"]
    assert port["source_candidates"]==4
    assert port["eligible_candidates"]==3
    assert port["quoted_candidates"]==2
    assert port["waiting_price_candidates"]==1
    assert port["excluded_candidates"]==1
    assert len(port["rows"])==3
    assert len(port["waiting_rows"])==2
    assert port["bookmakers"]["williamhill"]["baseline_ready"] is True
    assert port["bookmakers"]["ladbrokes_uk"]["distinct_events"]==1
    assert [x["event_id"] for x in port["rows"] if x["bookmaker_key"]=="williamhill"]==["E1","E2"]
    assert [x["event_id"] for x in rank["rows"] if x["bookmaker_key"]=="williamhill"]==["E2","E1"]
    assert rank["rows"][0]["quality_score"] is not None
    assert "preview" in rank["note"]
    assert result["no_extra_quote_polls"] is True


def test_queue_is_idempotently_cached_for_ninety_seconds(tmp_path):
    db=Database(db_path=str(tmp_path/"queue.sqlite"))
    cs,eligible,quotes=_pool()
    assert queues.capture(db,_at(),cs,eligible,quotes)["updated"]
    second=queues.capture(db,_at()+timedelta(seconds=10),cs,eligible,quotes)
    assert second == {"updated":False,"reason":"recent_snapshot"}
    assert db.fetchone("SELECT COUNT(*) AS n FROM hr_portfolio_queue_rows")["n"]==5
    moved=_at()+timedelta(minutes=2)
    assert queues.capture(db,moved,[],set(),{})["updated"]
    assert db.fetchone("SELECT COUNT(*) AS n FROM hr_portfolio_queue_rows")["n"]==0
    assert queues.read(db,view="quality")["source_candidates"]==0


def test_when_no_snapshot_pages_render_accurate_empty_state(tmp_path):
    db=Database(db_path=str(tmp_path/"queue.sqlite"))
    waiting=queues.read(db,view="quality")
    assert waiting["status"]=="AWAITING_FIRST_SNAPSHOT"
    assert "Awaiting first cached queue snapshot" in ui.render_quality(
        {"paired":0,"pending":0,"baseline_pnl_units":0,"quality_pnl_units":0,
         "paired_delta_pnl_units":0,"quality_wins":0,"baseline_wins":0,
         "same_result":0,"formats":[],"latest":[],"skipped":{}},
        queue=waiting,
    )


def test_table_sanitizes_fixture_and_selection_strings(tmp_path):
    db=Database(db_path=str(tmp_path/"queue.sqlite"))
    cs,eligible,quotes=_pool()
    cs[0]["home_team"]="<script>bad()</script>"
    queues.capture(db,_at(),cs,eligible,quotes)
    html=ui.render_portfolio({"portfolio":[],"formats":[],"lane_metrics":[],
                              "lane_policy":[],"latest_groups":[],
                              "recent_ncaaf_mix":{}},
                             queue=queues.read(db,view="portfolio"))
    assert "<script>bad()</script>" not in html
    assert "&lt;script&gt;bad()&lt;/script&gt;" in html
    assert "Currently priced queue" in html
    assert "Awaiting prices or excluded" in html


def test_unpriced_items_are_visible_even_with_priced_items(tmp_path):
    db=Database(db_path=str(tmp_path/"queue.sqlite"))
    cs,eligible,quotes=_pool()
    queues.capture(db,_at(),cs,eligible,quotes)
    payload=queues.read(db,view="portfolio")
    missing={x["event_id"]:x["queue_status"] for x in payload["waiting_rows"]}
    assert missing == {"E3":"WAITING_VALID_QUOTE","E4":"WATCH_OR_PAUSED"}
    html=ui.render_portfolio({"portfolio":[],"formats":[],"lane_metrics":[],
                              "lane_policy":[],"latest_groups":[],
                              "recent_ncaaf_mix":{}},queue=payload)
    assert "E3 v Away" in html
    assert "WATCH/PAUSED source" in html
