from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, Response

from config import settings
from db import Database
from the_odds_api import TheOddsApi
from quota import QuotaGuard
from collector import Collector
from signals import write_consensus_and_value_signals, write_consensus_snapshots_only, write_cross_market_signals, write_slow_book_signals
from closing import settle_signal
from results import ResultCollector
from canonical import (
    cluster_event_signals, backfill_canonical_bets, sync_canonical_bets,
    canonical_scoreboard, latest_canonical_bets,
)
from worker import Worker
from research_intelligence import (
    research_intelligence, upsert_weekly_report, latest_weekly_reports,
)
from execution_shadow import (
    backfill_execution_shadows, evaluate_latest_execution_wave, track_execution_prices,
    finalize_execution_clv, settle_execution_from_stored_results, execution_scoreboard,
    latest_execution_bets, execution_funnel, backfill_execution_accounting,
)
from exporter import build_research_export
from multiples_shadow import (
    generate_multiple_shadows, finalize_multiple_clv, settle_multiple_shadows,
    multiples_scoreboard, latest_multiple_shadows,
)
from manual_systems_shadow import (
    ManualSystemsShadowEngine, manual_systems_scoreboard, latest_manual_system_cards,
    generate_manual_system_shadows, finalize_manual_system_clv, settle_manual_system_shadows,
)
from instrumentation import instrumentation_report, run_measurement_maintenance
from tennis_shadow import (
    TennisShadowEngine, tennis_scoreboard, tennis_segments, latest_tennis_bets,
)
from multisport_shadow import (
    MultiSportShadowEngine, multisport_scoreboard, multisport_segments,
    latest_multisport_bets, multisport_funnel,
)
from multisport_lines_shadow import (
    MultiSportLinesEngine, line_scoreboard, line_segments, line_funnel,
    latest_line_bets,
)
from predictive_football import (
    PredictiveFootballEngine, predictive_scoreboard, predictive_funnel,
    predictive_league_summary, latest_predictive_bets,
    predictive_market_summary, predictive_market_funnel,
    latest_predictive_market_bets, predictive_bootstrap_status,
)
from predictive_football_pred2 import (
    PredictiveFootballPred2Engine, predictive2_scoreboard, predictive2_funnel,
    predictive2_league_summary, latest_predictive2_bets,
    predictive2_market_summary, predictive2_market_funnel,
    latest_predictive2_market_bets,
)
from predictive_football_pred3 import (
    PredictiveFootballPred3Engine, predictive3_scoreboard, predictive3_funnel,
    predictive3_league_summary, latest_predictive3_bets,
    predictive3_market_summary, predictive3_market_funnel,
    latest_predictive3_market_bets, predictive3_bootstrap_status,
)
from predictive_football_pred4 import (
    PredictiveFootballPred4Engine, predictive4_scoreboard, predictive4_funnel,
    predictive4_league_summary, latest_predictive4_bets,
    predictive4_market_summary, predictive4_market_funnel,
    latest_predictive4_market_bets, predictive4_bootstrap_status,
)
from predictive_historical import (
    PredictiveHistoricalValidator, historical_validation_summary,
)
from proxy_xg import ProxyXgEngine, proxy_xg_status
from outcome_edge import outcome_edge_report
from cohort_systems_shadow import (
    ensure_cohort_system_state, run_cohort_systems_maintenance, cohort_systems_scoreboard,
    latest_cohort_system_cards, settle_cohort_system_shadows,
)
from meta_edge import (
    run_meta_edge_maintenance, meta_edge_scoreboard, meta_edge_segments,
    latest_meta_edge_samples,
)
from meta_edge_model import (
    run_meta_model_maintenance, meta_model_status, latest_meta_model_scores,
)
from research import (
    track_signal_prices, finalize_closing_lines, strategy_scoreboard,
    rejection_summary, latest_evaluations, event_market_snapshot,
    event_price_history, signal_price_history, repair_premature_clv,
)

VERSION = "0.19.2"

db = Database(settings.database_url, settings.db_path)
api = TheOddsApi(settings.odds_api_key)
quota = QuotaGuard(db,reserve=settings.quota_reserve_credits,daily_budget=settings.daily_paid_credit_budget)
collector = Collector(
    db,api,quota,sport_keys=settings.sport_keys,region=settings.odds_region,
    markets=settings.odds_markets,max_events_per_cycle=settings.max_events_per_odds_cycle,
    breadth_polls_per_day=settings.breadth_polls_per_day,
    execution_bookmaker_keys=settings.execution_bookmaker_keys,
    predictive_high_res_price_path_enabled=settings.predictive_football_high_res_price_path_enabled,
    predictive_broad_close_enabled=settings.predictive_football_broad_close_enabled,
    predictive_broad_close_hours_before=settings.predictive_football_broad_close_hours_before,
)
result_collector = ResultCollector(
    db, api, quota,
    enabled=settings.enable_score_collection,
    min_minutes_after_kickoff=settings.result_min_minutes_after_kickoff,
    min_poll_interval_seconds=settings.result_poll_min_interval_seconds,
)
tennis_engine = TennisShadowEngine(
    db,api,settings,execution_bookmaker_keys=settings.execution_bookmaker_keys,
)
multisport_engine = MultiSportShadowEngine(
    db,api,settings,execution_bookmaker_keys=settings.execution_bookmaker_keys,
)
multisport_lines_engine = MultiSportLinesEngine(
    db,api,settings,execution_bookmaker_keys=settings.execution_bookmaker_keys,
)
predictive_football_engine = PredictiveFootballEngine(
    db,settings,execution_bookmaker_keys=settings.execution_bookmaker_keys,
)
predictive_football_pred2_engine = PredictiveFootballPred2Engine(
    db,settings,execution_bookmaker_keys=settings.execution_bookmaker_keys,
)
predictive_football_pred3_engine = PredictiveFootballPred3Engine(
    db,settings,execution_bookmaker_keys=settings.execution_bookmaker_keys,
)
proxy_xg_engine = ProxyXgEngine(db, settings)
predictive_football_pred4_engine = PredictiveFootballPred4Engine(
    db,settings,execution_bookmaker_keys=settings.execution_bookmaker_keys,
)
predictive_historical_engine = PredictiveHistoricalValidator(
    db,api,settings,predictive_football_engine,predictive_football_pred2_engine,
    execution_bookmaker_keys=settings.execution_bookmaker_keys,
)
manual_systems_engine = ManualSystemsShadowEngine(db, collector, settings)
worker = Worker(
    db,collector,min_books=settings.min_consensus_books,min_edge_pct=settings.min_edge_pct,
    min_cross_market_edge_pct=settings.min_cross_market_edge_pct,
    min_slow_book_gap_pct=settings.min_slow_book_gap_pct,
    tick_seconds=settings.worker_tick_seconds,discovery_interval_seconds=settings.discovery_interval_seconds,
    result_collector=result_collector,
    execution_bookmaker_keys=settings.execution_bookmaker_keys,
    multiples_api_bookmaker_keys=settings.multiples_api_bookmaker_keys,
    execution_shadow_enabled=settings.execution_shadow_enabled,
    tennis_engine=tennis_engine,
    multisport_engine=multisport_engine,
    multisport_lines_engine=multisport_lines_engine,
    predictive_football_engine=predictive_football_engine,
    predictive_football_pred2_engine=predictive_football_pred2_engine,
    predictive_football_pred3_engine=predictive_football_pred3_engine,
    proxy_xg_engine=proxy_xg_engine,
    predictive_football_pred4_engine=predictive_football_pred4_engine,
    predictive_historical_engine=predictive_historical_engine,
    manual_systems_engine=manual_systems_engine,
    meta_edge_enabled=settings.meta_edge_enabled,
    meta_edge_model_enabled=settings.meta_edge_model_enabled,
    meta_edge_min_clean_labels=settings.meta_edge_min_clean_labels,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_schema()
    # Freeze v0.19 discovery hypotheses immediately at deployment/startup so
    # all subsequent observations are clean forward validation.
    outcome_edge_report(db)
    ensure_cohort_system_state(db)
    repair_premature_clv(db)
    backfill_canonical_bets(db)
    sync_canonical_bets(db)
    if settings.execution_shadow_enabled:
        backfill_execution_shadows(db,settings.execution_bookmaker_keys)
        track_execution_prices(db)
        finalize_execution_clv(db)
        settle_execution_from_stored_results(db)
        backfill_execution_accounting(db)
    # v0.6.9 measurement-only instrumentation. No provider/API calls.
    run_measurement_maintenance(
        db, excluded_books=settings.execution_bookmaker_keys
    )
    # Forward-only downstream research layer. No historical multiple backfill
    # and no provider/API calls are made here.
    generate_multiple_shadows(db, allowed_bookmaker_keys=settings.multiples_api_bookmaker_keys)
    finalize_multiple_clv(db)
    settle_multiple_shadows(db)
    upsert_weekly_report(db)
    if settings.tennis_shadow_enabled and settings.odds_api_key:
        # Free active-tournament discovery; paid polling remains worker-controlled.
        try:
            tennis_engine.discover_active_tournaments()
            tennis_engine.maintenance()
        except Exception as exc:
            db.record_collector_run("TENNIS_STARTUP",False,detail=str(exc))
    if settings.multisport_shadow_enabled and settings.odds_api_key:
        try:
            multisport_engine.discover_active_leagues()
            multisport_engine.maintenance()
        except Exception as exc:
            db.record_collector_run("MULTISPORT_STARTUP",False,detail=str(exc))
    if settings.multisport_lines_enabled and settings.odds_api_key:
        try:
            multisport_lines_engine.discover_active_leagues()
            multisport_lines_engine.maintenance()
        except Exception as exc:
            db.record_collector_run("MULTISPORT_LINES_STARTUP",False,detail=str(exc))
    if settings.predictive_football_enabled:
        try:
            # v0.11.2: bootstrap one paced batch at application startup.
            # This is deliberately independent of RUN_WORKER and ODDS_API_KEY,
            # because historical score warm-up uses the free score source and
            # must never sit at 0/0 simply because the collector worker did not
            # start. Subsequent batches continue through the normal worker.
            # Bootstrap first. A failure in syncing existing internal results
            # must never prevent the independent historical warm-up.
            predictive_football_engine.bootstrap_historical_data()
            predictive_football_engine.sync_internal_results()
            predictive_football_engine.freeze_due_predictions()
            predictive_football_engine.ensure_market_predictions()
            predictive_football_engine.evaluate_predictions()
            predictive_football_engine.evaluate_market_predictions()
            predictive_football_engine.track_prices()
            predictive_football_engine.track_market_prices()
            predictive_football_engine.finalize_clv()
            predictive_football_engine.finalize_market_clv()
            predictive_football_engine.finalize_prediction_market_close()
            predictive_football_engine.settle()
            predictive_football_engine.settle_market_predictions()
        except Exception as exc:
            db.record_collector_run("PREDICTIVE_FOOTBALL_STARTUP",False,detail=str(exc))
    if settings.predictive_football_pred2_enabled:
        try:
            # PRED2 is an isolated challenger that reuses PRED1's stored
            # score history and already-collected odds snapshots. It does not
            # bootstrap a second data source or place live bets.
            predictive_football_pred2_engine.one_cycle()
            db.record_collector_run(
                "PREDICTIVE_FOOTBALL_PRED2_STARTUP",True,
                detail="PRED2 Dixon-Coles challenger initialized",
            )
        except Exception as exc:
            db.record_collector_run(
                "PREDICTIVE_FOOTBALL_PRED2_STARTUP",False,detail=str(exc)
            )
    if settings.predictive_football_pred3_enabled:
        # StatsBomb Open Data is fetched by the normal worker rather than in
        # the FastAPI lifespan so GitHub/network latency cannot delay web startup.
        db.record_collector_run(
            "PREDICTIVE_FOOTBALL_PRED3_STARTUP",True,
            detail="PRED3 StatsBomb xG challenger enabled; bootstrap deferred to worker",
        )
    if settings.predictive_football_pred4_enabled:
        db.record_collector_run(
            "PREDICTIVE_FOOTBALL_PRED4_STARTUP",True,
            detail="PRED4 current PXG1 challenger enabled; consumes stored PXG1 data only",
        )
    if settings.meta_edge_enabled:
        try:
            out = run_meta_edge_maintenance(db)
            db.record_collector_run(
                "META_EDGE_STARTUP", True,
                detail="; ".join(f"{k}={v}" for k,v in out.items()),
            )
            meta2 = run_meta_model_maintenance(
                db, settings.meta_edge_min_clean_labels, settings.meta_edge_model_enabled
            )
            db.record_collector_run(
                "META_EDGE_MODEL_STARTUP", True,
                detail="; ".join(f"{k}={v}" for k,v in meta2.items()),
            )
        except Exception as exc:
            db.record_collector_run("META_EDGE_STARTUP",False,detail=str(exc))
    if settings.enable_live_betting:
        raise RuntimeError("Betting Lab is shadow-only; disable ENABLE_LIVE_BETTING")
    if settings.run_worker and settings.odds_api_key:
        worker.start()
    yield
    worker.stop()

app=FastAPI(title="Project Exit Plan — Betting Lab",version=VERSION,lifespan=lifespan)

def _admin(secret: Optional[str]):
    if secret != settings.admin_secret: raise HTTPException(status_code=403,detail="invalid admin secret")

def _fmt(value,digits=2):
    if value is None:return "—"
    try:return f"{float(value):.{digits}f}"
    except Exception:return escape(str(value))

def _market_label(key):
    return {"h2h":"1X2","totals":"O/U","btts":"BTTS","draw_no_bet":"DNB"}.get(key,key)

def bets_until_midnight(db: Database, now: Optional[datetime] = None) -> int:
    """
    Count OPEN executable-shadow bets whose fixture kicks off between now and
    midnight in the UK (Europe/London). Multiple executable bets on one fixture
    count separately because this tile is explicitly a bet count.
    """
    london = ZoneInfo("Europe/London")
    if now is None:
        now_local = datetime.now(london)
    elif now.tzinfo is None:
        now_local = now.replace(tzinfo=london)
    else:
        now_local = now.astimezone(london)

    midnight_local = datetime(
        now_local.year, now_local.month, now_local.day,
        tzinfo=london,
    ).replace(day=now_local.day)
    # Advance to next local calendar day rather than adding 24 hours so DST
    # transitions still resolve to the correct UK midnight.
    from datetime import timedelta
    midnight_local = midnight_local + timedelta(days=1)

    now_utc = now_local.astimezone(timezone.utc)
    midnight_utc = midnight_local.astimezone(timezone.utc)

    rows = db.fetchall(
        """
        SELECT x.id,e.commence_time
        FROM execution_shadow_bets x
        JOIN events e ON e.event_id=x.event_id
        WHERE x.status='OPEN'
        """
    )

    count = 0
    for row in rows:
        raw = str(row.get("commence_time") or "")
        if not raw:
            continue
        try:
            kickoff = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=timezone.utc)
        kickoff = kickoff.astimezone(timezone.utc)
        if now_utc <= kickoff < midnight_utc:
            count += 1
    return count


@app.get('/health')
def health():
    return {"ok":True,"app":"Betting Lab","version":VERSION,"shadow_only":True,"provider_configured":bool(settings.odds_api_key)}

@app.get('/api/status')
def status():
    q=quota.state()
    count=lambda table,where='': (db.fetchone(f"SELECT COUNT(*) AS n FROM {table} {where}") or {}).get('n',0)
    scoreboard=strategy_scoreboard(db)
    canonical=canonical_scoreboard(db)
    execution=execution_scoreboard(db)
    funnel=execution_funnel(db)
    multiples=multiples_scoreboard(db)
    manual_systems=manual_systems_scoreboard(db)
    tennis=tennis_scoreboard(db)
    multisport=multisport_scoreboard(db)
    multisport_lines=line_scoreboard(db)
    predictive=predictive_scoreboard(db)
    predictive2=predictive2_scoreboard(db)
    predictive3=predictive3_scoreboard(db)
    predictive4=predictive4_scoreboard(db)
    pxg=proxy_xg_status(db,settings)
    outcome_edge=outcome_edge_report(db)
    cohort_systems=cohort_systems_scoreboard(db)
    predictive_historical=historical_validation_summary(db)
    meta_edge=meta_edge_scoreboard(db,settings.meta_edge_min_clean_labels)
    meta_edge_model=meta_model_status(db)
    until_midnight=bets_until_midnight(db)
    clv_samples=int(execution['clv_samples'])
    return {
        "app":"Betting Lab","version":VERSION,"shadow_only":True,
        "sports":settings.sport_keys,"markets":settings.odds_markets,"quota":q,
        "today_paid_cost":quota.today_paid_cost(),"daily_paid_credit_budget":settings.daily_paid_credit_budget,
        "poll_mode":"broad_discovery_then_narrow_execution_convergence",
        "breadth_polls_per_day":settings.breadth_polls_per_day,
        "broad_odds_cost":collector.broad_cost,
        "narrow_convergence_cost":collector.convergence_cost,
        "dnb_collection_enabled":settings.enable_dnb_market,
        "counts":{"events":count('events'),"quotes":count('odds_snapshots'),"signals":count('signals'),
                  "settled":count('signals',"WHERE status='SETTLED'"),"evaluations":count('candidate_evaluations'),
                  "price_observations":count('signal_price_observations'),
                  "results":count('event_results'),
                  "canonical_bets":count('canonical_bets'),
                  "canonical_settled":count('canonical_bets',"WHERE status='SETTLED'"),
                  "execution_bets":count('execution_shadow_bets'),
                  "execution_settled":count('execution_shadow_bets',"WHERE status='SETTLED'"),
                  "execution_evaluations":count('execution_evaluations'),
                  "execution_price_observations":count('execution_price_observations'),
                  "multiple_shadow_bets":count('multiple_shadow_bets'),
                  "multiple_shadow_settled":count('multiple_shadow_bets',"WHERE status='SETTLED'"),
                  "multiple_shadow_legs":count('multiple_shadow_legs'),
                  "manual_system_shadow_bets":count('manual_system_shadow_bets'),
                  "manual_system_shadow_settled":count('manual_system_shadow_bets',"WHERE status='SETTLED'"),
                  "manual_system_shadow_legs":count('manual_system_shadow_legs'),
                  "cohort_system_shadow_bets":count('cohort_system_shadow_bets'),
                  "cohort_system_shadow_settled":count('cohort_system_shadow_bets',"WHERE status='SETTLED'"),
                  "cohort_system_shadow_legs":count('cohort_system_shadow_legs')},
        "provider_credits_used":q.get("credits_used"),
        "score_collection_enabled":settings.enable_score_collection,
        "database_backend":"postgres" if db.is_postgres else "sqlite",
        "execution_shadow_enabled":settings.execution_shadow_enabled,
        "execution_bookmaker_keys":settings.execution_bookmaker_keys,
        "avg_clv_pct":execution['avg_clv_pct'],
        "bets_until_midnight":until_midnight,
        "bets_until_midnight_timezone":"Europe/London",
        "clv_samples":clv_samples,"scoreboard":scoreboard,
        "canonical_scoreboard":canonical,"execution_scoreboard":execution,"execution_funnel":funnel,
        "multiples_shadow":multiples,
        "manual_systems_shadow":manual_systems,
        "cohort_systems_shadow":cohort_systems,
        "manual_systems_enabled":settings.manual_systems_enabled,
        "manual_systems_placeable_bookmaker_keys":settings.manual_systems_placeable_bookmaker_keys,
        "manual_systems_comparison_bookmaker_keys":settings.manual_systems_comparison_bookmaker_keys,
        "manual_systems_daily_credit_budget":settings.manual_systems_daily_credit_budget,
        "multiples_api_bookmaker_keys":settings.multiples_api_bookmaker_keys,
        "multiples_formation_paused":not bool(settings.multiples_api_bookmaker_keys),
        "tennis_shadow_enabled":settings.tennis_shadow_enabled,
        "tennis_paid_credits_today":tennis_engine.quota.today_paid_cost(),
        "tennis_daily_paid_credit_budget":settings.tennis_daily_paid_credit_budget,
        "tennis_shadow":tennis,
        "multisport_shadow_enabled":settings.multisport_shadow_enabled,
        "multisport_paid_credits_today":multisport_engine.quota.today_paid_cost(),
        "multisport_daily_paid_credit_budget":settings.multisport_daily_paid_credit_budget,
        "multisport_target_sports":settings.multisport_sport_keys,
        "multisport_shadow":multisport,
        "multisport_lines_enabled":settings.multisport_lines_enabled,
        "multisport_lines_markets":settings.multisport_lines_markets,
        "multisport_lines_shadow":multisport_lines,
        "predictive_football_enabled":settings.predictive_football_enabled,
        "predictive_football":predictive,
        "predictive_football_pred2_enabled":settings.predictive_football_pred2_enabled,
        "predictive_football_pred2":predictive2,
        "predictive_football_pred3_enabled":settings.predictive_football_pred3_enabled,
        "predictive_football_pred3":predictive3,
        "predictive_football_pred4_enabled":settings.predictive_football_pred4_enabled,
        "predictive_football_pred4":predictive4,
        "proxy_xg":pxg,
        "outcome_edge":outcome_edge,
        "predictive_football_high_res_price_path_enabled":settings.predictive_football_high_res_price_path_enabled,
        "predictive_football_broad_close_enabled":settings.predictive_football_broad_close_enabled,
        "predictive_football_broad_close_hours_before":settings.predictive_football_broad_close_hours_before,
        "predictive_football_historical_enabled":settings.predictive_football_historical_enabled,
        "predictive_football_historical_paid_credits_today":predictive_historical_engine.today_paid_cost(),
        "predictive_football_historical_daily_credit_budget":settings.predictive_football_historical_daily_credit_budget,
        "predictive_football_historical":predictive_historical,
        "meta_edge_enabled":settings.meta_edge_enabled,
        "meta_edge":meta_edge,
        "meta_edge_model_enabled":settings.meta_edge_model_enabled,
        "meta_edge_model":meta_edge_model,
    }

@app.get('/api/proxy-xg/status')
def api_proxy_xg_status():
    
    return proxy_xg_status(db,settings)

@app.post('/admin/proxy-xg/run')
def admin_proxy_xg_run(x_admin_secret:Optional[str]=Header(None)):
    _admin(x_admin_secret);return proxy_xg_engine.one_cycle()

@app.get('/api/outcome-edge')
def api_outcome_edge():
    return outcome_edge_report(db)

@app.get('/api/meta-edge/status')
def api_meta_edge_status():
    
    return meta_edge_scoreboard(db,settings.meta_edge_min_clean_labels)

@app.get('/api/meta-edge/segments')
def api_meta_edge_segments():
    
    return meta_edge_segments(db)

@app.get('/api/meta-edge/samples')
def api_meta_edge_samples(limit:int=Query(100,ge=1,le=1000)):
    
    return latest_meta_edge_samples(db,limit)

@app.get('/api/meta-edge/model')
def api_meta_edge_model():
    
    return meta_model_status(db)

@app.get('/api/meta-edge/model/scores')
def api_meta_edge_model_scores(limit:int=Query(100,ge=1,le=1000)):
    
    return latest_meta_model_scores(db,limit)

@app.post('/admin/meta-edge/run')
def admin_meta_edge_run(x_admin_secret:Optional[str]=Header(None)):
    _admin(x_admin_secret);
    meta1=run_meta_edge_maintenance(db)
    meta2=run_meta_model_maintenance(
        db, settings.meta_edge_min_clean_labels, settings.meta_edge_model_enabled
    )
    return {"meta1":meta1,"meta2":meta2}

@app.get('/api/events')
def events(limit:int=Query(100,ge=1,le=500)):
    return db.fetchall("SELECT * FROM events ORDER BY commence_time ASC LIMIT ?",(limit,))

@app.get('/api/signals')
def signals(limit:int=Query(100,ge=1,le=1000)):
    return db.fetchall("""SELECT s.*,e.league,e.home_team,e.away_team,e.commence_time FROM signals s JOIN events e ON e.event_id=s.event_id ORDER BY s.id DESC LIMIT ?""",(limit,))

@app.get('/api/consensus')
def consensus(limit:int=Query(100,ge=1,le=1000)):
    return db.fetchall("SELECT * FROM consensus_snapshots ORDER BY id DESC LIMIT ?",(limit,))

@app.get('/api/canonical-bets')
def canonical_bets(limit:int=Query(100,ge=1,le=1000)):
    return latest_canonical_bets(db,limit)

@app.get('/api/execution-bets')
def execution_bets(limit:int=Query(100,ge=1,le=1000)):
    return latest_execution_bets(db,limit)

@app.get('/api/execution/evaluations')
def execution_evaluations(limit:int=Query(100,ge=1,le=1000)):
    return db.fetchall("SELECT * FROM execution_evaluations ORDER BY id DESC LIMIT ?",(limit,))

@app.get('/api/execution/scoreboard')
def execution_scoreboard_api():
    return execution_scoreboard(db)

@app.get('/api/multiples/scoreboard')
def multiples_scoreboard_api():
    return multiples_scoreboard(db)

@app.get('/api/multiples/bets')
def multiples_bets_api(
    limit:int=Query(100,ge=1,le=1000),
    include_legacy:bool=Query(False),
):
    
    return latest_multiple_shadows(db,limit,include_legacy=include_legacy)

@app.get('/api/manual-systems/status')
def manual_systems_status_api():
    
    return {
        "enabled":settings.manual_systems_enabled,
        "placeable_bookmaker_keys":settings.manual_systems_placeable_bookmaker_keys,
        "comparison_bookmaker_keys":settings.manual_systems_comparison_bookmaker_keys,
        "system_types":settings.manual_systems_types,
        "source_cohorts":settings.manual_systems_source_cohorts,
        "daily_credit_budget":settings.manual_systems_daily_credit_budget,
        "scoreboard":manual_systems_scoreboard(db),
    }

@app.get('/api/manual-systems/cards')
def manual_systems_cards_api(
    limit:int=Query(100,ge=1,le=1000),
    manual_only:bool=Query(False),
):
    
    return latest_manual_system_cards(db,limit,manual_only=manual_only)

@app.get('/api/cohort-systems/status')
def cohort_systems_status_api():
    
    return cohort_systems_scoreboard(db)

@app.get('/api/cohort-systems/cards')
def cohort_systems_cards_api(limit:int=Query(100,ge=1,le=1000)):
    
    return latest_cohort_system_cards(db,limit)

@app.get('/api/tennis/status')
def tennis_status_api():
    
    return {
        "enabled":settings.tennis_shadow_enabled,
        "market":settings.tennis_market,
        "execution_bookmaker_keys":settings.execution_bookmaker_keys,
        "paid_credits_today":tennis_engine.quota.today_paid_cost(),
        "daily_paid_credit_budget":settings.tennis_daily_paid_credit_budget,
        "scoreboard":tennis_scoreboard(db),
        "segments":tennis_segments(db),
    }

@app.get('/api/tennis/bets')
def tennis_bets_api(limit:int=Query(100,ge=1,le=1000)):
    return latest_tennis_bets(db,limit)

@app.get('/api/tennis/evaluations')
def tennis_evaluations_api(limit:int=Query(100,ge=1,le=1000)):
    
    return db.fetchall(
        "SELECT * FROM tennis_execution_evaluations ORDER BY id DESC LIMIT ?",(limit,)
    )

@app.get('/api/tennis/tournaments')
def tennis_tournaments_api():
    
    return db.fetchall(
        "SELECT * FROM tennis_tournament_state ORDER BY active DESC,tour,title"
    )

@app.get('/api/multisport/status')
def multisport_status_api():
    
    return {
        "enabled":settings.multisport_shadow_enabled,
        "market":settings.multisport_market,
        "region":settings.multisport_odds_region,
        "hockey_reference_region":settings.multisport_hockey_reference_region,
        "target_sports":settings.multisport_sport_keys,
        "execution_bookmaker_keys":settings.execution_bookmaker_keys,
        "paid_credits_today":multisport_engine.quota.today_paid_cost(),
        "daily_paid_credit_budget":settings.multisport_daily_paid_credit_budget,
        "scoreboard":multisport_scoreboard(db),
        "segments":multisport_segments(db),
    }

@app.get('/api/multisport/bets')
def multisport_bets_api(limit:int=Query(100,ge=1,le=1000)):
    return latest_multisport_bets(db,limit)

@app.get('/api/multisport/evaluations')
def multisport_evaluations_api(limit:int=Query(100,ge=1,le=1000)):
    
    return db.fetchall(
        "SELECT * FROM multisport_execution_evaluations ORDER BY id DESC LIMIT ?",
        (limit,),
    )

@app.get('/api/multisport/leagues')
def multisport_leagues_api():
    
    return db.fetchall(
        "SELECT * FROM multisport_league_state ORDER BY active DESC,sport_family,title"
    )

@app.get('/api/multisport/funnel')
def multisport_funnel_api():
    return multisport_funnel(db)

@app.get('/api/multisport-lines/status')
def multisport_lines_status_api():
    return {
        'enabled':settings.multisport_lines_enabled,
        'markets':settings.multisport_lines_markets,
        'min_consensus_books':settings.multisport_lines_min_consensus_books,
        'min_edge_pct':settings.multisport_lines_min_edge_pct,
        'paid_credits_today_shared':multisport_lines_engine.quota.today_paid_cost(),
        'shared_daily_budget':settings.multisport_daily_paid_credit_budget,
        'scoreboard':line_scoreboard(db),'segments':line_segments(db),'funnel':line_funnel(db),
    }

@app.get('/api/multisport-lines/bets')
def multisport_lines_bets_api(limit:int=Query(100,ge=1,le=1000)):
    return latest_line_bets(db,limit)

@app.get('/api/multisport-lines/evaluations')
def multisport_lines_evaluations_api(limit:int=Query(100,ge=1,le=1000)):
    return db.fetchall('SELECT * FROM multisport_line_evaluations ORDER BY id DESC LIMIT ?',(limit,))

@app.get('/api/predictive-football/status')
def predictive_football_status_api():
    
    startup_run=db.fetchone(
        """SELECT started_at,finished_at,ok,detail
           FROM collector_runs
           WHERE run_type='PREDICTIVE_FOOTBALL_STARTUP'
           ORDER BY id DESC LIMIT 1"""
    )
    return {
        "enabled":settings.predictive_football_enabled,
        "bootstrap_enabled":settings.predictive_football_bootstrap_enabled,
        "forecast_hours_before":settings.predictive_football_forecast_hours_before,
        "min_edge_pct":settings.predictive_football_min_edge_pct,
        "strong_edge_pct":settings.predictive_football_strong_edge_pct,
        "markets":list(settings.predictive_football_markets),
        "total_points":list(settings.predictive_football_total_points),
        "scoreboard":predictive_scoreboard(db),
        "funnel":predictive_funnel(db),
        "market_funnel":predictive_market_funnel(db),
        "market_summary":predictive_market_summary(db),
        "leagues":predictive_league_summary(db),
        "bootstrap":predictive_bootstrap_status(db),
        "bootstrap_target_sports":predictive_football_engine.bootstrap_target_sports(),
        "startup_run":startup_run,
    }

@app.get('/api/predictive-football/bets')
def predictive_football_bets_api(limit:int=Query(100,ge=1,le=1000)):
    return latest_predictive_bets(db,limit)

@app.get('/api/predictive-football/market-bets')
def predictive_football_market_bets_api(limit:int=Query(100,ge=1,le=1000)):
    return latest_predictive_market_bets(db,limit)

@app.get('/api/predictive-football/market-predictions')
def predictive_football_market_predictions_api(limit:int=Query(100,ge=1,le=1000)):
    
    return db.fetchall(
        "SELECT * FROM football_predictive_market_predictions ORDER BY id DESC LIMIT ?",
        (limit,),
    )

@app.get('/api/predictive-football/market-evaluations')
def predictive_football_market_evaluations_api(limit:int=Query(100,ge=1,le=1000)):
    
    return db.fetchall(
        "SELECT * FROM football_predictive_market_evaluations ORDER BY id DESC LIMIT ?",
        (limit,),
    )

@app.get('/api/predictive-football/predictions')
def predictive_football_predictions_api(limit:int=Query(100,ge=1,le=1000)):
    
    return db.fetchall(
        "SELECT * FROM football_predictive_predictions ORDER BY id DESC LIMIT ?",
        (limit,),
    )

@app.get('/api/predictive-football/evaluations')
def predictive_football_evaluations_api(limit:int=Query(100,ge=1,le=1000)):
    
    return db.fetchall(
        "SELECT * FROM football_predictive_evaluations ORDER BY id DESC LIMIT ?",
        (limit,),
    )

@app.get('/api/predictive-football-pred2/status')
def predictive_football_pred2_status_api():
    
    startup_run=db.fetchone(
        """SELECT started_at,finished_at,ok,detail
           FROM collector_runs
           WHERE run_type='PREDICTIVE_FOOTBALL_PRED2_STARTUP'
           ORDER BY id DESC LIMIT 1"""
    )
    return {
        "enabled":settings.predictive_football_pred2_enabled,
        "model":"PRED2_DIXON_COLES_V1",
        "forecast_hours_before":settings.predictive_football_forecast_hours_before,
        "min_edge_pct":settings.predictive_football_min_edge_pct,
        "strong_edge_pct":settings.predictive_football_strong_edge_pct,
        "rho_grid":{
            "min":settings.predictive_football_pred2_rho_min,
            "max":settings.predictive_football_pred2_rho_max,
            "step":settings.predictive_football_pred2_rho_step,
        },
        "scoreboard":predictive2_scoreboard(db),
        "funnel":predictive2_funnel(db),
        "market_funnel":predictive2_market_funnel(db),
        "market_summary":predictive2_market_summary(db),
        "leagues":predictive2_league_summary(db),
        "startup_run":startup_run,
    }

@app.get('/api/predictive-football-pred2/bets')
def predictive_football_pred2_bets_api(limit:int=Query(100,ge=1,le=1000)):
    return latest_predictive2_bets(db,limit)

@app.get('/api/predictive-football-pred2/market-bets')
def predictive_football_pred2_market_bets_api(limit:int=Query(100,ge=1,le=1000)):
    return latest_predictive2_market_bets(db,limit)

@app.get('/api/predictive-football-pred2/predictions')
def predictive_football_pred2_predictions_api(limit:int=Query(100,ge=1,le=1000)):
    return db.fetchall(
        "SELECT * FROM football_predictive2_predictions ORDER BY id DESC LIMIT ?",(limit,)
    )

@app.get('/api/predictive-football-pred2/market-predictions')
def predictive_football_pred2_market_predictions_api(limit:int=Query(100,ge=1,le=1000)):
    return db.fetchall(
        "SELECT * FROM football_predictive2_market_predictions ORDER BY id DESC LIMIT ?",(limit,)
    )

@app.get('/api/predictive-football-pred3/status')
def predictive_football_pred3_status_api():
    
    startup_run=db.fetchone(
        """SELECT started_at,finished_at,ok,detail FROM collector_runs
           WHERE run_type='PREDICTIVE_FOOTBALL_PRED3_STARTUP'
           ORDER BY id DESC LIMIT 1"""
    )
    manifest=db.fetchone(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status='IMPORTED' THEN 1 ELSE 0 END) AS imported,
                  SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) AS pending,
                  SUM(CASE WHEN status='ERROR' THEN 1 ELSE 0 END) AS errors
           FROM football_predictive3_statsbomb_manifest"""
    ) or {}
    latest=db.fetchone(
        "SELECT MAX(played_at) AS latest FROM football_predictive3_training_matches"
    ) or {}
    return {
        "enabled":settings.predictive_football_pred3_enabled,
        "model":"PRED3_STATSBOMB_XG_POISSON_V1",
        "source":"StatsBomb Open Data",
        "forecast_hours_before":settings.predictive_football_forecast_hours_before,
        "max_data_age_days":settings.predictive_football_pred3_max_data_age_days,
        "statsbomb_matches_per_cycle":settings.predictive_football_pred3_statsbomb_matches_per_cycle,
        "scoreboard":predictive3_scoreboard(db),
        "funnel":predictive3_funnel(db),
        "market_funnel":predictive3_market_funnel(db),
        "market_summary":predictive3_market_summary(db),
        "leagues":predictive3_league_summary(db),
        "bootstrap":predictive3_bootstrap_status(db),
        "statsbomb_manifest":manifest,
        "latest_statsbomb_match_at":latest.get("latest"),
        "startup_run":startup_run,
    }

@app.get('/api/predictive-football-pred3/bets')
def predictive_football_pred3_bets_api(limit:int=Query(100,ge=1,le=1000)):
    return latest_predictive3_bets(db,limit)

@app.get('/api/predictive-football-pred3/market-bets')
def predictive_football_pred3_market_bets_api(limit:int=Query(100,ge=1,le=1000)):
    return latest_predictive3_market_bets(db,limit)

@app.get('/api/predictive-football-pred3/predictions')
def predictive_football_pred3_predictions_api(limit:int=Query(100,ge=1,le=1000)):
    return db.fetchall(
        "SELECT * FROM football_predictive3_predictions ORDER BY id DESC LIMIT ?",(limit,)
    )

@app.post('/admin/predictive-football-pred3/run')
def admin_predictive_football_pred3_run(x_admin_secret:Optional[str]=Header(None)):
    _admin(x_admin_secret);return predictive_football_pred3_engine.one_cycle()

@app.get('/api/predictive-football-pred4/status')
def predictive_football_pred4_status_api():
    
    latest=db.fetchone(
        "SELECT MAX(played_at) AS latest FROM football_pxg_current_matches WHERE home_proxy_xg IS NOT NULL AND away_proxy_xg IS NOT NULL"
    ) or {}
    return {
        "enabled":settings.predictive_football_pred4_enabled,
        "model":"PRED4_PROXY_XG_POISSON_V1",
        "source":"PXG1 current API-Football proxy-xG",
        "forecast_hours_before":settings.predictive_football_forecast_hours_before,
        "min_team_matches":settings.predictive_football_pred4_min_team_matches,
        "lookback_days":settings.predictive_football_pred4_lookback_days,
        "max_data_age_days":settings.predictive_football_pred4_max_data_age_days,
        "scoreboard":predictive4_scoreboard(db),
        "funnel":predictive4_funnel(db),
        "market_funnel":predictive4_market_funnel(db),
        "market_summary":predictive4_market_summary(db),
        "leagues":predictive4_league_summary(db),
        "bootstrap":predictive4_bootstrap_status(db),
        "latest_pxg_match_at":latest.get("latest"),
    }

@app.get('/api/predictive-football-pred4/bets')
def predictive_football_pred4_bets_api(limit:int=Query(100,ge=1,le=1000)):
    return latest_predictive4_bets(db,limit)

@app.get('/api/predictive-football-pred4/market-bets')
def predictive_football_pred4_market_bets_api(limit:int=Query(100,ge=1,le=1000)):
    return latest_predictive4_market_bets(db,limit)

@app.get('/api/predictive-football-pred4/predictions')
def predictive_football_pred4_predictions_api(limit:int=Query(100,ge=1,le=1000)):
    return db.fetchall(
        "SELECT * FROM football_predictive4_predictions ORDER BY id DESC LIMIT ?",(limit,)
    )

@app.post('/admin/predictive-football-pred4/run')
def admin_predictive_football_pred4_run(x_admin_secret:Optional[str]=Header(None)):
    _admin(x_admin_secret);return predictive_football_pred4_engine.one_cycle()

@app.get('/api/predictive-football/compare')
def predictive_football_compare_api():
    
    p1=predictive_scoreboard(db);p2=predictive2_scoreboard(db);p3=predictive3_scoreboard(db);p4=predictive4_scoreboard(db)
    paired=(db.fetchone(
        """SELECT COUNT(*) AS n FROM football_predictive_predictions p1
           JOIN football_predictive2_predictions p2 ON p2.event_id=p1.event_id"""
    ) or {}).get("n",0)
    paired_settled=(db.fetchone(
        """SELECT COUNT(*) AS n FROM football_predictive_predictions p1
           JOIN football_predictive2_predictions p2 ON p2.event_id=p1.event_id
           WHERE p1.brier_score IS NOT NULL AND p2.brier_score IS NOT NULL"""
    ) or {}).get("n",0)
    p13=(db.fetchone("""SELECT COUNT(*) AS n FROM football_predictive_predictions p1
                         JOIN football_predictive3_predictions p3 ON p3.event_id=p1.event_id""") or {}).get("n",0)
    p23=(db.fetchone("""SELECT COUNT(*) AS n FROM football_predictive2_predictions p2
                         JOIN football_predictive3_predictions p3 ON p3.event_id=p2.event_id""") or {}).get("n",0)
    p14=(db.fetchone("""SELECT COUNT(*) AS n FROM football_predictive_predictions p1
                         JOIN football_predictive4_predictions p4 ON p4.event_id=p1.event_id""") or {}).get("n",0)
    p24=(db.fetchone("""SELECT COUNT(*) AS n FROM football_predictive2_predictions p2
                         JOIN football_predictive4_predictions p4 ON p4.event_id=p2.event_id""") or {}).get("n",0)
    return {"paired_predictions":paired,"paired_settled":paired_settled,
            "pred1_pred3_paired":p13,"pred2_pred3_paired":p23,
            "pred1_pred4_paired":p14,"pred2_pred4_paired":p24,
            "pred1":p1,"pred2":p2,"pred3":p3,"pred4":p4}

@app.get('/api/predictive-football/historical-validation')
def predictive_football_historical_validation_api(limit:int=Query(100,ge=1,le=1000)):
    
    return {
        "enabled":settings.predictive_football_historical_enabled,
        "settings":{
            "region":settings.predictive_football_historical_region,
            "snapshot_minutes":list(settings.predictive_football_historical_snapshot_minutes),
            "daily_credit_budget":settings.predictive_football_historical_daily_credit_budget,
            "interval_seconds":settings.predictive_football_historical_interval_seconds,
        },
        "summary":historical_validation_summary(db),
        "latest":db.fetchall(
            "SELECT * FROM football_predictive_historical_validations ORDER BY id DESC LIMIT ?",(limit,)
        ),
    }

@app.get('/api/predictive-football/historical-bets')
def predictive_football_historical_bets_api(limit:int=Query(200,ge=1,le=2000)):
    return db.fetchall(
        "SELECT * FROM football_predictive_historical_bets ORDER BY id DESC LIMIT ?",(limit,)
    )

@app.post('/admin/predictive-football/historical-run')
def admin_predictive_football_historical_run(x_admin_secret:Optional[str]=Header(None)):
    _admin(x_admin_secret);return predictive_historical_engine.one_cycle(force=True)

@app.get('/api/research/instrumentation')
def research_instrumentation_api():
    
    return instrumentation_report(db)

@app.get('/api/research/intelligence')
def research_intelligence_api():
    return research_intelligence(db)

@app.get('/api/research/weekly')
def research_weekly_api(limit:int=Query(12,ge=1,le=104)):
    upsert_weekly_report(db);return latest_weekly_reports(db,limit)

@app.get('/api/research/canonical-scoreboard')
def canonical_research_scoreboard():
    return canonical_scoreboard(db)

@app.get('/api/research/scoreboard')
def research_scoreboard(): return strategy_scoreboard(db)

@app.get('/api/research/rejections')
def research_rejections(): return rejection_summary(db)

@app.get('/api/research/evaluations')
def research_evaluations(limit:int=Query(100,ge=1,le=1000)): return latest_evaluations(db,limit)

@app.get('/api/events/{event_id}/market')
def event_market(event_id:str): return event_market_snapshot(db,event_id)

@app.get('/api/events/{event_id}/history')
def event_history(event_id:str,limit:int=Query(2000,ge=1,le=10000)): return event_price_history(db,event_id,limit)

@app.get('/api/signals/{signal_id}/price-history')
def signal_history(signal_id:int): return signal_price_history(db,signal_id)

@app.get('/api/collector-runs')
def collector_runs(limit:int=Query(100,ge=1,le=1000)):
    return db.fetchall("SELECT * FROM collector_runs ORDER BY id DESC LIMIT ?",(limit,))

@app.post('/admin/discover')
def admin_discover(x_admin_secret:Optional[str]=Header(None)):
    _admin(x_admin_secret);return {"events_seen":collector.discover()}

@app.post('/admin/poll')
def admin_poll(x_admin_secret:Optional[str]=Header(None)):
    _admin(x_admin_secret);result=collector.poll_one_cycle()
    if result.get('polled') and result.get('mode') == 'breadth':
        for eid in result.get('event_ids',[]):
            write_consensus_and_value_signals(db,eid,min_books=settings.min_consensus_books,min_edge_pct=settings.min_edge_pct)
            write_slow_book_signals(db,eid,min_books=settings.min_consensus_books,min_gap_pct=settings.min_slow_book_gap_pct,min_edge_pct=settings.min_edge_pct)
            write_cross_market_signals(db,eid,min_edge_pct=settings.min_cross_market_edge_pct)
            cluster_event_signals(db,eid)
            if settings.execution_shadow_enabled:
                evaluate_latest_execution_wave(db,eid,settings.execution_bookmaker_keys)
    elif result.get('polled') and result.get('mode') == 'predictive_close_broad':
        result['predictive_close_consensus_rows']=sum(
            write_consensus_snapshots_only(db,eid,min_books=settings.min_consensus_books)
            for eid in result.get('event_ids',[])
        )
    track_signal_prices(db);finalize_closing_lines(db);sync_canonical_bets(db)
    if settings.execution_shadow_enabled:
        track_execution_prices(db);finalize_execution_clv(db);settle_execution_from_stored_results(db)
    result["multiples_created"]=generate_multiple_shadows(db,allowed_bookmaker_keys=settings.multiples_api_bookmaker_keys)
    result["multiples_clv_finalized"]=finalize_multiple_clv(db)
    result["multiples_settled"]=settle_multiple_shadows(db)
    result["instrumentation"]=run_measurement_maintenance(
        db, excluded_books=settings.execution_bookmaker_keys
    )
    return result

@app.post('/admin/multisport-lines/run')
def admin_multisport_lines_run(x_admin_secret:Optional[str]=Header(None)):
    _admin(x_admin_secret);return multisport_lines_engine.one_cycle()

@app.post('/admin/multisport/run')
def admin_multisport_run(x_admin_secret:Optional[str]=Header(None)):
    _admin(x_admin_secret);
    return multisport_engine.one_cycle()

@app.post('/admin/predictive-football/run')
def admin_predictive_football_run(x_admin_secret:Optional[str]=Header(None)):
    _admin(x_admin_secret);
    return predictive_football_engine.one_cycle()

@app.post('/admin/predictive-football-pred2/run')
def admin_predictive_football_pred2_run(x_admin_secret:Optional[str]=Header(None)):
    _admin(x_admin_secret);
    return predictive_football_pred2_engine.one_cycle()

@app.post('/admin/tennis/run')
def admin_tennis_run(x_admin_secret:Optional[str]=Header(None)):
    _admin(x_admin_secret);
    return tennis_engine.one_cycle()

@app.post('/admin/multiples/run')
def admin_multiples_run(x_admin_secret:Optional[str]=Header(None)):
    _admin(x_admin_secret);
    return {
        "created":generate_multiple_shadows(db,allowed_bookmaker_keys=settings.multiples_api_bookmaker_keys),
        "clv_finalized":finalize_multiple_clv(db),
        "settled":settle_multiple_shadows(db),
        "scoreboard":multiples_scoreboard(db),
    }

@app.post('/admin/manual-systems/run')
def admin_manual_systems_run(x_admin_secret:Optional[str]=Header(None)):
    _admin(x_admin_secret);
    out=manual_systems_engine.one_cycle()
    out['scoreboard']=manual_systems_scoreboard(db)
    return out

@app.post('/admin/close-lines')
def admin_close_lines(x_admin_secret:Optional[str]=Header(None)):
    _admin(x_admin_secret);
    out={"finalized":finalize_closing_lines(db),"price_observations":track_signal_prices(db)}
    out["canonical_synced"]=sync_canonical_bets(db)
    if settings.execution_shadow_enabled:
        out["execution_observations"]=track_execution_prices(db)
        out["execution_finalized"]=finalize_execution_clv(db)
        out["execution_settled"]=settle_execution_from_stored_results(db)
    out["multiples_created"]=generate_multiple_shadows(db,allowed_bookmaker_keys=settings.multiples_api_bookmaker_keys)
    out["multiples_finalized"]=finalize_multiple_clv(db)
    out["multiples_settled"]=settle_multiple_shadows(db)
    out["manual_systems_clv_finalized"]=finalize_manual_system_clv(db)
    out["manual_systems_settled"]=settle_manual_system_shadows(db)
    return out


@app.get('/api/results')
def results(limit:int=Query(100,ge=1,le=1000)):
    
    return db.fetchall("""
        SELECT r.*,e.league,e.home_team,e.away_team,e.commence_time
        FROM event_results r JOIN events e ON e.event_id=r.event_id
        ORDER BY r.fetched_at DESC LIMIT ?
    """,(limit,))

@app.post('/admin/results')
def admin_results(x_admin_secret:Optional[str]=Header(None)):
    _admin(x_admin_secret);
    finalized=finalize_closing_lines(db)
    out=result_collector.collect()
    out["closing_finalized"]=finalized
    out["canonical_synced"]=sync_canonical_bets(db)
    if settings.execution_shadow_enabled:
        out["execution_finalized"]=finalize_execution_clv(db)
        out["execution_settled"]=settle_execution_from_stored_results(db)
    out["multiples_clv_finalized"]=finalize_multiple_clv(db)
    out["multiples_settled"]=settle_multiple_shadows(db)
    out["multiples_created"]=generate_multiple_shadows(db,allowed_bookmaker_keys=settings.multiples_api_bookmaker_keys)
    out["manual_systems_clv_finalized"]=finalize_manual_system_clv(db)
    out["manual_systems_settled"]=settle_manual_system_shadows(db)
    return out

@app.post('/admin/settle')
def admin_settle(signal_id:int,result:str,x_admin_secret:Optional[str]=Header(None)):
    _admin(x_admin_secret);
    try:return settle_signal(db,signal_id,result)
    except ValueError as exc:raise HTTPException(status_code=400,detail=str(exc))


@app.get('/export/research.zip')
def export_research_zip():
    
    payload, filename = build_research_export(db, settings, VERSION)
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )

BASE_STYLE="""
body{background:#0d1117;color:#e6edf3;font-family:Arial,sans-serif;margin:0;padding:24px}a{color:#79c0ff;text-decoration:none}h1,h2,h3{margin:0 0 12px}.sub{color:#8b949e;margin-bottom:22px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:12px;margin:18px 0 26px}.card,.panel{background:#161b22;border:1px solid #30363d;border-radius:10px}.card{padding:16px}.panel{padding:18px;margin:0 0 20px;overflow:auto}.label{color:#8b949e;font-size:12px;text-transform:uppercase;letter-spacing:.06em}.value{font-size:25px;font-weight:700;margin-top:5px}table{border-collapse:collapse;width:100%;min-width:760px}th,td{text-align:left;padding:9px;border-bottom:1px solid #30363d;font-size:13px}th{color:#8b949e}.ok{color:#3fb950}.warn{color:#d29922}.bad{color:#f85149}.muted{color:#8b949e}.pill{padding:3px 8px;border:1px solid #30363d;border-radius:999px;font-size:11px}.section-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px}.button{display:inline-block;background:#238636;color:#fff;padding:11px 15px;border-radius:8px;font-weight:700;margin-right:10px}.button.secondary{background:#21262d;border:1px solid #30363d}
"""

@app.get('/',response_class=HTMLResponse)
def dashboard():
    s=status();q=s['quota'];execution=s['execution_scoreboard'];funnel=s['execution_funnel'];score=s['scoreboard'];multiples=s['multiples_shadow'];manual_systems=s['manual_systems_shadow'];tennis=s['tennis_shadow'];multisport=s['multisport_shadow'];multisport_lines=s['multisport_lines_shadow'];predictive=s['predictive_football'];predictive2=s['predictive_football_pred2'];predictive3=s['predictive_football_pred3'];predictive4=s['predictive_football_pred4'];pxg=s['proxy_xg'];outcome_edge=s['outcome_edge'];cohort_systems=s['cohort_systems_shadow'];predictive_historical=s['predictive_football_historical'];meta_edge=s['meta_edge'];meta_edge_model=s['meta_edge_model']
    exec_bets=latest_execution_bets(db,40)
    recent_multiples=latest_multiple_shadows(db,8)
    recent_manual_systems=latest_manual_system_cards(db,8,manual_only=True)
    theoretical=latest_canonical_bets(db,20)
    sigs=db.fetchall("""SELECT s.*,e.home_team,e.away_team,e.league,e.commence_time FROM signals s JOIN events e ON e.event_id=s.event_id ORDER BY s.id DESC LIMIT 30""")
    cards=[
        ('Bets until midnight',s['bets_until_midnight']),
        ('Executable bets',execution['bets']),('Executable settled',execution['settled']),
        ('Gross P&L u',_fmt(execution['pnl_units'])),('Net P&L u',_fmt(execution['net_pnl_units'])),
        ('Gross ROI',f"{_fmt(execution['roi_pct'])}%"),('Net ROI',f"{_fmt(execution['net_roi_pct'])}%"),
        ('Theoretical canonical',funnel['theoretical_canonical']),('Execution rate',f"{_fmt(funnel['execution_accept_rate_pct'])}%"),
        ('A/B CLV samples',execution['clv_samples']),('Headline CLV %',_fmt(execution['avg_clv_pct'])),
        ('All CLV samples',execution['all_clv_samples']),('All-close CLV %',_fmt(execution['all_avg_clv_pct'])),
        ('Credits remaining',q.get('credits_remaining','—')),('Paid credits today',f"{s['today_paid_cost']}/{s['daily_paid_credit_budget']}"),
    ]
    card_html=''.join(f"<div class='card'><div class='label'>{escape(str(k))}</div><div class='value'>{escape(str(v))}</div></div>" for k,v in cards)
    erows=''.join(
        f"<tr><td>{x['id']}</td><td>{escape(x['home_team'])} v {escape(x['away_team'])}</td><td>{_market_label(x['market_key'])}</td><td>{escape(x['selection'])}</td>"
        f"<td>{escape(x['bookmaker_title'])}</td><td>{_fmt(x['offered_odds'])}</td><td>{_fmt(x['fair_odds'])}</td><td>{_fmt(x['edge_pct'])}%</td><td>{_fmt(x['min_odds'])}</td>"
        f"<td>{_fmt(x.get('reference_best_odds'))}</td><td>{_fmt(x.get('gap_to_reference_pct'))}%</td><td>{x.get('execution_venue_count',1)}</td><td>{x['strategy_count']}</td><td>{x['bookmaker_count']}</td><td>{x['detection_count']}</td>"
        f"<td>{_fmt(x.get('latest_move_pct'))}%</td><td>{_fmt(x.get('clv_pct'))}%</td>"
        f"<td>{escape(str(x.get('clv_quality') or 'PENDING'))}</td><td>{_fmt(x.get('closing_minutes_before_kickoff'))}</td>"
        f"<td>{escape(str(x.get('result') or 'PENDING'))}</td><td>{_fmt(x.get('pnl_units'))}</td>"
        f"<td>{_fmt(x.get('commission_units'))}</td><td>{_fmt(x.get('net_pnl_units'))}</td></tr>"
        for x in exec_bets
    ) or "<tr><td colspan='23'>No automation-capable executable shadow bets yet.</td></tr>"
    trows=''.join(
        f"<tr><td>{x['id']}</td><td>{escape(x['home_team'])} v {escape(x['away_team'])}</td><td>{_market_label(x['market_key'])}</td><td>{escape(x['selection'])}</td><td>{escape(x['bookmaker_title'])}</td><td>{_fmt(x['offered_odds'])}</td><td>{_fmt(x['edge_pct'])}%</td></tr>"
        for x in theoretical
    ) or "<tr><td colspan='7'>No theoretical canonical bets yet.</td></tr>"
    sig_rows=''.join(f"<tr><td>{x['id']}</td><td>{escape(x['strategy'])}</td><td>{escape(x['home_team'])} v {escape(x['away_team'])}</td><td>{_market_label(x['market_key'])}</td><td>{escape(x['selection'])}</td><td>{escape(x['bookmaker_title'])}</td><td>{_fmt(x['offered_odds'])}</td><td>{_fmt(x['edge_pct'])}%</td></tr>" for x in sigs) or "<tr><td colspan='8'>No raw detections yet.</td></tr>"
    multiple_cards=[
        ('Shadows',multiples['bets']),('Settled',multiples['settled']),
        ('Doubles',multiples['segments']['leg_count']['2']['bets']),
        ('Trebles',multiples['segments']['leg_count']['3']['bets']),
        ('Avg combined odds',_fmt(multiples['avg_combined_odds'])),
        ('P&L u',_fmt(multiples['pnl_units'])),
        ('A/B CLV samples',multiples['clv_samples']),
        ('A/B CLV %',_fmt(multiples['avg_clv_pct'])),
    ]
    multiple_card_html=''.join(f"<div class='card'><div class='label'>{escape(str(k))}</div><div class='value'>{escape(str(v))}</div></div>" for k,v in multiple_cards)
    multiple_rows=[]
    for m in recent_multiples:
        legs=' · '.join(
            f"{escape(str(l['home_team']))} v {escape(str(l['away_team']))}: {escape(str(l['selection']))} @{_fmt(l['entry_odds'])}"
            for l in m.get('legs',[])
        )
        multiple_rows.append(
            f"<tr><td>{m['id']}</td><td>{m['leg_count']}</td><td>{escape(str(m['bookmaker_title']))}</td><td>{legs}</td>"
            f"<td>{_fmt(m['combined_odds'])}</td><td>{_fmt(m['edge_pct'])}%</td><td>{escape(str(m.get('clv_quality') or 'PENDING'))}</td>"
            f"<td>{_fmt(m.get('clv_pct'))}%</td><td>{escape(str(m.get('result') or 'PENDING'))}</td><td>{_fmt(m.get('pnl_units'))}</td></tr>"
        )
    multiple_rows_html=''.join(multiple_rows) or "<tr><td colspan='10'>Multiples Shadow has not formed a same-book double/treble yet.</td></tr>"
    manual_cards=[
        ('Cards',manual_systems['cards']),('Manual-placeable',manual_systems['manual_placeable_cards']),
        ('Settled',manual_systems['settled']),('System P&L u',_fmt(manual_systems['system_pnl_units'])),
        ('Singles control P&L u',_fmt(manual_systems['singles_pnl_units'])),
        ('System − singles u',_fmt(manual_systems['system_minus_singles_units'])),
        ('A/B CLV samples',manual_systems['ab_clv_samples']),('Quote credits today',manual_systems['quote_credits_today']),
    ]
    manual_card_html=''.join(f"<div class='card'><div class='label'>{escape(str(k))}</div><div class='value'>{escape(str(v))}</div></div>" for k,v in manual_cards)
    manual_rows=[]
    for m in recent_manual_systems:
        legs=' · '.join(
            f"{escape(str(l['home_team']))} v {escape(str(l['away_team']))}: {escape(str(l['source_engine']))} {escape(str(l['selection']))} @{_fmt(l['entry_odds'])}"
            for l in m.get('legs',[])
        )
        manual_rows.append(
            f"<tr><td>{m['id']}</td><td>{escape(str(m['system_type']))}</td><td>{escape(str(m['bookmaker_title']))}</td><td>{escape(str(m['source_cohort']))}</td><td>{legs}</td>"
            f"<td>{_fmt(m.get('expected_roi_pct'))}%</td><td>{escape(str(m.get('clv_quality') or 'PENDING'))}</td><td>{_fmt(m.get('clv_pct'))}%</td>"
            f"<td>{_fmt(m.get('system_pnl_units'))}</td><td>{_fmt(m.get('singles_pnl_units'))}</td></tr>"
        )
    manual_rows_html=''.join(manual_rows) or "<tr><td colspan='10'>No manual-placeable Yankee/Heinz card has formed yet.</td></tr>"
    paused=bool(q.get('paid_polling_paused'));reason=q.get('pause_reason') or '—';provider='configured' if settings.odds_api_key else 'NOT CONFIGURED'
    approved=', '.join(settings.execution_bookmaker_keys)
    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Betting Lab v{VERSION}</title><style>{BASE_STYLE}</style></head><body>
    <h1>Project Exit Plan — Betting Lab v{VERSION}</h1><div class='sub'>Automation-capable execution shadow · all bookmakers retained only for consensus/reference research</div>
    <div class='panel'>Provider: <strong>{provider}</strong> · Paid polling: <strong class=\"{'warn' if paused else 'ok'}\">{'PAUSED' if paused else 'ACTIVE'}</strong> · reason: {escape(str(reason))} · reserve: {settings.quota_reserve_credits} · approved execution keys: <strong>{escape(approved)}</strong></div>
    <div class='grid'>{card_html}</div>
    <div class='panel'><h2>Tennis Shadow <span class='pill'>TS1</span></h2>
      <div class='muted'>Separate two-way match-winner pricing experiment. Football evidence is unchanged. <a href='/tennis'>Open Tennis Shadow →</a></div>
      <div class='grid'>
        <div class='card'><div class='label'>Tennis bets</div><div class='value'>{tennis['bets']}</div></div>
        <div class='card'><div class='label'>Settled</div><div class='value'>{tennis['settled']}</div></div>
        <div class='card'><div class='label'>A/B CLV samples</div><div class='value'>{tennis['clv_samples']}</div></div>
        <div class='card'><div class='label'>A/B CLV</div><div class='value'>{_fmt(tennis['avg_clv_pct'])}%</div></div>
        <div class='card'><div class='label'>Net ROI</div><div class='value'>{_fmt(tennis['net_roi_pct'])}%</div></div>
        <div class='card'><div class='label'>Tennis credits today</div><div class='value'>{s['tennis_paid_credits_today']}/{s['tennis_daily_paid_credit_budget']}</div></div>
      </div>
    </div>
    <div class='panel'><h2>Multi-Sport Shadow <span class='pill'>MSP1</span></h2>
      <div class='muted'>Two-way h2h/moneyline pricing research across active MLB, NFL/NCAAF, basketball, AFL/NRL and hockey targets. Separate evidence cohort. <a href='/multisport'>Open Multi-Sport Shadow →</a> · <a href='/multisport-lines'>Spreads/totals →</a></div>
      <div class='grid'>
        <div class='card'><div class='label'>Active leagues</div><div class='value'>{multisport['active_leagues']}</div></div>
        <div class='card'><div class='label'>Executable shadows</div><div class='value'>{multisport['bets']}</div></div>
        <div class='card'><div class='label'>Settled</div><div class='value'>{multisport['settled']}</div></div>
        <div class='card'><div class='label'>A/B CLV</div><div class='value'>{_fmt(multisport['avg_clv_pct'])}%</div></div>
        <div class='card'><div class='label'>Net ROI</div><div class='value'>{_fmt(multisport['net_roi_pct'])}%</div></div>
        <div class='card'><div class='label'>Credits today</div><div class='value'>{s['multisport_paid_credits_today']}/{s['multisport_daily_paid_credit_budget']}</div></div>
      </div>
    </div>
    <div class='panel'><h2>Predictive Football <span class='pill'>PRED1</span></h2>
      <div class='muted'>Independent score-only Bayesian Poisson model. One frozen score forecast now drives 1X2, BTTS and totals; prices are consulted only afterwards. <a href='/predictive-football'>Open Predictive Football →</a></div>
      <div class='grid'>
        <div class='card'><div class='label'>Training matches</div><div class='value'>{predictive['training_matches']}</div></div>
        <div class='card'><div class='label'>Predictions</div><div class='value'>{predictive['predictions']}</div></div>
        <div class='card'><div class='label'>Model shadows</div><div class='value'>{predictive['bets']}</div></div>
        <div class='card'><div class='label'>1X2 / BTTS / Totals</div><div class='value'>{predictive['h2h_bets']} / {predictive['btts_bets']} / {predictive['totals_bets']}</div></div>
        <div class='card'><div class='label'>A/B CLV</div><div class='value'>{_fmt(predictive['avg_clv_pct'])}%</div></div>
        <div class='card'><div class='label'>Brier</div><div class='value'>{_fmt(predictive['avg_brier_score'],4)}</div></div>
        <div class='card'><div class='label'>Net ROI</div><div class='value'>{_fmt(predictive['net_roi_pct'])}%</div></div>
      </div>
    </div>
    <div class='panel'><h2>Predictive Football Challenger <span class='pill'>PRED2</span></h2>
      <div class='muted'>Dixon-Coles low-score dependency challenger. Same score history, forecast horizon, thresholds and approved-venue waves as PRED1. <a href='/predictive-football-pred2'>Open PRED2 →</a></div>
      <div class='grid'>
        <div class='card'><div class='label'>Predictions</div><div class='value'>{predictive2['predictions']}</div></div>
        <div class='card'><div class='label'>Model shadows</div><div class='value'>{predictive2['bets']}</div></div>
        <div class='card'><div class='label'>1X2 / BTTS / Totals</div><div class='value'>{predictive2['h2h_bets']} / {predictive2['btts_bets']} / {predictive2['totals_bets']}</div></div>
        <div class='card'><div class='label'>A/B CLV</div><div class='value'>{_fmt(predictive2['avg_clv_pct'])}%</div></div>
        <div class='card'><div class='label'>Brier</div><div class='value'>{_fmt(predictive2['avg_brier_score'],4)}</div></div>
        <div class='card'><div class='label'>Net ROI</div><div class='value'>{_fmt(predictive2['net_roi_pct'])}%</div></div>
      </div>
    </div>
    <div class='panel'><h2>Predictive Football xG Challenger <span class='pill'>PRED3</span></h2>
      <div class='muted'>Independent StatsBomb Open Data xG/event challenger. Forecasts only when both teams have sufficient non-stale StatsBomb history; bookmaker prices are consulted only after freeze. <a href='/predictive-football-pred3'>Open PRED3 →</a></div>
      <div class='cards'>
        <div class='card'><div class='label'>Forecasts</div><div class='value'>{predictive3['predictions']}</div></div>
        <div class='card'><div class='label'>Settled forecasts</div><div class='value'>{predictive3['settled_predictions']}</div></div>
        <div class='card'><div class='label'>Brier</div><div class='value'>{_fmt(predictive3['avg_brier_score'],4)}</div></div>
        <div class='card'><div class='label'>A/B CLV</div><div class='value'>{_fmt(predictive3['avg_clv_pct'])}%</div></div>
      </div>
    </div>
    <div class='panel'><h2>Current PXG Challenger <span class='pill'>PRED4 · SHADOW ONLY</span></h2>
      <div class='muted'>Uses only recent completed PXG1 current-match histories. Requires repeated team evidence before forecasting and consults bookmaker prices only after the forecast is frozen. <a href='/predictive-football-pred4'>Open PRED4 →</a></div>
      <div class='grid'>
        <div class='card'><div class='label'>Forecasts</div><div class='value'>{predictive4['predictions']}</div></div>
        <div class='card'><div class='label'>Model shadows</div><div class='value'>{predictive4['bets']}</div></div>
        <div class='card'><div class='label'>BTTS shadows</div><div class='value'>{predictive4['btts_bets']}</div></div>
        <div class='card'><div class='label'>Brier</div><div class='value'>{_fmt(predictive4['avg_brier_score'],4)}</div></div>
        <div class='card'><div class='label'>A/B CLV</div><div class='value'>{_fmt(predictive4['avg_clv_pct'])}%</div></div>
        <div class='card'><div class='label'>Net ROI</div><div class='value'>{_fmt(predictive4['net_roi_pct'])}%</div></div>
      </div>
    </div>
    <div class='panel'><h2>Outcome Edge Research <span class='pill'>ACTUAL vs IMPLIED</span></h2>
      <div class='muted'>Reframes the evidence around whether selections actually win more often than their stored entry prices imply. CLV remains a diagnostic, not the objective. <a href='/outcome-edge'>Open Outcome Edge →</a></div>
      <div class='grid'>
        <div class='card'><div class='label'>Unique settled</div><div class='value'>{outcome_edge['overall']['selections']}</div></div>
        <div class='card'><div class='label'>Hit rate</div><div class='value'>{_fmt(outcome_edge['overall']['hit_rate_pct'])}%</div></div>
        <div class='card'><div class='label'>Mean implied</div><div class='value'>{_fmt(outcome_edge['overall']['mean_implied_probability_pct'])}%</div></div>
        <div class='card'><div class='label'>Actual − implied</div><div class='value'>{_fmt(outcome_edge['overall']['hit_minus_implied_pp'])}pp</div></div>
        <div class='card'><div class='label'>4–7.49 sample</div><div class='value'>{outcome_edge['focus_4_to_7_49']['selections']}</div></div>
        <div class='card'><div class='label'>4–7.49 ROI</div><div class='value'>{_fmt(outcome_edge['focus_4_to_7_49']['flat_stake_roi_pct'])}%</div></div>
      </div>
    </div>
    <div class='panel'><h2>Current Underlying Performance <span class='pill'>PXG1 · RESEARCH ONLY</span></h2>
      <div class='muted'>Free proxy-xG research: learn from genuine StatsBomb xG, then apply the mapping to current completed-match statistics when an API-Football key is supplied. PRED1/PRED2/PRED3 remain untouched; PRED4 consumes this dataset as an isolated shadow challenger. <a href='/proxy-xg'>Open PXG1 →</a></div>
      <div class='grid'>
        <div class='card'><div class='label'>Training samples</div><div class='value'>{pxg['training_samples']}</div></div>
        <div class='card'><div class='label'>Current matches</div><div class='value'>{int(pxg['current_matches'].get('matches') or 0)}</div></div>
        <div class='card'><div class='label'>API-Football</div><div class='value'>{'READY' if pxg['api_football_configured'] else 'WAITING'}</div></div>
      </div>
    </div>
    <div class='panel'><h2>Meta-Edge / CLV Trust Research <span class='pill'>META1 + META2 · SHADOW ONLY</span></h2>
      <div class='muted'>META1 freezes entry-time features and labels them later with A/B-quality CLV. Once the clean-label gate is crossed, META2 freezes a time-validated trust model and annotates future PRED1/PRED2 opportunities. It makes zero provider calls and cannot create, reject, resize or place bets. <a href='/meta-edge'>Open Meta-Edge →</a></div>
      <div class='grid'>
        <div class='card'><div class='label'>Feature samples</div><div class='value'>{meta_edge['samples']}</div></div>
        <div class='card'><div class='label'>Clean A/B labels</div><div class='value'>{meta_edge['clean_ab_labels']}</div></div>
        <div class='card'><div class='label'>META2 status</div><div class='value'>{escape(str(meta_edge_model.get('status') or 'WAITING'))}</div></div>
        <div class='card'><div class='label'>Forward META2 scores</div><div class='value'>{meta_edge_model.get('forward_scores',0)}</div></div>
        <div class='card'><div class='label'>META1 avg CLV</div><div class='value'>{_fmt(meta_edge['avg_clv_pct'])}%</div></div>
        <div class='card'><div class='label'>META1 beat close</div><div class='value'>{_fmt(meta_edge['beat_close_pct'])}%</div></div>
      </div>
    </div>
    <div class='panel'><h2>Predictive Historical Validation <span class='pill'>PRED-HIST · FROZEN</span></h2>
      <div class='muted'>Retrospective corroboration only: PRED1/PRED2 are reconstructed using information available at the original 24h freeze, with sampled historical prices at the configured checkpoints. It never writes to the forward PRED tables and is not a tuning lane. <a href='/api/predictive-football/historical-validation'>Open validation JSON →</a></div>
      <div class='grid'>
        <div class='card'><div class='label'>Complete fixtures</div><div class='value'>{predictive_historical['complete_fixtures']}</div></div>
        <div class='card'><div class='label'>Partial fixtures</div><div class='value'>{predictive_historical['partial_fixtures']}</div></div>
        <div class='card'><div class='label'>PRED1 Brier</div><div class='value'>{_fmt(predictive_historical['pred1_avg_brier'],4)}</div></div>
        <div class='card'><div class='label'>PRED2 Brier</div><div class='value'>{_fmt(predictive_historical['pred2_avg_brier'],4)}</div></div>
        <div class='card'><div class='label'>Closing-market Brier</div><div class='value'>{_fmt(predictive_historical['closing_market_avg_brier'],4)}</div></div>
        <div class='card'><div class='label'>P1 / P2 fixture wins</div><div class='value'>{predictive_historical['pred1_fixture_wins']} / {predictive_historical['pred2_fixture_wins']}</div></div>
        <div class='card'><div class='label'>PRED1 sampled CLV</div><div class='value'>{_fmt(predictive_historical['sampled_execution']['PRED1']['avg_clv_pct'])}%</div></div>
        <div class='card'><div class='label'>PRED2 sampled CLV</div><div class='value'>{_fmt(predictive_historical['sampled_execution']['PRED2']['avg_clv_pct'])}%</div></div>
        <div class='card'><div class='label'>Historical credits today</div><div class='value'>{s['predictive_football_historical_paid_credits_today']}/{s['predictive_football_historical_daily_credit_budget']}</div></div>
      </div>
    </div>
    <div class='panel'><h2>Multi-Sport Lines <span class='pill'>MSP2</span></h2>
      <div class='muted'>Featured spreads/handicaps + totals at exact reference lines. Separate from MSP1 moneylines. <a href='/multisport-lines'>Open Lines Shadow →</a></div>
      <div class='grid'>
        <div class='card'><div class='label'>Line shadows</div><div class='value'>{multisport_lines['bets']}</div></div>
        <div class='card'><div class='label'>Settled</div><div class='value'>{multisport_lines['settled']}</div></div>
        <div class='card'><div class='label'>A/B line closes</div><div class='value'>{multisport_lines['line_close_samples']}</div></div>
        <div class='card'><div class='label'>Avg line CLV pts</div><div class='value'>{_fmt(multisport_lines['avg_line_clv_points'])}</div></div>
        <div class='card'><div class='label'>Price CLV samples</div><div class='value'>{multisport_lines['price_clv_samples']}</div></div>
        <div class='card'><div class='label'>Net ROI</div><div class='value'>{_fmt(multisport_lines['net_roi_pct'])}%</div></div>
      </div>
    </div>
    <div class='panel'><h2>Execution Shadow</h2><div class='muted'>Only Betfair Exchange / Matchbook / Smarkets prices can enter this ledger. A theoretical signal is rejected if no approved venue has at least the strategy's minimum acceptable price. This is the universe that future automated live execution would use.</div><p><a class='button' href='/export/research.zip'>Download Research Export (.zip)</a><a class='button secondary' href='/research'>Open Research Intelligence →</a></p><div class='muted'>The export contains the full research tables and summaries, but no API keys, database credentials or admin secret.</div></div>
    <div class='panel'><h2>Multiples Shadow <span class='pill'>API-GATED</span></h2><div class='muted'>New doubles/trebles form only at an explicitly verified accumulator-capable API venue. Ordinary fixed-odds books are no longer eligible. Current API venue allowlist: {', '.join(settings.multiples_api_bookmaker_keys) if settings.multiples_api_bookmaker_keys else 'NONE — formation paused'}. Legacy non-API MS1 records are retained for research/settlement but are excluded from this headline lane.</div><div class='grid'>{multiple_card_html}</div><p><a class='button secondary' href='/multiples'>Open Multiples Shadow →</a></p><table><thead><tr><th>ID</th><th>Legs</th><th>Book</th><th>Selections</th><th>Combined odds</th><th>Model edge</th><th>CLV quality</th><th>CLV</th><th>Result</th><th>P&L u</th></tr></thead><tbody>{multiple_rows_html}</tbody></table></div>
    <div class='panel'><h2>Manual Systems Shadow <span class='pill'>MS2 · YANKEE / HEINZ</span></h2><div class='muted'>William Hill and Ladbrokes are manual-placeable research venues. Betfair Exchange, Matchbook and Smarkets are synthetic price-comparison controls only. Each system risks exactly 1u in total and is compared with the same 1u split equally across its legs. <a href='/manual-systems'>Open Manual Systems Shadow →</a></div><div class='grid'>{manual_card_html}</div><table><thead><tr><th>ID</th><th>System</th><th>Book</th><th>Cohort</th><th>Legs</th><th>Expected ROI</th><th>CLV quality</th><th>CLV</th><th>System P&L</th><th>Singles P&L</th></tr></thead><tbody>{manual_rows_html}</tbody></table></div>
    <div class='panel'><h2>Cohort Systems Shadow <span class='pill'>MS3 · FORWARD ONLY</span></h2><div class='muted'>Frozen BTTS-only, 4.00–7.49-only and hybrid Yankee/Heinz experiments. v0.19.2 arms prospective cards then priority-refreshes the exact constituent event/markets before confirming them; shadow-only with no order placement. <a href='/cohort-systems'>Open MS3 →</a></div><div class='grid'><div class='card'><div class='label'>Cards</div><div class='value'>{cohort_systems['cards']}</div></div><div class='card'><div class='label'>Settled</div><div class='value'>{cohort_systems['settled']}</div></div><div class='card'><div class='label'>System P&L u</div><div class='value'>{_fmt(cohort_systems['system_pnl_units'])}</div></div><div class='card'><div class='label'>Singles P&L u</div><div class='value'>{_fmt(cohort_systems['singles_pnl_units'])}</div></div></div></div>
    <div class='panel'><h2>Executable shadow betting performance</h2><div class='muted'>Headline CLV uses A/B closes only (within 30 minutes of kickoff). Net P&L deducts the configured research commission assumption while preserving gross P&L.</div><table><thead><tr><th>Bets</th><th>Settled</th><th>Wins</th><th>Gross P&L u</th><th>Gross ROI</th><th>Commission u</th><th>Net P&L u</th><th>Net ROI</th><th>Win rate</th><th>Avg edge</th><th>A/B Avg CLV</th><th>A/B samples</th><th>All-close CLV</th><th>All samples</th><th>Beat close</th><th>Gross DD</th><th>Net DD</th></tr></thead><tbody><tr><td>{execution['bets']}</td><td>{execution['settled']}</td><td>{execution['wins']}</td><td>{_fmt(execution['pnl_units'])}</td><td>{_fmt(execution['roi_pct'])}%</td><td>{_fmt(execution['commission_units'])}</td><td>{_fmt(execution['net_pnl_units'])}</td><td>{_fmt(execution['net_roi_pct'])}%</td><td>{_fmt(execution['win_rate_pct'])}%</td><td>{_fmt(execution['avg_edge_pct'])}%</td><td>{_fmt(execution['avg_clv_pct'])}%</td><td>{execution['clv_samples']}</td><td>{_fmt(execution['all_avg_clv_pct'])}%</td><td>{execution['all_clv_samples']}</td><td>{_fmt(execution['beat_close_pct'])}%</td><td>{_fmt(execution['max_drawdown_units'])}</td><td>{_fmt(execution['net_max_drawdown_units'])}</td></tr></tbody></table></div>
    <div class='panel'><h2>Executable shadow bets</h2><table><thead><tr><th>ID</th><th>Fixture</th><th>Market</th><th>Selection</th><th>API venue</th><th>Entry</th><th>Fair</th><th>Edge</th><th>Min</th><th>Best market ref</th><th>Gap</th><th>Exec venues</th><th>Strategies</th><th>Raw books</th><th>Detections</th><th>Current move</th><th>Final CLV</th><th>CLV quality</th><th>Close mins</th><th>Result</th><th>Gross P&L u</th><th>Commission u</th><th>Net P&L u</th></tr></thead><tbody>{erows}</tbody></table></div>
    <div class='panel'><h2>Theoretical canonical opportunities</h2><div class='muted'>These keep the original all-bookmaker research intact. They are not headline bets and may be impossible to automate.</div><table><thead><tr><th>ID</th><th>Fixture</th><th>Market</th><th>Selection</th><th>Theoretical best book</th><th>Price</th><th>Edge</th></tr></thead><tbody>{trows}</tbody></table></div>
    <div class='panel'><h2>Latest raw detections</h2><table><thead><tr><th>ID</th><th>Strategy</th><th>Fixture</th><th>Market</th><th>Selection</th><th>Book</th><th>Offered</th><th>Edge</th></tr></thead><tbody>{sig_rows}</tbody></table></div>
    </body></html>""")

@app.get('/meta-edge',response_class=HTMLResponse)
def meta_edge_page():
    
    score=meta_edge_scoreboard(db,settings.meta_edge_min_clean_labels)
    model=meta_model_status(db)
    seg=meta_edge_segments(db)
    recent=latest_meta_edge_samples(db,80)
    model_scores=latest_meta_model_scores(db,80)
    cards=[
        ('META1 status',score['status']),('Feature samples',score['samples']),
        ('Clean A/B labels',score['clean_ab_labels']),('Readiness %',_fmt(score['progress_pct'])),
        ('Avg CLV %',_fmt(score['avg_clv_pct'])),('Beat close %',_fmt(score['beat_close_pct'])),
        ('Avg claimed edge %',_fmt(score['avg_claimed_edge_pct'])),('Edge − CLV pp',_fmt(score['edge_minus_clv_pp'])),
    ]
    card_html=''.join(f"<div class='card'><div class='label'>{escape(str(k))}</div><div class='value'>{escape(str(v))}</div></div>" for k,v in cards)
    model_cards=[('META2 status',model.get('status','—'))]
    if model.get('active'):
        metrics=model.get('metrics') or {}
        model_cards += [
            ('Frozen labels',model.get('clean_labels_at_freeze')),
            ('Train / holdout',f"{model.get('train_labels')}/{model.get('holdout_labels')}"),
            ('Holdout Brier',_fmt(metrics.get('holdout_brier'),4)),
            ('Baseline Brier',_fmt(metrics.get('baseline_brier'),4)),
            ('Holdout avg CLV',f"{_fmt(metrics.get('holdout_avg_clv_pct'))}%"),
            ('Top-quartile CLV',f"{_fmt(metrics.get('top_quartile_avg_clv_pct'))}%"),
            ('Forward scored',model.get('forward_scores')),
            ('Forward clean labels',model.get('forward_clean_labels')),
            ('Forward avg CLV',f"{_fmt(model.get('forward_avg_clv_pct'))}%"),
            ('High-trust avg CLV',f"{_fmt(model.get('high_trust_avg_clv_pct'))}%"),
        ]
    model_card_html=''.join(f"<div class='card'><div class='label'>{escape(str(k))}</div><div class='value'>{escape(str(v))}</div></div>" for k,v in model_cards)
    rows=[]
    for r in recent:
        rows.append(
            f"<tr><td>{r['id']}</td><td>{escape(str(r['source_model']))}</td><td>{escape(str(r['market_key']))}</td>"
            f"<td>{escape(str(r['league']))}</td><td>{escape(str(r['selection']))}</td><td>{_fmt(r['edge_pct'])}%</td>"
            f"<td>{escape(str(r['agreement_band']))}</td><td>{escape(str(r['uncertainty_band']))}</td>"
            f"<td>{escape(str(r['archetype']))}</td><td>{escape(str(r['label_status']))}</td><td>{_fmt(r.get('clv_pct'))}%</td></tr>"
        )
    body=''.join(rows) or "<tr><td colspan='11'>No META1 feature rows yet.</td></tr>"
    seg_rows=[]
    for r in seg['agreement_band']:
        seg_rows.append(f"<tr><td>{escape(str(r['segment']))}</td><td>{r['samples']}</td><td>{_fmt(r.get('avg_claimed_edge_pct'))}%</td><td>{_fmt(r.get('avg_clv_pct'))}%</td><td>{_fmt(r.get('beat_close_pct'))}%</td></tr>")
    seg_html=''.join(seg_rows) or "<tr><td colspan='5'>Waiting for clean A/B labels.</td></tr>"
    score_rows=[]
    for r in model_scores:
        score_rows.append(
            f"<tr><td>{r['sample_id']}</td><td>{escape(str(r['phase']))}</td><td>{escape(str(r['source_model']))}</td>"
            f"<td>{escape(str(r['market_key']))}</td><td>{escape(str(r['selection']))}</td><td>{_fmt(r['offered_odds'])}</td>"
            f"<td>{_fmt(float(r['prob_beat_close'])*100.0)}%</td><td>{_fmt(r['expected_clv_pct'])}%</td>"
            f"<td>{escape(str(r['trust_band']))}</td><td>{escape(str(r['label_status']))}</td><td>{_fmt(r.get('clv_pct'))}%</td></tr>"
        )
    model_rows=''.join(score_rows) or "<tr><td colspan='11'>META2 will appear after the frozen model is created.</td></tr>"
    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'><style>{BASE_STYLE}</style></head><body>
    <a href='/'>← Betting Lab</a><h1>Meta-Edge / CLV Trust Research <span class='pill'>META1 + META2</span></h1>
    <div class='sub'>META1 freezes entry-time features and attaches later A/B-quality CLV labels. META2 is a frozen shadow model trained only after the clean-label gate: it predicts probability of beating close and expected CLV for future PRED1/PRED2 opportunities. No additional provider calls and no selection, rejection, staking or execution authority.</div>
    <div class='grid'>{card_html}</div>
    <div class='panel'><h2>Frozen META2 shadow model</h2><div class='muted'>The first model uses a time-ordered holdout, then freezes one final model at the activation sample. It does not retrain itself as new data arrives; forward scores remain a clean out-of-sample experiment.</div><div class='grid'>{model_card_html}</div><table><thead><tr><th>Sample</th><th>Phase</th><th>Model</th><th>Market</th><th>Selection</th><th>Odds</th><th>P(beat close)</th><th>Expected CLV</th><th>Trust</th><th>Label</th><th>Actual CLV</th></tr></thead><tbody>{model_rows}</tbody></table></div>
    <div class='panel'><h2>PRED1/PRED2 agreement</h2><table><thead><tr><th>Agreement band</th><th>Samples</th><th>Claimed edge</th><th>Avg CLV</th><th>Beat close</th></tr></thead><tbody>{seg_html}</tbody></table></div>
    <div class='panel'><h2>Latest frozen feature samples</h2><table><thead><tr><th>ID</th><th>Model</th><th>Market</th><th>League</th><th>Selection</th><th>Edge</th><th>Agreement</th><th>Uncertainty</th><th>Archetype</th><th>Label</th><th>CLV</th></tr></thead><tbody>{body}</tbody></table></div>
    </body></html>""")

@app.get('/predictive-football',response_class=HTMLResponse)
def predictive_football_page():
    
    score=predictive_scoreboard(db)
    bootstrap=predictive_bootstrap_status(db)
    startup_run=db.fetchone(
        """SELECT started_at,finished_at,ok,detail
           FROM collector_runs
           WHERE run_type='PREDICTIVE_FOOTBALL_STARTUP'
           ORDER BY id DESC LIMIT 1"""
    )
    h2h_bets=latest_predictive_bets(db,100)
    market_bets=latest_predictive_market_bets(db,150)
    funnel=predictive_funnel(db)
    market_funnel=predictive_market_funnel(db)
    market_summary=predictive_market_summary(db)
    leagues=predictive_league_summary(db)
    preds=db.fetchall(
        """SELECT * FROM football_predictive_predictions
           ORDER BY id DESC LIMIT 100"""
    )
    cards=[
        ("Training matches",score["training_matches"]),
        ("Bootstrap sources",f"{bootstrap['sources_successful']}/{bootstrap['sources_attempted']}"),
        ("Bootstrap errors",bootstrap["sources_failed"]),
        ("Frozen score forecasts",score["predictions"]),
        ("Derived market cases",score["derived_market_cases"]),
        ("1X2 shadows",score["h2h_bets"]),
        ("BTTS shadows",score["btts_bets"]),
        ("Totals shadows",score["totals_bets"]),
        ("All A/B avg CLV",f"{_fmt(score['avg_clv_pct'])}%"),
        ("1X2 Brier",_fmt(score["avg_brier_score"],4)),
        ("Derived Brier",_fmt(score["avg_derived_market_brier"],4)),
        ("Net ROI",f"{_fmt(score['net_roi_pct'])}%"),
    ]
    card_html=''.join(
        f"<div class='card'><div class='label'>{escape(str(k))}</div><div class='value'>{escape(str(v))}</div></div>"
        for k,v in cards
    )
    hbrows=''.join(
        f"<tr><td>1X2</td><td>{escape(x['league'])}</td>"
        f"<td>{escape(x['home_team'])} v {escape(x['away_team'])}</td>"
        f"<td>{escape(x['selection'])}</td><td>{escape(x['bookmaker_title'])}</td>"
        f"<td>{_fmt(x['offered_odds'])}</td><td>{_fmt(x['model_fair_odds'])}</td>"
        f"<td>{_fmt(x['edge_pct'])}%</td><td>{_fmt(x.get('clv_pct'))}%</td>"
        f"<td>{escape(str(x.get('clv_quality') or 'PENDING'))}</td>"
        f"<td>{escape(str(x.get('result') or 'PENDING'))}</td><td>{_fmt(x.get('net_pnl_units'))}</td></tr>"
        for x in h2h_bets
    )
    mbrows=''.join(
        f"<tr><td>{escape(str(x['market_key']).upper())}{(' '+escape(str(x['line_key']))) if x.get('line_key') else ''}</td>"
        f"<td>{escape(x['league'])}</td><td>{escape(x['home_team'])} v {escape(x['away_team'])}</td>"
        f"<td>{escape(x['selection'])}</td><td>{escape(x['bookmaker_title'])}</td>"
        f"<td>{_fmt(x['offered_odds'])}</td><td>{_fmt(x['model_fair_odds'])}</td>"
        f"<td>{_fmt(x['edge_pct'])}%</td><td>{_fmt(x.get('clv_pct'))}%</td>"
        f"<td>{escape(str(x.get('clv_quality') or 'PENDING'))}</td>"
        f"<td>{escape(str(x.get('result') or 'PENDING'))}</td><td>{_fmt(x.get('net_pnl_units'))}</td></tr>"
        for x in market_bets
    )
    brows=(mbrows+hbrows) or "<tr><td colspan='12'>No predictive shadows yet.</td></tr>"
    prows=''.join(
        f"<tr><td>{escape(x['league'])}</td><td>{escape(x['home_team'])} v {escape(x['away_team'])}</td>"
        f"<td>{_fmt(x['expected_home_goals'])}-{_fmt(x['expected_away_goals'])}</td>"
        f"<td>{_fmt(100*x['home_probability'])}%</td><td>{_fmt(100*x['draw_probability'])}%</td>"
        f"<td>{_fmt(100*x['away_probability'])}%</td><td>{x['league_training_matches']}</td>"
        f"<td>{escape(str(x.get('actual_outcome') or 'PENDING'))}</td></tr>"
        for x in preds
    ) or "<tr><td colspan='8'>No frozen predictions yet.</td></tr>"
    frows=''.join(
        f"<tr><td>1X2</td><td>—</td><td>{escape(x['decision'])}</td><td>{escape(x['reason'])}</td><td>{x['count']}</td></tr>"
        for x in funnel
    )
    frows += ''.join(
        f"<tr><td>{escape(str(x['market_key']).upper())}</td><td>{escape(str(x['line_key'] or '—'))}</td>"
        f"<td>{escape(x['decision'])}</td><td>{escape(x['reason'])}</td><td>{x['count']}</td></tr>"
        for x in market_funnel
    )
    frows=frows or "<tr><td colspan='5'>No execution evaluations yet.</td></tr>"
    mrows=''.join(
        f"<tr><td>{escape(str(x['market_key']).upper())}</td><td>{escape(str(x['line_key'] or '—'))}</td>"
        f"<td>{x['cases']}</td><td>{x['bets']}</td><td>{x['settled_bets']}</td>"
        f"<td>{_fmt(x.get('avg_brier'),4)}</td><td>{x['ab_clv_samples']}</td>"
        f"<td>{_fmt(x.get('avg_ab_clv_pct'))}%</td><td>{_fmt(x.get('net_roi_pct'))}%</td></tr>"
        for x in market_summary
    ) or "<tr><td colspan='9'>No BTTS/totals sample yet.</td></tr>"
    lrows=''.join(
        f"<tr><td>{escape(x['league'])}</td><td>{x['predictions']}</td><td>{x['settled']}</td>"
        f"<td>{_fmt(x.get('avg_brier'),4)}</td><td>{_fmt(x.get('avg_brier_advantage'),4)}</td></tr>"
        for x in leagues
    ) or "<tr><td colspan='5'>No league sample yet.</td></tr>"
    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Predictive Football</title><style>{BASE_STYLE}</style></head><body>
    <a href='/'>← Betting Lab</a><h1>Predictive Football <span class='pill'>PRED1 · RESEARCH ONLY</span></h1>
    <div class='sub'>One Bayesian, time-decayed Poisson score forecast is frozen inside {settings.predictive_football_forecast_hours_before:g}h of kickoff before any price is consulted. It independently produces 1X2, BTTS and O/U {', '.join(str(x) for x in settings.predictive_football_total_points)} probabilities; only then are approved execution prices checked.</div>
    <div class='sub'>Historical bootstrap: {bootstrap['sources_successful']}/{bootstrap['sources_attempted']} sources successful · {bootstrap['rows_imported']} rows imported · last attempt {escape(str(bootstrap['latest_attempt_at'] or 'not yet'))}.{" Latest source error: "+escape(str(bootstrap['failures'][-1]['last_error'])) if bootstrap['failures'] else ""}{" Startup error: "+escape(str(startup_run['detail'])) if startup_run and not startup_run['ok'] else ""}</div>
    <div class='grid'>{card_html}</div>
    <div class='panel'><h2>Latest predictive shadows</h2><table><thead><tr><th>Market</th><th>League</th><th>Event</th><th>Selection</th><th>Venue</th><th>Entry</th><th>Model fair</th><th>Model edge</th><th>CLV</th><th>Quality</th><th>Result</th><th>Net P&L</th></tr></thead><tbody>{brows}</tbody></table></div>
    <div class='panel'><h2>BTTS / totals research</h2><table><thead><tr><th>Market</th><th>Line</th><th>Forecast cases</th><th>Shadows</th><th>Settled</th><th>Brier</th><th>A/B CLV n</th><th>A/B CLV</th><th>Net ROI</th></tr></thead><tbody>{mrows}</tbody></table></div>
    <div class='panel'><h2>Latest frozen score forecasts</h2><table><thead><tr><th>League</th><th>Event</th><th>λ home-away</th><th>Home p</th><th>Draw p</th><th>Away p</th><th>League matches</th><th>Outcome</th></tr></thead><tbody>{prows}</tbody></table></div>
    <div class='section-grid'><div class='panel'><h2>Execution funnel</h2><table><thead><tr><th>Market</th><th>Line</th><th>Decision</th><th>Reason</th><th>Count</th></tr></thead><tbody>{frows}</tbody></table></div>
    <div class='panel'><h2>1X2 by league</h2><table><thead><tr><th>League</th><th>Predictions</th><th>Settled</th><th>Brier</th><th>Model vs close Brier Δ</th></tr></thead><tbody>{lrows}</tbody></table></div></div>
    </body></html>""")



@app.get('/proxy-xg',response_class=HTMLResponse)
def proxy_xg_page():
     st=proxy_xg_status(db,settings); model=st.get('latest_model') or {}; cur=st.get('current_matches') or {}; man=st.get('statsbomb_manifest') or {}; api_man=st.get('api_manifest') or {}; usage=st.get('today_api_usage') or {}
    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>PXG1</title><style>{BASE_STYLE}</style></head><body>
    <a href='/'>← Betting Lab</a> · <a href='/predictive-football-pred3'>PRED3</a> · <a href='/outcome-edge'>Outcome Edge</a>
    <h1>Proxy xG <span class='pill'>PXG1 · RESEARCH ONLY</span></h1>
    <div class='sub'>PXG1 learns a fixed mapping from ordinary match statistics to genuine StatsBomb xG, then can apply that mapping to current completed matches from API-Football. It does not alter or select PRED1/PRED2/PRED3 bets.</div>
    <div class='cards'>
      <div class='card'><div class='label'>Training samples</div><div class='value'>{st['training_samples']}</div><div class='small'>{st['training_matches']} StatsBomb matches</div></div>
      <div class='card'><div class='label'>Proxy model</div><div class='value'>{escape(str(model.get('status') or 'WAITING'))}</div><div class='small'>holdout MAE {_fmt(model.get('holdout_mae'),3)} · baseline {_fmt(model.get('baseline_mae'),3)}</div></div>
      <div class='card'><div class='label'>Current matches</div><div class='value'>{int(cur.get('matches') or 0)}</div><div class='small'>scored {int(cur.get('scored') or 0)} · latest {escape(str(cur.get('latest') or '—'))}</div></div>
      <div class='card'><div class='label'>API-Football</div><div class='value'>{'READY' if st['api_football_configured'] else 'WAITING FOR KEY'}</div><div class='small'>today {int(usage.get('calls') or 0)}/{st['api_daily_budget']} calls</div></div>
      <div class='card'><div class='label'>StatsBomb proxy backfill</div><div class='value'>{int(man.get('imported') or 0)}</div><div class='small'>of {int(man.get('total') or 0)} · pending {int(man.get('pending') or 0)}</div></div>
      <div class='card'><div class='label'>Current-data queue</div><div class='value'>{int(api_man.get('pending') or 0)}</div><div class='small'>imported {int(api_man.get('imported') or 0)} · errors {int(api_man.get('errors') or 0)}</div></div>
    </div>
    <div class='panel'><h2>Research gate</h2><div class='sub'>No proxy-xG forecast lane is promoted from this data until the held-out StatsBomb reconstruction beats its simple baseline and enough current team-match samples exist. The existing Betting Lab continues collecting exactly as before.</div></div>
    </body></html>""")

@app.get('/outcome-edge',response_class=HTMLResponse)
def outcome_edge_page():
     report=outcome_edge_report(db); overall=report['overall']; focus=report['focus_4_to_7_49']
    rows=''.join(
        f"<tr><td>{escape(str(x['odds_band']))}</td><td>{x['selections']}</td><td>{x['wins']}</td><td>{_fmt(x['hit_rate_pct'])}%</td><td>{_fmt(x['mean_implied_probability_pct'])}%</td><td>{_fmt(x['hit_minus_implied_pp'])}pp</td><td>{_fmt(x['flat_stake_roi_pct'])}%</td><td>{_fmt(x['avg_ab_clv_pct'])}%</td></tr>"
        for x in report['odds_bands']
    ) or "<tr><td colspan='8'>No settled sample yet.</td></tr>"
    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Outcome Edge</title><style>{BASE_STYLE}</style></head><body>
    <a href='/'>← Betting Lab</a> · <a href='/proxy-xg'>PXG1</a>
    <h1>Outcome Edge <span class='pill'>ACTUAL vs IMPLIED · RESEARCH ONLY</span></h1>
    <div class='sub'>This reframes the research around the economic question: are our stored selections winning more often than their entry prices imply? CLV remains an independent diagnostic rather than the objective.</div>
    <div class='cards'>
      <div class='card'><div class='label'>Unique settled selections</div><div class='value'>{overall['selections']}</div></div>
      <div class='card'><div class='label'>Actual hit rate</div><div class='value'>{_fmt(overall['hit_rate_pct'])}%</div></div>
      <div class='card'><div class='label'>Mean implied</div><div class='value'>{_fmt(overall['mean_implied_probability_pct'])}%</div></div>
      <div class='card'><div class='label'>Actual − implied</div><div class='value'>{_fmt(overall['hit_minus_implied_pp'])}pp</div></div>
      <div class='card'><div class='label'>4.0–7.49 sample</div><div class='value'>{focus['selections']}</div><div class='small'>{focus['wins']} wins · hit {_fmt(focus['hit_rate_pct'])}%</div></div>
      <div class='card'><div class='label'>4.0–7.49 flat ROI</div><div class='value'>{_fmt(focus['flat_stake_roi_pct'])}%</div><div class='small'>A/B CLV {_fmt(focus['avg_ab_clv_pct'])}%</div></div>
    </div>
    <div class='panel'><h2>Odds-band evidence</h2><table><thead><tr><th>Odds</th><th>N</th><th>Wins</th><th>Hit rate</th><th>Mean implied</th><th>Excess hit</th><th>Flat ROI</th><th>A/B CLV</th></tr></thead><tbody>{rows}</tbody></table></div>
    <div class='panel'><h2>Frozen forward watch cohorts</h2><div class='muted'>These hypotheses were frozen at v0.19 activation. Future rows are kept separate from the discovery sample so we do not validate a finding on the same data that discovered it.</div><table><thead><tr><th>Cohort</th><th>Frozen</th><th>Discovery N / ROI</th><th>Forward N</th><th>Forward hit</th><th>Forward implied</th><th>Forward ROI</th><th>Forward A/B CLV</th></tr></thead><tbody>{''.join(f"<tr><td>{escape(str(c['label']))}</td><td>{escape(str(c['frozen_at']))[:16]}</td><td>{c['discovery_sample']['selections']} / {_fmt(c['discovery_sample']['flat_stake_roi_pct'])}%</td><td>{c['forward_sample']['selections']}</td><td>{_fmt(c['forward_sample']['hit_rate_pct'])}%</td><td>{_fmt(c['forward_sample']['mean_implied_probability_pct'])}%</td><td>{_fmt(c['forward_sample']['flat_stake_roi_pct'])}%</td><td>{_fmt(c['forward_sample']['avg_ab_clv_pct'])}%</td></tr>" for c in report.get('frozen_watch_cohorts',[])) or "<tr><td colspan='8'>No frozen cohorts yet.</td></tr>"}</tbody></table></div>
    <div class='muted'>{escape(report['definition'])}</div>
    </body></html>""")

@app.get('/predictive-football-pred4',response_class=HTMLResponse)
def predictive_football_pred4_page():
     score=predictive4_scoreboard(db); status4=predictive_football_pred4_status_api()
    bets=latest_predictive4_market_bets(db,60)+latest_predictive4_bets(db,60)
    preds=db.fetchall("SELECT * FROM football_predictive4_predictions ORDER BY id DESC LIMIT 60")
    brows=''.join(
        f"<tr><td>{escape(str(x.get('league') or ''))}</td><td>{escape(str(x.get('home_team') or ''))} v {escape(str(x.get('away_team') or ''))}</td><td>{escape(str(x.get('market_key') or 'h2h'))}</td><td>{escape(str(x.get('selection') or ''))}</td><td>{_fmt(x.get('offered_odds'))}</td><td>{_fmt(x.get('edge_pct'))}%</td><td>{_fmt(x.get('clv_pct'))}%</td><td>{escape(str(x.get('result') or 'OPEN'))}</td></tr>"
        for x in bets[:60]
    ) or "<tr><td colspan='8'>No PRED4 shadows yet.</td></tr>"
    prows=''.join(
        f"<tr><td>{escape(str(x.get('league') or ''))}</td><td>{escape(str(x.get('home_team') or ''))} v {escape(str(x.get('away_team') or ''))}</td><td>{_fmt(x.get('expected_home_goals'))}–{_fmt(x.get('expected_away_goals'))}</td><td>{_fmt(x.get('pxg_data_age_days'))}d</td><td>{_fmt(100*float(x.get('home_probability') or 0))}%</td><td>{_fmt(100*float(x.get('draw_probability') or 0))}%</td><td>{_fmt(100*float(x.get('away_probability') or 0))}%</td></tr>"
        for x in preds
    ) or "<tr><td colspan='7'>No PRED4 forecasts yet; teams need repeated current PXG histories.</td></tr>"
    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>PRED4 Current PXG</title><style>{BASE_STYLE}</style></head><body>
    <a href='/'>← Betting Lab</a> · <a href='/proxy-xg'>PXG1</a> · <a href='/outcome-edge'>Outcome Edge</a>
    <h1>Predictive Football <span class='pill'>PRED4 · CURRENT PXG · SHADOW ONLY</span></h1>
    <div class='sub'>Fresh current-performance challenger using PXG1 estimates derived from API-Football match statistics. It requires at least {settings.predictive_football_pred4_min_team_matches} current matches per team and has no live execution authority.</div>
    <div class='cards'>
      <div class='card'><div class='label'>Current PXG matches</div><div class='value'>{score.get('current_pxg_matches',0)}</div></div>
      <div class='card'><div class='label'>Forecasts</div><div class='value'>{score['predictions']}</div></div>
      <div class='card'><div class='label'>BTTS bets</div><div class='value'>{score['btts_bets']}</div></div>
      <div class='card'><div class='label'>A/B CLV</div><div class='value'>{_fmt(score['avg_clv_pct'])}%</div></div>
      <div class='card'><div class='label'>Brier</div><div class='value'>{_fmt(score['avg_brier_score'],4)}</div></div>
      <div class='card'><div class='label'>Net ROI</div><div class='value'>{_fmt(score['net_roi_pct'])}%</div></div>
    </div>
    <div class='panel'><h2>Latest frozen PRED4 forecasts</h2><table><thead><tr><th>League</th><th>Event</th><th>PXG λ</th><th>Data age</th><th>Home</th><th>Draw</th><th>Away</th></tr></thead><tbody>{prows}</tbody></table></div>
    <div class='panel'><h2>Latest PRED4 shadows</h2><table><thead><tr><th>League</th><th>Event</th><th>Market</th><th>Selection</th><th>Entry</th><th>Edge</th><th>CLV</th><th>Result</th></tr></thead><tbody>{brows}</tbody></table></div>
    </body></html>""")

@app.get('/predictive-football-pred3',response_class=HTMLResponse)
def predictive_football_pred3_page():
     score=predictive3_scoreboard(db); status3=predictive_football_pred3_status_api()
    bets=latest_predictive3_bets(db,60); preds=db.fetchall(
        "SELECT * FROM football_predictive3_predictions ORDER BY id DESC LIMIT 60"
    )
    brows="".join(
        f"<tr><td>{escape(str(r.get('league') or ''))}</td><td>{escape(str(r.get('home_team') or ''))} v {escape(str(r.get('away_team') or ''))}</td><td>{escape(str(r.get('selection') or ''))}</td><td>{_fmt(r.get('offered_odds'))}</td><td>{_fmt(r.get('edge_pct'))}%</td><td>{_fmt(r.get('clv_pct'))}%</td><td>{escape(str(r.get('result') or ''))}</td></tr>"
        for r in bets
    ) or "<tr><td colspan='7'>No PRED3 executable shadows yet.</td></tr>"
    prows="".join(
        f"<tr><td>{escape(str(r.get('league') or ''))}</td><td>{escape(str(r.get('home_team') or ''))} v {escape(str(r.get('away_team') or ''))}</td><td>{_fmt(r.get('expected_home_goals'))}-{_fmt(r.get('expected_away_goals'))}</td><td>{_fmt(r.get('xg_data_age_days'),0)}d</td><td>{_fmt(100*float(r.get('home_probability') or 0),1)}%</td><td>{_fmt(100*float(r.get('draw_probability') or 0),1)}%</td><td>{_fmt(100*float(r.get('away_probability') or 0),1)}%</td><td>{escape(str(r.get('actual_outcome') or ''))}</td></tr>"
        for r in preds
    ) or "<tr><td colspan='8'>No PRED3 forecasts yet. StatsBomb coverage is selective, so this can legitimately remain sparse.</td></tr>"
    m=status3['statsbomb_manifest']; latest=status3.get('latest_statsbomb_match_at') or '—'
    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>PRED3 StatsBomb xG</title><style>{BASE_STYLE}</style></head><body>
    <a href='/'>← Betting Lab</a> · <a href='/predictive-football'>PRED1</a> · <a href='/predictive-football-pred2'>PRED2</a>
    <h1>Predictive Football <span class='pill'>PRED3 · STATSBOMB xG · RESEARCH ONLY</span></h1>
    <div class='sub'>Genuinely different information set: StatsBomb Open Data event xG. PRED3 does not read bookmaker odds before its 24h forecast freeze. Coverage is intentionally selective and stale/unsupported teams are skipped.</div>
    <div class='cards'>
      <div class='card'><div class='label'>xG training matches</div><div class='value'>{score['training_matches']}</div></div>
      <div class='card'><div class='label'>Forecasts</div><div class='value'>{score['predictions']}</div></div>
      <div class='card'><div class='label'>Brier</div><div class='value'>{_fmt(score['avg_brier_score'],4)}</div></div>
      <div class='card'><div class='label'>A/B CLV</div><div class='value'>{_fmt(score['avg_clv_pct'])}%</div></div>
      <div class='card'><div class='label'>Manifest imported</div><div class='value'>{int(m.get('imported') or 0)}</div><div class='small'>of {int(m.get('total') or 0)} · pending {int(m.get('pending') or 0)}</div></div>
      <div class='card'><div class='label'>Latest StatsBomb match</div><div class='value' style='font-size:16px'>{escape(str(latest))}</div></div>
    </div>
    <div class='panel'><h2>Latest frozen PRED3 forecasts</h2><table><thead><tr><th>League</th><th>Event</th><th>xG λ</th><th>Data age</th><th>Home</th><th>Draw</th><th>Away</th><th>Outcome</th></tr></thead><tbody>{prows}</tbody></table></div>
    <div class='panel'><h2>Latest PRED3 shadows</h2><table><thead><tr><th>League</th><th>Event</th><th>Selection</th><th>Entry</th><th>Edge</th><th>CLV</th><th>Result</th></tr></thead><tbody>{brows}</tbody></table></div>
    <div class='muted'>Data source: StatsBomb Open Data. Publicly shared analysis using the source should credit StatsBomb.</div>
    </body></html>""")

@app.get('/predictive-football-pred2',response_class=HTMLResponse)
def predictive_football_pred2_page():
    
    score=predictive2_scoreboard(db)
    score1=predictive_scoreboard(db)
    h2h_bets=latest_predictive2_bets(db,100)
    market_bets=latest_predictive2_market_bets(db,150)
    market_summary=predictive2_market_summary(db)
    market_funnel=predictive2_market_funnel(db)
    leagues=predictive2_league_summary(db)
    preds=db.fetchall(
        """SELECT * FROM football_predictive2_predictions
           ORDER BY id DESC LIMIT 100"""
    )
    paired=(db.fetchone(
        """SELECT COUNT(*) AS n FROM football_predictive_predictions p1
           JOIN football_predictive2_predictions p2 ON p2.event_id=p1.event_id"""
    ) or {}).get('n',0)
    paired_settled=(db.fetchone(
        """SELECT COUNT(*) AS n FROM football_predictive_predictions p1
           JOIN football_predictive2_predictions p2 ON p2.event_id=p1.event_id
           WHERE p1.brier_score IS NOT NULL AND p2.brier_score IS NOT NULL"""
    ) or {}).get('n',0)
    cards=[
        ("Shared training matches",score["training_matches"]),
        ("Frozen forecasts",score["predictions"]),
        ("Paired with PRED1",paired),
        ("Paired settled",paired_settled),
        ("1X2 shadows",score["h2h_bets"]),
        ("BTTS shadows",score["btts_bets"]),
        ("Totals shadows",score["totals_bets"]),
        ("A/B avg CLV",f"{_fmt(score['avg_clv_pct'])}%"),
        ("PRED2 Brier",_fmt(score["avg_brier_score"],4)),
        ("PRED1 Brier",_fmt(score1["avg_brier_score"],4)),
        ("Model vs close Brier Δ",_fmt(score["avg_model_brier_advantage"],4)),
        ("Net ROI",f"{_fmt(score['net_roi_pct'])}%"),
    ]
    card_html=''.join(
        f"<div class='card'><div class='label'>{escape(str(k))}</div><div class='value'>{escape(str(v))}</div></div>"
        for k,v in cards
    )
    all_bets=h2h_bets+market_bets
    all_bets=sorted(all_bets,key=lambda x:int(x.get('id') or 0),reverse=True)[:150]
    brows=''.join(
        f"<tr><td>{escape(('1X2' if not x.get('market_key') else str(x['market_key']).upper()) + ((' '+str(x.get('line_key'))) if x.get('line_key') else ''))}</td>"
        f"<td>{escape(str(x.get('league') or ''))}</td><td>{escape(str(x.get('home_team') or ''))} v {escape(str(x.get('away_team') or ''))}</td>"
        f"<td>{escape(str(x.get('selection') or ''))}</td><td>{escape(str(x.get('bookmaker_title') or ''))}</td>"
        f"<td>{_fmt(x.get('offered_odds'))}</td><td>{_fmt(x.get('model_fair_odds'))}</td><td>{_fmt(x.get('edge_pct'))}%</td>"
        f"<td>{_fmt(x.get('clv_pct'))}%</td><td>{escape(str(x.get('clv_quality') or 'PENDING'))}</td>"
        f"<td>{escape(str(x.get('result') or 'PENDING'))}</td><td>{_fmt(x.get('net_pnl_units'))}</td></tr>"
        for x in all_bets
    ) or "<tr><td colspan='12'>No PRED2 executable shadows yet.</td></tr>"
    prows=''.join(
        f"<tr><td>{escape(str(x['league']))}</td><td>{escape(str(x['home_team']))} v {escape(str(x['away_team']))}</td>"
        f"<td>{_fmt(x['expected_home_goals'])} - {_fmt(x['expected_away_goals'])}</td><td>{_fmt(x.get('dixon_coles_rho'),3)}</td>"
        f"<td>{_fmt(float(x['home_probability'])*100,1)}%</td><td>{_fmt(float(x['draw_probability'])*100,1)}%</td>"
        f"<td>{_fmt(float(x['away_probability'])*100,1)}%</td><td>{escape(str(x.get('actual_outcome') or 'PENDING'))}</td></tr>"
        for x in preds
    ) or "<tr><td colspan='8'>No PRED2 forecasts yet.</td></tr>"
    mrows=''.join(
        f"<tr><td>{escape(str(x['market_key']).upper())}</td><td>{escape(str(x['line_key'] or '—'))}</td>"
        f"<td>{x['cases']}</td><td>{x['bets']}</td><td>{x['settled_bets']}</td><td>{_fmt(x.get('avg_brier'),4)}</td>"
        f"<td>{x['ab_clv_samples']}</td><td>{_fmt(x.get('avg_ab_clv_pct'))}%</td><td>{_fmt(x.get('net_roi_pct'))}%</td></tr>"
        for x in market_summary
    ) or "<tr><td colspan='9'>No PRED2 derived-market sample yet.</td></tr>"
    frows=''.join(
        f"<tr><td>{escape(str(x['market_key']).upper())}</td><td>{escape(str(x['line_key'] or '—'))}</td>"
        f"<td>{escape(str(x['decision']))}</td><td>{escape(str(x['reason']))}</td><td>{x['count']}</td></tr>"
        for x in market_funnel
    ) or "<tr><td colspan='5'>No PRED2 execution evaluations yet.</td></tr>"
    lrows=''.join(
        f"<tr><td>{escape(str(x['league']))}</td><td>{x['predictions']}</td><td>{x['settled']}</td>"
        f"<td>{_fmt(x.get('avg_brier'),4)}</td><td>{_fmt(x.get('avg_brier_advantage'),4)}</td></tr>"
        for x in leagues
    ) or "<tr><td colspan='5'>No PRED2 league sample yet.</td></tr>"
    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>PRED2 Dixon-Coles</title><style>{BASE_STYLE}</style></head><body>
    <a href='/'>← Betting Lab</a> · <a href='/predictive-football'>PRED1</a><h1>Predictive Football <span class='pill'>PRED2 · DIXON-COLES · RESEARCH ONLY</span></h1>
    <div class='sub'>Paired challenger to PRED1. It uses the same score-only history, time decay, attack/defence strengths, 24h freeze, 3% entry gate, approved venues and stored price waves. The only model change is a fitted Dixon-Coles low-score dependency parameter ρ, estimated from pre-kickoff league history. No extra odds-data pipeline is added.</div>
    <div class='sub'>Frozen rho grid: {settings.predictive_football_pred2_rho_min:g} to {settings.predictive_football_pred2_rho_max:g} in {settings.predictive_football_pred2_rho_step:g} steps. PRED1 remains untouched as the control.</div>
    <div class='grid'>{card_html}</div>
    <div class='panel'><h2>Latest PRED2 shadows</h2><table><thead><tr><th>Market</th><th>League</th><th>Event</th><th>Selection</th><th>Venue</th><th>Entry</th><th>Model fair</th><th>Edge</th><th>CLV</th><th>Quality</th><th>Result</th><th>Net P&L</th></tr></thead><tbody>{brows}</tbody></table></div>
    <div class='panel'><h2>Latest frozen PRED2 forecasts</h2><table><thead><tr><th>League</th><th>Event</th><th>λ home-away</th><th>ρ</th><th>Home p</th><th>Draw p</th><th>Away p</th><th>Outcome</th></tr></thead><tbody>{prows}</tbody></table></div>
    <div class='panel'><h2>BTTS / totals research</h2><table><thead><tr><th>Market</th><th>Line</th><th>Cases</th><th>Shadows</th><th>Settled</th><th>Brier</th><th>A/B CLV n</th><th>A/B CLV</th><th>Net ROI</th></tr></thead><tbody>{mrows}</tbody></table></div>
    <div class='section-grid'><div class='panel'><h2>Execution funnel</h2><table><thead><tr><th>Market</th><th>Line</th><th>Decision</th><th>Reason</th><th>Count</th></tr></thead><tbody>{frows}</tbody></table></div>
    <div class='panel'><h2>1X2 by league</h2><table><thead><tr><th>League</th><th>Predictions</th><th>Settled</th><th>Brier</th><th>Model vs close Brier Δ</th></tr></thead><tbody>{lrows}</tbody></table></div></div>
    </body></html>""")

@app.get('/multisport-lines',response_class=HTMLResponse)
def multisport_lines_page():
    score=line_scoreboard(db);segments=line_segments(db);funnel_data=line_funnel(db);bets=latest_line_bets(db,120)
    cards=[('Events',score['events']),('Line shadows',score['bets']),('Settled',score['settled']),('A/B line closes',score['line_close_samples']),('Avg line CLV pts',_fmt(score['avg_line_clv_points'])),('Positive line move',f"{_fmt(score['positive_line_move_pct'])}%"),('Same-line price CLV',score['price_clv_samples']),('Avg price CLV',f"{_fmt(score['avg_price_clv_pct'])}%"),('Net P&L u',_fmt(score['net_pnl_units'])),('Net ROI',f"{_fmt(score['net_roi_pct'])}%"),('Shared credits today',f"{multisport_lines_engine.quota.today_paid_cost()}/{settings.multisport_daily_paid_credit_budget}")]
    card_html=''.join(f"<div class='card'><div class='label'>{escape(str(k))}</div><div class='value'>{escape(str(v))}</div></div>" for k,v in cards)
    brows=''.join(f"<tr><td>{x['id']}</td><td>{escape(x['sport_family'])}</td><td>{escape(x['league_title'])}</td><td>{escape(x['market_key'])}</td><td>{escape(x['home_team'])} v {escape(x['away_team'])}</td><td>{escape(x['selection'])}</td><td>{_fmt(x['line_point'])}</td><td>{escape(x['bookmaker_title'])}</td><td>{_fmt(x['offered_odds'])}</td><td>{_fmt(x['edge_pct'])}%</td><td>{_fmt(x.get('latest_line_move_points'))}</td><td>{_fmt(x.get('line_clv_points'))}</td><td>{escape(str(x.get('close_quality') or 'PENDING'))}</td><td>{escape(str(x.get('result') or 'PENDING'))}</td></tr>" for x in bets) or "<tr><td colspan='14'>No line shadows yet.</td></tr>"
    def seg_table(items):
        rows=''.join(f"<tr><td>{escape(x['label'])}</td><td>{x['bets']}</td><td>{x['settled']}</td><td>{x['line_close_samples']}</td><td>{_fmt(x['avg_line_clv_points'])}</td><td>{x['price_clv_samples']}</td><td>{_fmt(x['avg_price_clv_pct'])}%</td><td>{_fmt(x['net_roi_pct'])}%</td></tr>" for x in items) or "<tr><td colspan='8'>No sample yet.</td></tr>"
        return f"<table><thead><tr><th>Segment</th><th>Bets</th><th>Settled</th><th>A/B line</th><th>Avg line CLV</th><th>Price CLV n</th><th>Avg price CLV</th><th>Net ROI</th></tr></thead><tbody>{rows}</tbody></table>"
    frows=''.join(f"<tr><td>{escape(str(x['market_key']))}</td><td>{escape(str(x['decision']))}</td><td>{escape(str(x['reason']))}</td><td>{x['n']}</td></tr>" for x in funnel_data['rows']) or "<tr><td colspan='4'>No evaluations yet.</td></tr>"
    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Multi-Sport Lines</title><style>{BASE_STYLE}</style></head><body><a href='/'>← Betting Lab</a><h1>Multi-Sport Lines <span class='pill'>MSP2 · RESEARCH ONLY</span></h1><div class='sub'>Featured spreads/handicaps and totals. Exact-line bookmaker consensus → approved venue → first acceptable executable shadow. Same {settings.multisport_lines_min_edge_pct}% edge threshold as MSP1; only the market universe is expanded.</div><div class='grid'>{card_html}</div><div class='panel'><h2>Latest line shadows</h2><table><thead><tr><th>ID</th><th>Sport</th><th>League</th><th>Market</th><th>Event</th><th>Selection</th><th>Entry line</th><th>Venue</th><th>Odds</th><th>Edge</th><th>Latest move</th><th>Close move</th><th>Quality</th><th>Result</th></tr></thead><tbody>{brows}</tbody></table></div><div class='section-grid'><div class='panel'><h2>By market</h2>{seg_table(segments['market'])}</div><div class='panel'><h2>By sport</h2>{seg_table(segments['sport'])}</div></div><div class='panel'><h2>Execution funnel</h2><table><thead><tr><th>Market</th><th>Decision</th><th>Reason</th><th>Count</th></tr></thead><tbody>{frows}</tbody></table></div></body></html>""")

@app.get('/multisport',response_class=HTMLResponse)
def multisport_page():
    
    score=multisport_scoreboard(db);segments=multisport_segments(db);moneyline_funnel=multisport_funnel(db)
    bets=latest_multisport_bets(db,100)
    leagues=db.fetchall(
        "SELECT * FROM multisport_league_state ORDER BY active DESC,sport_family,title"
    )
    evals=db.fetchall(
        "SELECT * FROM multisport_execution_evaluations ORDER BY id DESC LIMIT 100"
    )
    cards=[
        ("Events",score["events"]),("Active leagues",score["active_leagues"]),
        ("Executable shadows",score["bets"]),("Settled",score["settled"]),
        ("Settlement excluded",score["settlement_excluded"]),
        ("A/B CLV samples",score["clv_samples"]),
        ("A/B avg CLV",f"{_fmt(score['avg_clv_pct'])}%"),
        ("Median CLV",f"{_fmt(score['median_clv_pct'])}%"),
        ("Beat close",f"{_fmt(score['beat_close_pct'])}%"),
        ("Net P&L u",_fmt(score["net_pnl_units"])),
        ("Net ROI",f"{_fmt(score['net_roi_pct'])}%"),
        ("Pushes",score["pushes"]),
        ("Brier",_fmt(score["calibration"]["brier_score"],4)),
        ("Credits today",f"{multisport_engine.quota.today_paid_cost()}/{settings.multisport_daily_paid_credit_budget}"),
    ]
    card_html=''.join(
        f"<div class='card'><div class='label'>{escape(str(k))}</div><div class='value'>{escape(str(v))}</div></div>"
        for k,v in cards
    )
    brows=''.join(
        f"<tr><td>{x['id']}</td><td>{escape(x['sport_family'])}</td><td>{escape(x['league_title'])}</td>"
        f"<td>{escape(x['home_team'])} v {escape(x['away_team'])}</td><td>{escape(x['selection'])}</td>"
        f"<td>{escape(x['bookmaker_title'])}</td><td>{_fmt(x['offered_odds'])}</td><td>{_fmt(x['fair_odds'])}</td>"
        f"<td>{_fmt(x['edge_pct'])}%</td><td>{_fmt(x['min_odds'])}</td><td>{_fmt(x.get('latest_move_pct'))}%</td>"
        f"<td>{_fmt(x.get('clv_pct'))}%</td><td>{escape(str(x.get('clv_quality') or 'PENDING'))}</td>"
        f"<td>{escape(str(x.get('result') or 'PENDING'))}</td><td>{_fmt(x.get('net_pnl_units'))}</td></tr>"
        for x in bets
    ) or "<tr><td colspan='15'>No Multi-Sport Shadow executions yet.</td></tr>"
    lrows=''.join(
        f"<tr><td>{escape(str(x['active']))}</td><td>{escape(x['sport_family'])}</td><td>{escape(x['title'])}</td>"
        f"<td>{escape(x['sport_key'])}</td><td>{escape(str(x.get('last_broad_poll_at') or '—'))}</td>"
        f"<td>{escape(str(x.get('last_convergence_poll_at') or '—'))}</td></tr>"
        for x in leagues
    ) or "<tr><td colspan='6'>No target sports discovered yet.</td></tr>"
    erows=''.join(
        f"<tr><td>{escape(x['evaluated_at'])}</td><td>{escape(x['sport_key'])}</td><td>{escape(x['selection'])}</td>"
        f"<td>{_fmt(x.get('best_executable_odds'))}</td><td>{_fmt(x.get('min_required_odds'))}</td>"
        f"<td>{_fmt(x.get('fair_odds'))}</td><td>{_fmt(x.get('edge_pct'))}%</td>"
        f"<td>{escape(x['decision'])}</td><td>{escape(x['reason'])}</td></tr>"
        for x in evals
    ) or "<tr><td colspan='9'>No multi-sport execution evaluations yet.</td></tr>"
    funnel_rows=''.join(
        f"<tr><td>{escape(str(x['decision']))}</td><td>{escape(str(x['reason']))}</td><td>{x['n']}</td></tr>"
        for x in moneyline_funnel['rows']
    ) or "<tr><td colspan='3'>No moneyline evaluations yet.</td></tr>"
    def seg_table(items):
        rows=''.join(
            f"<tr><td>{escape(x['label'])}</td><td>{x['bets']}</td><td>{x['settled']}</td><td>{x['clv_samples']}</td>"
            f"<td>{_fmt(x['avg_clv_pct'])}%</td><td>{_fmt(x['median_clv_pct'])}%</td>"
            f"<td>{_fmt(x['beat_close_pct'])}%</td><td>{_fmt(x['net_roi_pct'])}%</td></tr>"
            for x in items
        ) or "<tr><td colspan='8'>No sample yet.</td></tr>"
        return f"<table><thead><tr><th>Segment</th><th>Bets</th><th>Settled</th><th>A/B</th><th>Avg CLV</th><th>Median CLV</th><th>Beat close</th><th>Net ROI</th></tr></thead><tbody>{rows}</tbody></table>"
    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Multi-Sport Shadow</title><style>{BASE_STYLE}</style></head><body>
    <a href='/'>← Betting Lab</a><h1>Multi-Sport Shadow <span class='pill'>MSP1 · RESEARCH ONLY</span></h1>
    <div class='sub'>In-season two-way h2h/moneyline consensus → approved execution price → closing-price validation. Exactly-two-outcome market waves only. Hockey broad-reference region: {settings.multisport_hockey_reference_region}. Daily lane cap: {settings.multisport_daily_paid_credit_budget} credits.</div>
    <div class='grid'>{card_html}</div>
    <div class='panel'><h2>Latest executable shadows</h2><table><thead><tr><th>ID</th><th>Sport</th><th>League</th><th>Event</th><th>Selection</th><th>Venue</th><th>Entry</th><th>Fair</th><th>Edge</th><th>Min</th><th>Move</th><th>CLV</th><th>Quality</th><th>Result</th><th>Net P&L</th></tr></thead><tbody>{brows}</tbody></table></div>
    <div class='section-grid'><div class='panel'><h2>By sport</h2>{seg_table(segments['sport'])}</div><div class='panel'><h2>By league</h2>{seg_table(segments['league'])}</div></div>
    <div class='section-grid'><div class='panel'><h2>By odds band</h2>{seg_table(segments['odds_band'])}</div><div class='panel'><h2>Favourite vs outsider</h2>{seg_table(segments['side'])}</div></div>
    <div class='panel'><h2>Target / active leagues</h2><table><thead><tr><th>Active</th><th>Sport</th><th>League</th><th>Provider key</th><th>Last breadth</th><th>Last convergence</th></tr></thead><tbody>{lrows}</tbody></table></div>
    <div class='panel'><h2>Moneyline execution funnel</h2><table><thead><tr><th>Decision</th><th>Reason</th><th>Count</th></tr></thead><tbody>{funnel_rows}</tbody></table></div>
    <div class='panel'><h2>Latest execution audit</h2><table><thead><tr><th>Evaluated</th><th>League key</th><th>Selection</th><th>Executable</th><th>Min</th><th>Fair</th><th>Edge</th><th>Decision</th><th>Reason</th></tr></thead><tbody>{erows}</tbody></table></div>
    <p><a class='button secondary' href='/multisport-lines'>Open spreads / totals shadow →</a></p>
    </body></html>""")

@app.get('/tennis',response_class=HTMLResponse)
def tennis_page():
    
    score=tennis_scoreboard(db);segments=tennis_segments(db)
    bets=latest_tennis_bets(db,100)
    tournaments=db.fetchall(
        "SELECT * FROM tennis_tournament_state ORDER BY active DESC,tour,title"
    )
    evals=db.fetchall(
        "SELECT * FROM tennis_execution_evaluations ORDER BY id DESC LIMIT 100"
    )
    cards=[
        ("Events",score["events"]),("Active tournaments",score["active_tournaments"]),
        ("Executable shadows",score["bets"]),("Settled",score["settled"]),
        ("A/B CLV samples",score["clv_samples"]),("A/B avg CLV",f"{_fmt(score['avg_clv_pct'])}%"),
        ("Median CLV",f"{_fmt(score['median_clv_pct'])}%"),("Beat close",f"{_fmt(score['beat_close_pct'])}%"),
        ("Gross P&L u",_fmt(score["gross_pnl_units"])),("Net P&L u",_fmt(score["net_pnl_units"])),
        ("Net ROI",f"{_fmt(score['net_roi_pct'])}%"),("Win rate",f"{_fmt(score['win_rate_pct'])}%"),
        ("Brier",_fmt(score["calibration"]["brier_score"],4)),
        ("Tennis credits today",f"{tennis_engine.quota.today_paid_cost()}/{settings.tennis_daily_paid_credit_budget}"),
    ]
    card_html=''.join(
        f"<div class='card'><div class='label'>{escape(str(k))}</div><div class='value'>{escape(str(v))}</div></div>"
        for k,v in cards
    )
    brows=''.join(
        f"<tr><td>{x['id']}</td><td>{escape(x['tour'])}</td><td>{escape(x['tournament_title'])}</td>"
        f"<td>{escape(x['player_one'])} v {escape(x['player_two'])}</td><td>{escape(x['selection'])}</td>"
        f"<td>{escape(x['bookmaker_title'])}</td><td>{_fmt(x['offered_odds'])}</td><td>{_fmt(x['fair_odds'])}</td>"
        f"<td>{_fmt(x['edge_pct'])}%</td><td>{_fmt(x['min_odds'])}</td><td>{_fmt(x.get('latest_move_pct'))}%</td>"
        f"<td>{_fmt(x.get('clv_pct'))}%</td><td>{escape(str(x.get('clv_quality') or 'PENDING'))}</td>"
        f"<td>{escape(str(x.get('result') or 'PENDING'))}</td><td>{_fmt(x.get('net_pnl_units'))}</td></tr>"
        for x in bets
    ) or "<tr><td colspan='15'>No Tennis Shadow executions yet.</td></tr>"
    trows=''.join(
        f"<tr><td>{escape(str(x['active']))}</td><td>{escape(x['tour'])}</td><td>{escape(x['title'])}</td>"
        f"<td>{escape(x['tournament_level'])}</td><td>{escape(str(x.get('last_broad_poll_at') or '—'))}</td>"
        f"<td>{escape(str(x.get('last_convergence_poll_at') or '—'))}</td></tr>"
        for x in tournaments
    ) or "<tr><td colspan='6'>No active tennis tournaments discovered yet.</td></tr>"
    erows=''.join(
        f"<tr><td>{escape(x['evaluated_at'])}</td><td>{escape(x['selection'])}</td>"
        f"<td>{_fmt(x.get('best_executable_odds'))}</td><td>{_fmt(x.get('min_required_odds'))}</td>"
        f"<td>{_fmt(x.get('fair_odds'))}</td><td>{_fmt(x.get('edge_pct'))}%</td>"
        f"<td>{escape(x['decision'])}</td><td>{escape(x['reason'])}</td></tr>"
        for x in evals
    ) or "<tr><td colspan='8'>No tennis execution evaluations yet.</td></tr>"
    def seg_table(items):
        rows=''.join(
            f"<tr><td>{escape(x['label'])}</td><td>{x['bets']}</td><td>{x['settled']}</td>"
            f"<td>{x['clv_samples']}</td><td>{_fmt(x['avg_clv_pct'])}%</td>"
            f"<td>{_fmt(x['median_clv_pct'])}%</td><td>{_fmt(x['beat_close_pct'])}%</td>"
            f"<td>{_fmt(x['net_roi_pct'])}%</td></tr>" for x in items
        ) or "<tr><td colspan='8'>No sample yet.</td></tr>"
        return f"<table><thead><tr><th>Segment</th><th>Bets</th><th>Settled</th><th>A/B</th><th>Avg CLV</th><th>Median CLV</th><th>Beat close</th><th>Net ROI</th></tr></thead><tbody>{rows}</tbody></table>"
    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Tennis Shadow</title><style>{BASE_STYLE}</style></head><body>
    <a href='/'>← Betting Lab</a><h1>Tennis Shadow <span class='pill'>TS1 · RESEARCH ONLY</span></h1>
    <div class='sub'>Two-way match-winner consensus → approved execution price → closing-price validation. Separate from football and capped at {settings.tennis_daily_paid_credit_budget} paid credits/day.</div>
    <div class='grid'>{card_html}</div>
    <div class='panel'><h2>Latest Tennis Shadow bets</h2><table><thead><tr><th>ID</th><th>Tour</th><th>Tournament</th><th>Match</th><th>Selection</th><th>Venue</th><th>Entry</th><th>Fair</th><th>Edge</th><th>Min</th><th>Latest move</th><th>CLV</th><th>Quality</th><th>Result</th><th>Net P&L</th></tr></thead><tbody>{brows}</tbody></table></div>
    <div class='section-grid'><div class='panel'><h2>By tour</h2>{seg_table(segments['tour'])}</div><div class='panel'><h2>By odds band</h2>{seg_table(segments['odds_band'])}</div></div>
    <div class='section-grid'><div class='panel'><h2>Favourite vs outsider</h2>{seg_table(segments['side'])}</div><div class='panel'><h2>By venue</h2>{seg_table(segments['venue'])}</div></div>
    <div class='panel'><h2>Active tournaments</h2><table><thead><tr><th>Active</th><th>Tour</th><th>Tournament</th><th>Level</th><th>Last breadth</th><th>Last convergence</th></tr></thead><tbody>{trows}</tbody></table></div>
    <div class='panel'><h2>Latest execution audit</h2><table><thead><tr><th>Evaluated</th><th>Selection</th><th>Executable</th><th>Min</th><th>Fair</th><th>Edge</th><th>Decision</th><th>Reason</th></tr></thead><tbody>{erows}</tbody></table></div>
    </body></html>""")

@app.get('/multiples',response_class=HTMLResponse)
def multiples_page():
    
    score=multiples_scoreboard(db)
    recent=latest_multiple_shadows(db,100)

    cards=[
        ('Research shadows',score['bets']),('Open',score['open']),('Settled',score['settled']),
        ('Doubles',score['segments']['leg_count']['2']['bets']),('Trebles',score['segments']['leg_count']['3']['bets']),
        ('Avg combined odds',_fmt(score['avg_combined_odds'])),('Median odds',_fmt(score['median_combined_odds'])),
        ('P&L u',_fmt(score['pnl_units'])),('ROI',f"{_fmt(score['roi_pct'])}%"),
        ('A/B CLV samples',score['clv_samples']),('A/B Avg CLV',f"{_fmt(score['avg_clv_pct'])}%"),
        ('Beat close',f"{_fmt(score['beat_close_pct'])}%"),('Max DD u',_fmt(score['max_drawdown_units'])),
    ]
    card_html=''.join(f"<div class='card'><div class='label'>{escape(str(k))}</div><div class='value'>{escape(str(v))}</div></div>" for k,v in cards)

    def segment_rows(items):
        if isinstance(items,dict):
            items=[dict(v,key=k,label=("Doubles" if k=='2' else "Trebles" if k=='3' else k)) for k,v in items.items()]
        return ''.join(
            f"<tr><td>{escape(str(x.get('label') or x.get('key')))}</td><td>{x['bets']}</td><td>{x['settled']}</td><td>{_fmt(x['avg_combined_odds'])}</td>"
            f"<td>{_fmt(x['pnl_units'])}</td><td>{_fmt(x['roi_pct'])}%</td><td>{x['clv_samples']}</td><td>{_fmt(x['avg_clv_pct'])}%</td><td>{_fmt(x['beat_close_pct'])}%</td></tr>"
            for x in items
        ) or "<tr><td colspan='9'>No sample yet.</td></tr>"

    recent_rows=[]
    for m in recent:
        legs='<br>'.join(
            f"{i+1}. {escape(str(l['home_team']))} v {escape(str(l['away_team']))} — {_market_label(l['market_key'])} {escape(str(l['selection']))} @ {_fmt(l['entry_odds'])}"
            for i,l in enumerate(m.get('legs',[]))
        )
        recent_rows.append(
            f"<tr><td>{m['id']}</td><td>{escape(str(m['created_at']))}</td><td>{m['leg_count']}</td><td>{escape(str(m['bookmaker_title']))}</td><td>{legs}</td>"
            f"<td>{_fmt(m['combined_odds'])}</td><td>{_fmt(m['fair_odds'])}</td><td>{_fmt(m['edge_pct'])}%</td><td>{_fmt(m['max_entry_quote_age_minutes'])}</td>"
            f"<td>{_fmt(m.get('closing_combined_odds'))}</td><td>{escape(str(m.get('clv_quality') or 'PENDING'))}</td><td>{_fmt(m.get('clv_pct'))}%</td>"
            f"<td>{escape(str(m.get('result') or 'PENDING'))}</td><td>{_fmt(m.get('pnl_units'))}</td></tr>"
        )
    recent_html=''.join(recent_rows) or "<tr><td colspan='14'>No multiples formed yet.</td></tr>"
    started=escape(str(score.get('started_at') or '—'))
    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Multiples Shadow v{VERSION}</title><style>{BASE_STYLE}</style></head><body>
    <a href='/'>← Betting Lab</a><h1>Multiples Shadow <span class='pill'>RESEARCH ONLY</span></h1>
    <div class='sub'>Algorithm {escape(str(score['algorithm_version']))} · forward collection started {started}</div>
    <div class='panel'><strong>Formation rule:</strong> use current executable-shadow singles only; doubles/trebles; different fixtures; same UK kickoff date; top {score['max_source_legs_per_uk_date']} source singles by modeled edge per date; all legs must have a quote no more than {score['quote_freshness_minutes']:.0f} minutes old at the same <strong>verified accumulator-capable API venue</strong>; every leg price must still meet its frozen singles minimum. First actionable same-venue wave is frozen. <strong>No provider calls are made by this layer.</strong><br><br><span class='muted'>Configured multiple API venues: {', '.join(settings.multiples_api_bookmaker_keys) if settings.multiples_api_bookmaker_keys else 'NONE — new formation is intentionally paused'}. Legacy non-API MS1 rows remain in Postgres/export for historical research but are not shown as API-ready bets. Combined fair probability still assumes different-fixture legs are independent.</span></div>
    <div class='grid'>{card_html}</div>
    <div class='section-grid'>
      <div class='panel'><h2>Doubles vs trebles</h2><table><thead><tr><th>Type</th><th>Bets</th><th>Settled</th><th>Avg odds</th><th>P&L u</th><th>ROI</th><th>A/B CLV n</th><th>A/B CLV</th><th>Beat close</th></tr></thead><tbody>{segment_rows(score['segments']['leg_count'])}</tbody></table></div>
      <div class='panel'><h2>Combined odds bands</h2><table><thead><tr><th>Band</th><th>Bets</th><th>Settled</th><th>Avg odds</th><th>P&L u</th><th>ROI</th><th>A/B CLV n</th><th>A/B CLV</th><th>Beat close</th></tr></thead><tbody>{segment_rows(score['segments']['odds_band'])}</tbody></table></div>
    </div>
    <div class='section-grid'>
      <div class='panel'><h2>Market mix</h2><table><thead><tr><th>Mix</th><th>Bets</th><th>Settled</th><th>Avg odds</th><th>P&L u</th><th>ROI</th><th>A/B CLV n</th><th>A/B CLV</th><th>Beat close</th></tr></thead><tbody>{segment_rows(score['segments']['market_mix'])}</tbody></table></div>
      <div class='panel'><h2>Bookmaker</h2><table><thead><tr><th>Book</th><th>Bets</th><th>Settled</th><th>Avg odds</th><th>P&L u</th><th>ROI</th><th>A/B CLV n</th><th>A/B CLV</th><th>Beat close</th></tr></thead><tbody>{segment_rows(score['segments']['bookmaker'])}</tbody></table></div>
    </div>
    <div class='panel'><h2>Latest multiples</h2><table><thead><tr><th>ID</th><th>Formed</th><th>Legs</th><th>Book</th><th>Selections</th><th>Entry odds</th><th>Fair odds</th><th>Model edge</th><th>Max quote age m</th><th>Close odds</th><th>CLV quality</th><th>CLV</th><th>Result</th><th>P&L u</th></tr></thead><tbody>{recent_html}</tbody></table></div>
    </body></html>""")

@app.get('/manual-systems',response_class=HTMLResponse)
def manual_systems_page():
    
    score=manual_systems_scoreboard(db)
    recent=latest_manual_system_cards(db,150)
    cards=[
        ('Cards',score['cards']),('Manual-placeable',score['manual_placeable_cards']),
        ('Synthetic comparisons',score['synthetic_comparison_cards']),('Open',score['open']),
        ('Settled',score['settled']),('System P&L u',_fmt(score['system_pnl_units'])),
        ('System ROI',f"{_fmt(score['system_roi_pct'])}%"),('Singles control P&L u',_fmt(score['singles_pnl_units'])),
        ('Singles ROI',f"{_fmt(score['singles_roi_pct'])}%"),('System − singles u',_fmt(score['system_minus_singles_units'])),
        ('A/B CLV samples',score['ab_clv_samples']),('A/B CLV',f"{_fmt(score['avg_clv_pct'])}%"),
        ('Quote credits today',f"{score['quote_credits_today']}/{settings.manual_systems_daily_credit_budget}"),
    ]
    card_html=''.join(f"<div class='card'><div class='label'>{escape(str(k))}</div><div class='value'>{escape(str(v))}</div></div>" for k,v in cards)

    def seg_rows(items):
        return ''.join(
            f"<tr><td>{escape(str(x.get('label') or x.get('key')))}</td><td>{x['cards']}</td><td>{x['settled']}</td>"
            f"<td>{_fmt(x['system_pnl_units'])}</td><td>{_fmt(x['system_roi_pct'])}%</td>"
            f"<td>{_fmt(x['singles_pnl_units'])}</td><td>{_fmt(x['singles_roi_pct'])}%</td>"
            f"<td>{_fmt(x['system_minus_singles_units'])}</td><td>{x['ab_clv_samples']}</td><td>{_fmt(x['avg_clv_pct'])}%</td></tr>"
            for x in items
        ) or "<tr><td colspan='10'>No sample yet.</td></tr>"

    rows=[]
    for m in recent:
        legs='<br>'.join(
            f"{i+1}. {escape(str(l['home_team']))} v {escape(str(l['away_team']))} — {escape(str(l['source_engine']))} · {_market_label(l['market_key'])} {escape(str(l['selection']))} @ {_fmt(l['entry_odds'])}"
            for i,l in enumerate(m.get('legs',[]))
        )
        rows.append(
            f"<tr><td>{m['id']}</td><td>{escape(str(m['created_at']))}</td><td>{escape(str(m['system_type']))}</td>"
            f"<td>{escape(str(m['bookmaker_title']))}</td><td>{escape(str(m['placement_mode']))}</td><td>{escape(str(m['source_cohort']))}</td><td>{legs}</td>"
            f"<td>{m['line_count']}</td><td>{_fmt(m['expected_roi_pct'])}%</td><td>{_fmt(m['singles_expected_roi_pct'])}%</td>"
            f"<td>{escape(str(m.get('clv_quality') or 'PENDING'))}</td><td>{_fmt(m.get('clv_pct'))}%</td>"
            f"<td>{_fmt(m.get('system_pnl_units'))}</td><td>{_fmt(m.get('singles_pnl_units'))}</td><td>{_fmt((m.get('system_pnl_units') or 0)-(m.get('singles_pnl_units') or 0)) if m.get('system_pnl_units') is not None else '—'}</td></tr>"
        )
    recent_html=''.join(rows) or "<tr><td colspan='15'>No Yankee/Heinz cards formed yet.</td></tr>"
    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Manual Systems Shadow v{VERSION}</title><style>{BASE_STYLE}</style></head><body>
    <a href='/'>← Betting Lab</a><h1>Manual Systems Shadow <span class='pill'>MS2 · RESEARCH ONLY</span></h1>
    <div class='sub'>Yankee + Heinz · 1u total system stake vs the same 1u singles control · no automatic order placement</div>
    <div class='panel'><strong>Manual-placeable:</strong> {escape(', '.join(settings.manual_systems_placeable_bookmaker_keys))}. <strong>Comparison only:</strong> {escape(', '.join(settings.manual_systems_comparison_bookmaker_keys))}. Cards freeze only when all legs have fresh same-venue prices that still meet their source strategy minimum odds. Different fixtures only. Source cohorts: {escape(', '.join(settings.manual_systems_source_cohorts))}.</div>
    <div class='grid'>{card_html}</div>
    <div class='section-grid'>
      <div class='panel'><h2>Yankee vs Heinz</h2><table><thead><tr><th>Type</th><th>Cards</th><th>Settled</th><th>System P&L</th><th>System ROI</th><th>Singles P&L</th><th>Singles ROI</th><th>Δ u</th><th>A/B n</th><th>CLV</th></tr></thead><tbody>{seg_rows(score['segments']['system_type'])}</tbody></table></div>
      <div class='panel'><h2>Manual vs comparison</h2><table><thead><tr><th>Mode</th><th>Cards</th><th>Settled</th><th>System P&L</th><th>System ROI</th><th>Singles P&L</th><th>Singles ROI</th><th>Δ u</th><th>A/B n</th><th>CLV</th></tr></thead><tbody>{seg_rows(score['segments']['placement_mode'])}</tbody></table></div>
    </div>
    <div class='section-grid'>
      <div class='panel'><h2>By bookmaker</h2><table><thead><tr><th>Book</th><th>Cards</th><th>Settled</th><th>System P&L</th><th>System ROI</th><th>Singles P&L</th><th>Singles ROI</th><th>Δ u</th><th>A/B n</th><th>CLV</th></tr></thead><tbody>{seg_rows(score['segments']['bookmaker_key'])}</tbody></table></div>
      <div class='panel'><h2>By source cohort</h2><table><thead><tr><th>Cohort</th><th>Cards</th><th>Settled</th><th>System P&L</th><th>System ROI</th><th>Singles P&L</th><th>Singles ROI</th><th>Δ u</th><th>A/B n</th><th>CLV</th></tr></thead><tbody>{seg_rows(score['segments']['source_cohort'])}</tbody></table></div>
    </div>
    <div class='panel'><h2>Latest MS3 activity</h2><table><thead><tr><th>ID</th><th>Formed</th><th>Type</th><th>Book</th><th>Mode</th><th>Cohort</th><th>Legs</th><th>Lines</th><th>Exp system ROI</th><th>Exp singles ROI</th><th>CLV q</th><th>CLV</th><th>System P&L</th><th>Singles P&L</th><th>Δ</th></tr></thead><tbody>{recent_html}</tbody></table></div>
    </body></html>""")

@app.get('/cohort-systems',response_class=HTMLResponse)
def cohort_systems_page():
    
    score=cohort_systems_scoreboard(db)
    recent=latest_cohort_system_cards(db,150)
    cards=[
        ('Cards',score['cards']),('Armed',score.get('armed',0)),('Open',score['open']),('Settled',score['settled']),
        ('Rejected arms',score.get('rejected_arms',0)),('Expired arms',score.get('expired_arms',0)),
        ('Profitable cards',score['profitable_cards']),('System P&L u',_fmt(score['system_pnl_units'])),
        ('System ROI',f"{_fmt(score['system_roi_pct'])}%"),('Singles control P&L u',_fmt(score['singles_pnl_units'])),
        ('Singles ROI',f"{_fmt(score['singles_roi_pct'])}%"),('System − singles u',_fmt(score['system_minus_singles_units'])),
        ('A/B leg CLV n',score['ab_clv_samples']),('A/B leg CLV',f"{_fmt(score['avg_leg_clv_pct'])}%"),
    ]
    card_html=''.join(f"<div class='card'><div class='label'>{escape(str(k))}</div><div class='value'>{escape(str(v))}</div></div>" for k,v in cards)
    def seg_rows(items):
        return ''.join(
            f"<tr><td>{escape(str(x['label']))}</td><td>{x['cards']}</td><td>{x['settled']}</td><td>{x['profitable_cards']}</td>"
            f"<td>{_fmt(x['system_pnl_units'])}</td><td>{_fmt(x['system_roi_pct'])}%</td><td>{_fmt(x['singles_pnl_units'])}</td>"
            f"<td>{_fmt(x['singles_roi_pct'])}%</td><td>{_fmt(x['system_minus_singles_units'])}</td><td>{x['ab_clv_samples']}</td><td>{_fmt(x['avg_leg_clv_pct'])}%</td></tr>"
            for x in items
        ) or "<tr><td colspan='11'>No forward sample yet.</td></tr>"
    rows=[]
    for m in recent:
        legs='<br>'.join(
            f"{i+1}. {escape(str(l['home_team']))} v {escape(str(l['away_team']))} — {escape(str(l['source_engine']))} · {_market_label(l['market_key'])} {escape(str(l['selection']))} @ {_fmt(l['entry_odds'])}"
            for i,l in enumerate(m.get('legs',[]))
        )
        rows.append(
            f"<tr><td>{m['id']}</td><td>{escape(str(m.get('confirmed_at') or m['created_at']))}</td><td>{escape(str(m['cohort_key']))}</td><td>{escape(str(m['system_type']))}</td>"
            f"<td>{legs}</td><td>{m['line_count'] if str(m.get('status')) != 'ARMED' else '—'}</td><td>{escape(str(m.get('status') or ''))}{(' · '+escape(str(m.get('result')))) if m.get('result') else ''}</td><td>{m.get('winning_legs') if m.get('winning_legs') is not None else '—'}/{m['leg_count']}</td>"
            f"<td>{_fmt(m.get('system_pnl_units'))}</td><td>{_fmt(m.get('singles_pnl_units'))}</td><td>{_fmt((m.get('system_pnl_units') or 0)-(m.get('singles_pnl_units') or 0)) if m.get('system_pnl_units') is not None else '—'}</td></tr>"
        )
    recent_html=''.join(rows) or "<tr><td colspan='11'>No cards formed yet.</td></tr>"
    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>MS3 Cohort Systems v{VERSION}</title><style>{BASE_STYLE}</style></head><body>
    <a href='/'>← Betting Lab</a><h1>Cohort Systems Shadow <span class='pill'>MS3 · FORWARD ONLY</span></h1>
    <div class='sub'>Frozen from {escape(str(score['started_at']))} · algorithm {escape(str(score['algorithm_version']))}</div>
    <div class='panel'><strong>Rules frozen:</strong> BTTS Yankee = 4 PRED1/PRED2 BTTS legs; BTTS Heinz = 6. Odds-band systems use selections priced 4.00–7.49. Hybrid Yankee = 3 BTTS + 1 odds-band; Hybrid Heinz = 4 BTTS + 2 odds-band. Different fixtures only. Each exact selection can be used once per confirmed cohort/system experiment. <strong>v0.19.2 formation:</strong> a prospective card first ARMS from the recent candidate pool, then its exact event/market legs receive priority approved-venue refreshes. The card becomes OPEN only when every leg has a post-arm quote no more than 45 minutes old and still meets its threshold/band; otherwise it is rejected or expires. 1u total system stake is compared with the same 1u split across singles. <strong>Prices are synthetic constituent products, not native bookmaker multiple quotes.</strong> Shadow-only; no order placement.</div>
    <div class='grid'>{card_html}</div>
    <div class='section-grid'><div class='panel'><h2>Six frozen cohorts</h2><table><thead><tr><th>Cohort · System</th><th>Cards</th><th>Settled</th><th>Profitable</th><th>System P&L</th><th>System ROI</th><th>Singles P&L</th><th>Singles ROI</th><th>Δ u</th><th>A/B n</th><th>Leg CLV</th></tr></thead><tbody>{seg_rows(score['segments']['cohort_system'])}</tbody></table></div></div>
    <div class='panel'><h2>Latest cards</h2><table><thead><tr><th>ID</th><th>Formed</th><th>Cohort</th><th>System</th><th>Legs</th><th>Lines</th><th>Result</th><th>Wins</th><th>System P&L</th><th>Singles P&L</th><th>Δ</th></tr></thead><tbody>{recent_html}</tbody></table></div>
    </body></html>""")

@app.get('/research',response_class=HTMLResponse)
def research_page():
    
    upsert_weekly_report(db)
    intel=research_intelligence(db)
    overall=intel['overall'];status=intel['sample_status'];rolling=intel['rolling_7d']
    segments=intel['segments'];checkpoints=intel['price_checkpoints'];clv_results=intel['clv_vs_results']
    calibration=intel['edge_clv_calibration'];fixture_exposure=intel['fixture_exposure']
    reports=latest_weekly_reports(db,12);alerts=intel['data_quality_alerts']
    instrumentation=instrumentation_report(db)

    def metric_rows(rows):
        return ''.join(
            f"<tr><td>{escape(str(x['segment']))}</td><td>{x['bets']}</td><td>{x['settled']}</td>"
            f"<td>{_fmt(x['pnl_units'])}</td><td>{_fmt(x['roi_pct'])}%</td>"
            f"<td>{_fmt(x.get('net_pnl_units'))}</td><td>{_fmt(x.get('net_roi_pct'))}%</td>"
            f"<td>{_fmt(x['avg_edge_pct'])}%</td><td>{_fmt(x['avg_clv_pct'])}%</td>"
            f"<td>{_fmt(x['beat_close_pct'])}%</td><td>{_fmt(x['max_drawdown_units'])}</td>"
            f"<td>{escape(str(x['sample_status']))}</td></tr>"
            for x in rows
        ) or "<tr><td colspan='12'>No executable shadow bets in this segment yet.</td></tr>"

    checkpoint_rows=''.join(
        f"<tr><td>{escape(x['checkpoint'])}</td><td>{x['samples']}</td>"
        f"<td>{_fmt(x['avg_move_pct'])}%</td><td>{_fmt(x['median_move_pct'])}%</td>"
        f"<td>{_fmt(x['positive_move_pct'])}%</td></tr>"
        for x in checkpoints
    ) or "<tr><td colspan='5'>No price observations yet.</td></tr>"

    alert_rows=''.join(
        f"<tr><td>{escape(x['severity'])}</td><td>{escape(x['code'])}</td><td>{escape(x['message'])}</td></tr>"
        for x in alerts
    )

    weekly_rows=''.join(
        f"<tr><td>{escape(x['report_key'])}</td><td>{escape(x['period_start'][:10])}</td>"
        f"<td>{x['canonical_bets']}</td><td>{x['settled_bets']}</td><td>{_fmt(x['pnl_units'])}</td>"
        f"<td>{_fmt(x.get('roi_pct'))}%</td><td>{_fmt(x.get('net_pnl_units'))}</td><td>{_fmt(x.get('net_roi_pct'))}%</td>"
        f"<td>{_fmt(x.get('avg_clv_pct'))}%</td>"
        f"<td>{_fmt(x.get('beat_close_pct'))}%</td><td>{escape(x['sample_status'])}</td></tr>"
        for x in reports
    ) or "<tr><td colspan='11'>No weekly reports yet.</td></tr>"

    calibration_rows=''.join(
        f"<tr><td>{escape(x['segment'])}</td><td>{x['clv_samples']}</td>"
        f"<td>{_fmt(x['avg_model_edge_pct'])}%</td><td>{_fmt(x['avg_final_clv_pct'])}%</td>"
        f"<td>{_fmt(x['edge_minus_clv_pp'])}pp</td><td>{_fmt(x['pnl_units'])}</td>"
        f"<td>{_fmt(x['roi_pct'])}%</td><td>{_fmt(x['beat_close_pct'])}%</td></tr>"
        for x in calibration['buckets']
    ) or "<tr><td colspan='8'>No final CLV samples yet.</td></tr>"

    exposure_rows=''.join(
        f"<tr><td>{escape(x['fixture'])}</td><td>{escape(x['league'])}</td>"
        f"<td>{x['bets']}</td><td>{_fmt(x['gross_exposure_units'])}</td>"
        f"<td>{escape(x['markets'])}</td><td>{x['settled']}</td>"
        f"<td>{_fmt(x['pnl_units'])}</td><td>{_fmt(x['avg_clv_pct'])}%</td></tr>"
        for x in fixture_exposure['top_fixtures']
        if int(x['bets']) > 1
    ) or "<tr><td colspan='8'>No multi-bet fixture exposure yet.</td></tr>"

    cards=[
        ('Evidence status',status['level']),('Executable bets',overall['bets']),
        ('Settled',overall['settled']),('CLV samples',overall['clv_samples']),
        ('Gross P&L u',_fmt(overall['pnl_units'])),('Gross ROI',f"{_fmt(overall['roi_pct'])}%"),
        ('Net P&L u',_fmt(overall['net_pnl_units'])),('Net ROI',f"{_fmt(overall['net_roi_pct'])}%"),
        ('A/B Avg CLV',f"{_fmt(overall['avg_clv_pct'])}%"),('A/B CLV samples',overall['clv_samples']),
        ('All-close CLV',f"{_fmt(overall['all_avg_clv_pct'])}%"),('All CLV samples',overall['all_clv_samples']),
        ('7d bets',rolling['bets']),('7d P&L u',_fmt(rolling['pnl_units'])),
    ]
    card_html=''.join(
        f"<div class='card'><div class='label'>{escape(str(k))}</div><div class='value'>{escape(str(v))}</div></div>"
        for k,v in cards
    )

    table_header="<thead><tr><th>Segment</th><th>Bets</th><th>Settled</th><th>Gross P&L u</th><th>Gross ROI</th><th>Net P&L u</th><th>Net ROI</th><th>Avg edge</th><th>A/B Avg CLV</th><th>Beat close</th><th>Max DD</th><th>Evidence</th></tr></thead>"

    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Betting Lab Research Intelligence</title><style>{BASE_STYLE}</style></head><body>
    <a href='/'>← Betting Lab</a><h1>Execution Research Intelligence — v{VERSION}</h1>
    <div class='sub'>{escape(status['note'])} Approved-venue execution shadows only. Thresholds and signal rules are unchanged; this page is observational.</div>
    <div class='grid'>{card_html}</div>
    <div class='panel'><h2>Data quality</h2><table><thead><tr><th>Severity</th><th>Code</th><th>Message</th></tr></thead><tbody>{alert_rows}</tbody></table></div>
    <div class='panel'><h2>CLV measurement quality</h2><div class='muted'>A = ≤15m before kickoff · B = 15–30m · C = 30–60m · STALE = &gt;60m. Only A/B enters headline CLV and model-edge calibration.</div><table>{table_header}<tbody>{metric_rows(segments['clv_quality'])}</tbody></table></div>
    <div class='panel'><h2>Edge calibration</h2><div class='muted'>Tests whether larger modelled edges actually produce better closing-price and outcome performance.</div><table>{table_header}<tbody>{metric_rows(segments['edge'])}</tbody></table></div>
    <div class='panel'><h2>Model edge vs final CLV</h2><div class='muted'>A positive Edge−CLV gap means the model's claimed edge was more optimistic than subsequent closing-market validation. Overall A/B sample: model edge {_fmt(calibration['avg_model_edge_pct'])}% vs final CLV {_fmt(calibration['avg_final_clv_pct'])}% · gap {_fmt(calibration['edge_minus_clv_pp'])}pp across {calibration['samples']} high-quality samples ({calibration['all_clv_samples']} total closes retained).</div><table><thead><tr><th>Edge bucket</th><th>CLV samples</th><th>Avg model edge</th><th>Avg final CLV</th><th>Edge−CLV gap</th><th>P&L u</th><th>ROI</th><th>Beat close</th></tr></thead><tbody>{calibration_rows}</tbody></table></div>
    <div class='section-grid'>
      <div class='panel'><h2>Strategy agreement</h2><div class='muted'>1 vs 2 vs 3+ supporting strategies.</div><table>{table_header}<tbody>{metric_rows(segments['strategy_agreement'])}</tbody></table></div>
      <div class='panel'><h2>Bookmaker agreement</h2><div class='muted'>1 vs 2 vs 3+ approved automation-capable execution venues with acceptable prices.</div><table>{table_header}<tbody>{metric_rows(segments['bookmaker_agreement'])}</tbody></table></div>
    </div>
    <div class='section-grid'>
      <div class='panel'><h2>Market performance</h2><table>{table_header}<tbody>{metric_rows(segments['market'])}</tbody></table></div>
      <div class='panel'><h2>League performance</h2><table>{table_header}<tbody>{metric_rows(segments['league'])}</tbody></table></div>
    </div>
    <div class='section-grid'>
      <div class='panel'><h2>Odds-band performance</h2><div class='muted'>Tracks whether longshots systematically fail to validate at the close.</div><table>{table_header}<tbody>{metric_rows(segments['odds_band'])}</tbody></table></div>
      <div class='panel'><h2>Time-to-kickoff performance</h2><div class='muted'>Entry time buckets: &lt;2h, 2–6h, 6–24h, 1–3d, 3d+.</div><table>{table_header}<tbody>{metric_rows(segments['time_to_kickoff'])}</tbody></table></div>
    </div>
    <div class='panel'><h2>Entry bookmaker performance</h2><table>{table_header}<tbody>{metric_rows(segments['bookmaker'])}</tbody></table></div>
    <div class='panel'><h2>Fixture exposure / correlation</h2><div class='muted'>{fixture_exposure['multi_bet_fixtures']} fixtures currently carry multiple executable bets; { _fmt(fixture_exposure['pct_bets_on_multi_bet_fixtures']) }% of all executable bets sit on multi-bet fixtures. Maximum exposure on one fixture: {fixture_exposure['max_bets_one_fixture']} units at flat 1u sizing.</div><table><thead><tr><th>Fixture</th><th>League</th><th>Bets</th><th>Gross exposure u</th><th>Markets</th><th>Settled</th><th>P&L u</th><th>Avg CLV</th></tr></thead><tbody>{exposure_rows}</tbody></table><h3>Performance by bets-per-fixture</h3><table>{table_header}<tbody>{metric_rows(fixture_exposure['exposure_buckets'])}</tbody></table></div>
    <div class='panel'><h2>Price movement checkpoints</h2><div class='muted'>Positive move means the market shortened after our canonical entry.</div><table><thead><tr><th>Checkpoint</th><th>Samples</th><th>Avg move</th><th>Median move</th><th>% positive</th></tr></thead><tbody>{checkpoint_rows}</tbody></table></div>
    <div class='panel'><h2>CLV vs actual outcomes</h2><div class='muted'>Separates bets that beat the closing line from those that did not, helping distinguish genuine pricing signal from short-run result variance.</div><table>{table_header}<tbody>{metric_rows(clv_results)}</tbody></table></div>
    <div class='panel'><h2>Data engine instrumentation <span class='pill'>v0.6.10</span></h2>
      <div class='muted'>Measurement only; zero additional provider calls. New cohorts are fingerprinted by app/experiment/config, entry consensus quality is frozen, broad-market closes are benchmarked, probability calibration excludes unverified regulation-time competitions, and Multiples overlap is quantified.</div>
      <div class='grid'>
        <div class='card'><div class='label'>Singles calibrated</div><div class='value'>{instrumentation['probability_calibration']['singles']['samples']}</div></div>
        <div class='card'><div class='label'>Singles Brier</div><div class='value'>{_fmt(instrumentation['probability_calibration']['singles']['brier_score'],4)}</div></div>
        <div class='card'><div class='label'>Entry market measured</div><div class='value'>{instrumentation['entry_market_quality'].get('measured') or 0}</div></div>
        <div class='card'><div class='label'>Reference closes</div><div class='value'>{instrumentation['closing_reference_benchmarks'].get('measured') or 0}</div></div>
        <div class='card'><div class='label'>Unique multiple legs</div><div class='value'>{instrumentation['multiples_overlap']['unique_source_legs']}</div></div>
        <div class='card'><div class='label'>Max leg reuse</div><div class='value'>{instrumentation['multiples_overlap']['max_reuse_one_source_leg']}</div></div>
      </div>
    </div>
    <div class='panel'><h2>Automatic weekly reports</h2><div class='muted'>Current ISO week is refreshed by the worker and historical weeks remain in Postgres.</div><table><thead><tr><th>Week</th><th>Start</th><th>Bets</th><th>Settled</th><th>Gross P&L u</th><th>Gross ROI</th><th>Net P&L u</th><th>Net ROI</th><th>A/B Avg CLV</th><th>Beat close</th><th>Evidence</th></tr></thead><tbody>{weekly_rows}</tbody></table></div>
    </body></html>""")

@app.get('/event/{event_id}',response_class=HTMLResponse)
def event_page(event_id:str):
    snap=event_market_snapshot(db,event_id);event=snap.get('event')
    if not event: raise HTTPException(status_code=404,detail='event not found')
    quotes=snap['quotes'];cons=db.fetchall("""SELECT * FROM consensus_snapshots WHERE event_id=? AND captured_at=(SELECT MAX(captured_at) FROM consensus_snapshots WHERE event_id=?) ORDER BY market_key,point,selection""",(event_id,event_id))
    sigs=db.fetchall("SELECT * FROM signals WHERE event_id=? ORDER BY id DESC",(event_id,));evals=db.fetchall("SELECT * FROM candidate_evaluations WHERE event_id=? ORDER BY id DESC LIMIT 100",(event_id,))
    canons=db.fetchall("SELECT * FROM canonical_bets WHERE event_id=? ORDER BY id DESC",(event_id,))
    execs=db.fetchall("SELECT * FROM execution_shadow_bets WHERE event_id=? ORDER BY id DESC",(event_id,))
    qrows=''.join(f"<tr><td>{_market_label(x['market_key'])}</td><td>{escape(x['outcome_name'])}</td><td>{escape(x['bookmaker_title'])}</td><td>{_fmt(x.get('point'))}</td><td>{_fmt(x['price'])}</td></tr>" for x in quotes) or "<tr><td colspan='5'>No odds snapshot yet.</td></tr>"
    crows=''.join(f"<tr><td>{_market_label(x['market_key'])}</td><td>{escape(x['selection'])}</td><td>{_fmt(x.get('point'))}</td><td>{_fmt(x['fair_probability']*100)}%</td><td>{_fmt(x['fair_odds'])}</td><td>{x['num_books']}</td></tr>" for x in cons) or "<tr><td colspan='6'>No consensus yet.</td></tr>"
    srows=''.join(f"<tr><td><a href='/signal/{x['id']}'>{x['id']}</a></td><td>{escape(x['strategy'])}</td><td>{escape(x['selection'])}</td><td>{escape(x['bookmaker_title'])}</td><td>{_fmt(x['offered_odds'])}</td><td>{_fmt(x['fair_odds'])}</td><td>{_fmt(x['edge_pct'])}%</td></tr>" for x in sigs) or "<tr><td colspan='7'>No detections.</td></tr>"
    canonrows=''.join(f"<tr><td>{x['id']}</td><td>{escape(x['selection'])}</td><td>{escape(x['bookmaker_title'])}</td><td>{_fmt(x['offered_odds'])}</td><td>{x['strategy_count']}</td><td>{x['bookmaker_count']}</td><td>{x['detection_count']}</td><td>{escape(str(x.get('result') or 'PENDING'))}</td><td>{_fmt(x.get('pnl_units'))}</td></tr>" for x in canons) or "<tr><td colspan='9'>No canonical bets.</td></tr>"
    execrows=''.join(f"<tr><td>{x['id']}</td><td>{escape(x['selection'])}</td><td>{escape(x['bookmaker_title'])}</td><td>{_fmt(x['offered_odds'])}</td><td>{_fmt(x['min_odds'])}</td><td>{_fmt(x['edge_pct'])}%</td><td>{escape(str(x.get('clv_quality') or 'PENDING'))}</td><td>{escape(str(x.get('result') or 'PENDING'))}</td><td>{_fmt(x.get('pnl_units'))}</td><td>{_fmt(x.get('net_pnl_units'))}</td></tr>" for x in execs) or "<tr><td colspan='10'>No executable shadow bets.</td></tr>"
    erows=''.join(f"<tr><td>{escape(x['strategy'])}</td><td>{_market_label(x['market_key'])}</td><td>{escape(str(x.get('selection') or '—'))}</td><td>{escape(str(x.get('bookmaker_title') or '—'))}</td><td>{_fmt(x.get('offered_odds'))}</td><td>{_fmt(x.get('fair_odds'))}</td><td>{_fmt(x.get('edge_pct'))}%</td><td>{escape(x['decision'])}</td><td>{escape(x['reason'])}</td></tr>" for x in evals) or "<tr><td colspan='9'>No audit rows.</td></tr>"
    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{escape(event['home_team'])} v {escape(event['away_team'])}</title><style>{BASE_STYLE}</style></head><body><a href='/'>← Betting Lab</a><h1>{escape(event['home_team'])} v {escape(event['away_team'])}</h1><div class='sub'>{escape(event['league'])} · {escape(event['commence_time'])} · snapshot {escape(str(snap.get('captured_at') or '—'))}</div>
    <div class='panel'><h2>Latest bookmaker market</h2><table><thead><tr><th>Market</th><th>Selection</th><th>Book</th><th>Point</th><th>Odds</th></tr></thead><tbody>{qrows}</tbody></table></div>
    <div class='panel'><h2>Consensus fair prices</h2><table><thead><tr><th>Market</th><th>Selection</th><th>Point</th><th>Probability</th><th>Fair odds</th><th>Books</th></tr></thead><tbody>{crows}</tbody></table></div>
    <div class='panel'><h2>Executable shadow bets</h2><table><thead><tr><th>ID</th><th>Selection</th><th>API venue</th><th>Entry</th><th>Min</th><th>Edge</th><th>CLV quality</th><th>Result</th><th>Gross P&L u</th><th>Net P&L u</th></tr></thead><tbody>{execrows}</tbody></table></div>
    <div class='panel'><h2>Theoretical canonical opportunities</h2><table><thead><tr><th>ID</th><th>Selection</th><th>Best book</th><th>Entry</th><th>Strategies</th><th>Books</th><th>Detections</th><th>Result</th><th>P&L u</th></tr></thead><tbody>{canonrows}</tbody></table></div>
    <div class='panel'><h2>Raw detections</h2><table><thead><tr><th>ID</th><th>Strategy</th><th>Selection</th><th>Book</th><th>Offered</th><th>Fair</th><th>Edge</th></tr></thead><tbody>{srows}</tbody></table></div>
    <div class='panel'><h2>Decision audit</h2><table><thead><tr><th>Strategy</th><th>Market</th><th>Selection</th><th>Book</th><th>Offered</th><th>Fair</th><th>Edge</th><th>Decision</th><th>Reason</th></tr></thead><tbody>{erows}</tbody></table></div></body></html>""")

@app.get('/signal/{signal_id}',response_class=HTMLResponse)
def signal_page(signal_id:int):
    sig=db.fetchone("""SELECT s.*,e.home_team,e.away_team,e.league,e.commence_time FROM signals s JOIN events e ON e.event_id=s.event_id WHERE s.id=?""",(signal_id,))
    if not sig: raise HTTPException(status_code=404,detail='signal not found')
    hist=signal_price_history(db,signal_id)
    rows=''.join(f"<tr><td>{escape(x['source_snapshot_at'])}</td><td>{_fmt(x['price'])}</td><td>{_fmt(x['move_vs_entry_pct'])}%</td></tr>" for x in hist) or "<tr><td colspan='3'>No follow-up observation yet.</td></tr>"
    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Signal {signal_id}</title><style>{BASE_STYLE}</style></head><body><a href='/'>← Betting Lab</a><h1>Signal #{signal_id} — {escape(sig['strategy'])}</h1><div class='sub'>{escape(sig['home_team'])} v {escape(sig['away_team'])} · {_market_label(sig['market_key'])} · {escape(sig['selection'])}</div><div class='grid'><div class='card'><div class='label'>Bookmaker</div><div class='value'>{escape(sig['bookmaker_title'])}</div></div><div class='card'><div class='label'>Entry odds</div><div class='value'>{_fmt(sig['offered_odds'])}</div></div><div class='card'><div class='label'>Fair odds</div><div class='value'>{_fmt(sig['fair_odds'])}</div></div><div class='card'><div class='label'>Edge %</div><div class='value'>{_fmt(sig['edge_pct'])}</div></div><div class='card'><div class='label'>Final closing</div><div class='value'>{_fmt(sig.get('closing_odds'))}</div></div><div class='card'><div class='label'>Final CLV %</div><div class='value'>{_fmt(sig.get('clv_pct'))}</div></div></div><div class='panel'><h2>Price convergence path</h2><table><thead><tr><th>Snapshot</th><th>Observed odds</th><th>Move vs entry</th></tr></thead><tbody>{rows}</tbody></table></div></body></html>""")
