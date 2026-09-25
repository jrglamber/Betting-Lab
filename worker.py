from __future__ import annotations

import threading
import time
import inspect
from datetime import datetime, timezone
from typing import Callable, Optional

from collector import Collector
from signals import write_consensus_and_value_signals, write_consensus_snapshots_only, write_cross_market_signals, write_slow_book_signals
from research import track_signal_prices, finalize_closing_lines, repair_premature_clv
from db import Database
from results import ResultCollector
from canonical import cluster_event_signals, backfill_canonical_bets, sync_canonical_bets
from research_intelligence import upsert_weekly_report
from execution_shadow import (
    backfill_execution_shadows, evaluate_latest_execution_wave, track_execution_prices,
    finalize_execution_clv, settle_execution_from_stored_results,
    backfill_execution_accounting,
)
from multiples_shadow import (
    generate_multiple_shadows, finalize_multiple_clv, settle_multiple_shadows,
)
from instrumentation import run_measurement_maintenance
from meta_edge import run_meta_edge_maintenance
from meta_edge_model import run_meta_model_maintenance
from cohort_systems_shadow import run_cohort_systems_maintenance
from execution_shadow import execution_scoreboard
from football_predictive import predictive_scoreboard
from football_predictive_pred2 import predictive2_scoreboard
from football_predictive_pred3 import predictive3_scoreboard
from football_predictive_pred4 import predictive4_scoreboard


def _compact_evidence(score):
    """Small, aggregate-only research snapshot for operational observability."""
    keys = (
        "predictions", "settled_predictions", "avg_brier", "avg_log_loss",
        "market_comparison_sample", "model_brier_advantage",
        "bets", "settled_bets", "net_pnl", "net_roi_pct",
        "clv_samples", "avg_clv_pct", "beat_close_pct",
    )
    return {key: score.get(key) for key in keys if key in score}


def record_phase3_evidence_snapshot(db):
    payload = {
        "execution": _compact_evidence(execution_scoreboard(db)),
        "pred1": _compact_evidence(predictive_scoreboard(db)),
        "pred2": _compact_evidence(predictive2_scoreboard(db)),
        "pred3": _compact_evidence(predictive3_scoreboard(db)),
        "pred4": _compact_evidence(predictive4_scoreboard(db)),
    }
    detail = repr(payload)
    db.record_collector_run("PHASE3_EVIDENCE_SNAPSHOT", True, detail=detail)
    return payload


class Worker:
    def __init__(
        self,
        db: Database,
        collector: Collector,
        *,
        min_books: int,
        min_edge_pct: float,
        min_cross_market_edge_pct: float,
        min_slow_book_gap_pct: float,
        tick_seconds: int = 300,
        discovery_interval_seconds: int = 1800,
        result_collector: Optional[ResultCollector] = None,
        execution_bookmaker_keys=(),
        multiples_api_bookmaker_keys=(),
        execution_shadow_enabled: bool = True,
        tennis_engine=None,
        multisport_engine=None,
        multisport_lines_engine=None,
        predictive_football_engine=None,
        predictive_football_pred2_engine=None,
        predictive_football_pred3_engine=None,
        proxy_xg_engine=None,
        predictive_football_pred4_engine=None,
        predictive_historical_engine=None,
        manual_systems_engine=None,
        meta_edge_enabled: bool = True,
        meta_edge_model_enabled: bool = True,
        meta_edge_min_clean_labels: int = 200,
    ):
        self.db = db
        self.collector = collector
        self.min_books = min_books
        self.min_edge_pct = min_edge_pct
        self.min_cross_market_edge_pct = min_cross_market_edge_pct
        self.min_slow_book_gap_pct = min_slow_book_gap_pct
        self.tick_seconds = max(30, int(tick_seconds))
        self.discovery_interval_seconds = max(60, int(discovery_interval_seconds))
        self.result_collector = result_collector
        self.execution_bookmaker_keys = tuple(execution_bookmaker_keys)
        self.multiples_api_bookmaker_keys = tuple(
            str(x) for x in multiples_api_bookmaker_keys if str(x)
        )
        self.execution_shadow_enabled = bool(execution_shadow_enabled)
        self.tennis_engine = tennis_engine
        self.multisport_engine = multisport_engine
        self.multisport_lines_engine = multisport_lines_engine
        self.predictive_football_engine = predictive_football_engine
        self.predictive_football_pred2_engine = predictive_football_pred2_engine
        self.predictive_football_pred3_engine = predictive_football_pred3_engine
        self.proxy_xg_engine = proxy_xg_engine
        self.predictive_football_pred4_engine = predictive_football_pred4_engine
        self.predictive_historical_engine = predictive_historical_engine
        self.manual_systems_engine = manual_systems_engine
        self.meta_edge_enabled = bool(meta_edge_enabled)
        self.meta_edge_model_enabled = bool(meta_edge_model_enabled)
        self.meta_edge_min_clean_labels = int(meta_edge_min_clean_labels)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="betting-lab-worker")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def process_event(self, event_id: str):
        write_consensus_and_value_signals(
            self.db,
            event_id,
            min_books=self.min_books,
            min_edge_pct=self.min_edge_pct,
        )
        write_slow_book_signals(
            self.db,
            event_id,
            min_books=self.min_books,
            min_gap_pct=self.min_slow_book_gap_pct,
            min_edge_pct=self.min_edge_pct,
        )
        write_cross_market_signals(
            self.db,
            event_id,
            min_edge_pct=self.min_cross_market_edge_pct,
        )
        cluster_event_signals(self.db, event_id)
        if self.execution_shadow_enabled:
            evaluate_latest_execution_wave(self.db,event_id,self.execution_bookmaker_keys)

    def one_cycle(self):
        # PRED1 and PRED2 run from the same clock instant so newly due
        # forecasts are a clean paired forward experiment. PRED2 reads the
        # same stored training history and odds waves; it makes no provider
        # call of its own here.
        pred_now = datetime.now(timezone.utc)
        if self.predictive_football_engine is not None:
            try:
                pred = (self.predictive_football_engine.one_cycle() if len(inspect.signature(self.predictive_football_engine.one_cycle).parameters)==0 else self.predictive_football_engine.one_cycle(pred_now))
                self.db.record_collector_run(
                    "PREDICTIVE_FOOTBALL_MAINT", True, detail=f"cycle={pred}"
                )
            except Exception as exc:
                self.db.record_collector_run(
                    "PREDICTIVE_FOOTBALL_MAINT", False, detail=str(exc)
                )
        if self.predictive_football_pred2_engine is not None:
            try:
                pred2 = (self.predictive_football_pred2_engine.one_cycle() if len(inspect.signature(self.predictive_football_pred2_engine.one_cycle).parameters)==0 else self.predictive_football_pred2_engine.one_cycle(pred_now))
                self.db.record_collector_run(
                    "PREDICTIVE_FOOTBALL_PRED2_MAINT", True, detail=f"cycle={pred2}"
                )
            except Exception as exc:
                self.db.record_collector_run(
                    "PREDICTIVE_FOOTBALL_PRED2_MAINT", False, detail=str(exc)
                )

        if self.predictive_football_pred3_engine is not None:
            try:
                pred3 = (self.predictive_football_pred3_engine.one_cycle() if len(inspect.signature(self.predictive_football_pred3_engine.one_cycle).parameters)==0 else self.predictive_football_pred3_engine.one_cycle(pred_now))
                self.db.record_collector_run(
                    "PREDICTIVE_FOOTBALL_PRED3_MAINT", True, detail=f"cycle={pred3}"
                )
            except Exception as exc:
                self.db.record_collector_run(
                    "PREDICTIVE_FOOTBALL_PRED3_MAINT", False, detail=str(exc)
                )

        # v0.17.0 PXG1: research-only proxy-xG training/current-stat collection.
        # It has no betting authority and does not alter PRED1/PRED2/PRED3.
        if self.proxy_xg_engine is not None:
            try:
                pxg = (self.proxy_xg_engine.one_cycle() if len(inspect.signature(self.proxy_xg_engine.one_cycle).parameters)==0 else self.proxy_xg_engine.one_cycle(pred_now))
                self.db.record_collector_run(
                    "PROXY_XG_MAINT", True, detail=f"cycle={pxg}"
                )
            except Exception as exc:
                self.db.record_collector_run(
                    "PROXY_XG_MAINT", False, detail=str(exc)
                )

        # v0.19.0 PRED4: consumes only PXG1 data already stored above.
        # No provider calls and no execution authority beyond shadow creation.
        if self.predictive_football_pred4_engine is not None:
            try:
                pred4 = (self.predictive_football_pred4_engine.one_cycle() if len(inspect.signature(self.predictive_football_pred4_engine.one_cycle).parameters)==0 else self.predictive_football_pred4_engine.one_cycle(pred_now))
                self.db.record_collector_run(
                    "PREDICTIVE_FOOTBALL_PRED4_MAINT", True, detail=f"cycle={pred4}"
                )
            except Exception as exc:
                self.db.record_collector_run(
                    "PREDICTIVE_FOOTBALL_PRED4_MAINT", False, detail=str(exc)
                )

        # v0.15.0 META1: capture entry-time PRED1/PRED2 features and attach
        # future A/B-quality CLV labels. Zero provider calls; research-only.
        if self.meta_edge_enabled:
            try:
                meta = run_meta_edge_maintenance(self.db)
                self.db.record_collector_run(
                    "META_EDGE_MAINT", True,
                    detail="; ".join(f"{k}={v}" for k,v in meta.items()),
                )
                meta2 = run_meta_model_maintenance(
                    self.db,
                    min_clean_labels=self.meta_edge_min_clean_labels,
                    enabled=self.meta_edge_model_enabled,
                )
                self.db.record_collector_run(
                    "META_EDGE_MODEL_MAINT", True,
                    detail="; ".join(f"{k}={v}" for k,v in meta2.items()),
                )
            except Exception as exc:
                self.db.record_collector_run("META_EDGE_MAINT", False, detail=str(exc))

        result = self.collector.poll_one_cycle()
        if result.get("polled") and result.get("mode") == "breadth":
            for event_id in result.get("event_ids", []):
                try:
                    self.process_event(event_id)
                    self.db.record_collector_run(
                        "RESEARCH", True, event_id=event_id, detail="strategy evaluation complete"
                    )
                except Exception as exc:
                    self.db.record_collector_run(
                        "RESEARCH", False, event_id=event_id, detail=str(exc)
                    )
        elif result.get("polled") and result.get("mode") == "predictive_close_broad":
            for event_id in result.get("event_ids", []):
                try:
                    n = write_consensus_snapshots_only(
                        self.db,event_id,min_books=self.min_books
                    )
                    self.db.record_collector_run(
                        "PREDICTIVE_CLOSE_CONSENSUS", True, event_id=event_id,
                        detail=f"consensus_rows={n}; measurement_only=true",
                    )
                except Exception as exc:
                    self.db.record_collector_run(
                        "PREDICTIVE_CLOSE_CONSENSUS", False, event_id=event_id, detail=str(exc)
                    )
        try:
            tracked = track_signal_prices(self.db)
            finalized = finalize_closing_lines(self.db)
            canonical_synced = sync_canonical_bets(self.db)
            execution_tracked = track_execution_prices(self.db) if self.execution_shadow_enabled else 0
            execution_finalized = finalize_execution_clv(self.db) if self.execution_shadow_enabled else 0
            execution_settled = settle_execution_from_stored_results(self.db) if self.execution_shadow_enabled else 0
            accounting_backfilled = backfill_execution_accounting(self.db) if self.execution_shadow_enabled else 0
            weekly = upsert_weekly_report(self.db)
            evidence_snapshot = record_phase3_evidence_snapshot(self.db)
            self.db.record_collector_run(
                "RESEARCH_MAINT", True,
                detail=f"price_observations={tracked}; closing_finalized={finalized}; canonical_synced={canonical_synced}; execution_observations={execution_tracked}; execution_finalized={execution_finalized}; execution_settled={execution_settled}; execution_accounting={accounting_backfilled}; weekly_report={weekly['report_key']}"
            )
        except Exception as exc:
            self.db.record_collector_run("RESEARCH_MAINT", False, detail=str(exc))

        # v0.6.9 measurement-only maintenance. Reads/writes already-stored
        # database rows and makes zero provider calls.
        try:
            instrumentation = run_measurement_maintenance(
                self.db, excluded_books=self.execution_bookmaker_keys
            )
            self.db.record_collector_run(
                "INSTRUMENTATION_MAINT", True,
                detail="; ".join(f"{k}={v}" for k,v in instrumentation.items()),
            )
        except Exception as exc:
            self.db.record_collector_run(
                "INSTRUMENTATION_MAINT", False, detail=str(exc)
            )

        # v0.14.0 MS2 Manual Systems Shadow. Targeted William Hill/Ladbrokes
        # quote refresh plus Yankee/Heinz research cards. This lane is fully
        # shadow-only and cannot place a bet.
        if self.manual_systems_engine is not None:
            try:
                ms2 = self.manual_systems_engine.one_cycle()
                self.db.record_collector_run(
                    "MANUAL_SYSTEMS_MAINT", True, detail=f"cycle={ms2}"
                )
            except Exception as exc:
                self.db.record_collector_run(
                    "MANUAL_SYSTEMS_MAINT", False, detail=str(exc)
                )

        # v0.19.2 MS3 Cohort Systems Shadow. Forward-only BTTS, 4.00-7.49
        # and hybrid Yankee/Heinz cards now arm first, then rely on the collector
        # for one priority approved-venue refresh of each exact event/market leg
        # before confirmation. Still no order-placement authority.
        try:
            ms3 = run_cohort_systems_maintenance(self.db, now=pred_now)
            self.db.record_collector_run(
                "COHORT_SYSTEMS_MAINT", True, detail=f"cycle={ms3}"
            )
        except Exception as exc:
            self.db.record_collector_run("COHORT_SYSTEMS_MAINT", False, detail=str(exc))

        # Multiples Shadow is deliberately isolated from the primary singles
        # engine. Any failure here is audited but cannot block singles research.
        try:
            multiples_created = generate_multiple_shadows(
                self.db,
                allowed_bookmaker_keys=self.multiples_api_bookmaker_keys,
            )
            multiples_clv = finalize_multiple_clv(self.db)
            multiples_settled = settle_multiple_shadows(self.db)
            self.db.record_collector_run(
                "MULTIPLES_SHADOW_MAINT", True,
                detail=(
                    f"created={multiples_created}; clv_finalized={multiples_clv}; "
                    f"settled={multiples_settled}"
                ),
            )
        except Exception as exc:
            self.db.record_collector_run("MULTIPLES_SHADOW_MAINT", False, detail=str(exc))

        if self.result_collector is not None:
            try:
                self.result_collector.collect()
                sync_canonical_bets(self.db)
                if self.execution_shadow_enabled:
                    finalize_execution_clv(self.db)
                    settle_execution_from_stored_results(self.db)
                    backfill_execution_accounting(self.db)
                try:
                    finalize_multiple_clv(self.db)
                    settle_multiple_shadows(self.db)
                except Exception as exc:
                    self.db.record_collector_run("MULTIPLES_RESULTS_MAINT", False, detail=str(exc))
                if self.manual_systems_engine is not None:
                    try:
                        from manual_systems_shadow import finalize_manual_system_clv, settle_manual_system_shadows
                        finalize_manual_system_clv(self.db)
                        settle_manual_system_shadows(self.db)
                    except Exception as exc:
                        self.db.record_collector_run("MANUAL_SYSTEMS_RESULTS_MAINT", False, detail=str(exc))
                try:
                    from cohort_systems_shadow import settle_cohort_system_shadows
                    settle_cohort_system_shadows(self.db)
                except Exception as exc:
                    self.db.record_collector_run("COHORT_SYSTEMS_RESULTS_MAINT", False, detail=str(exc))
            except Exception as exc:
                self.db.record_collector_run("RESULTS_MAINT", False, detail=str(exc))
        # v0.7.0 Tennis Shadow is a fully isolated research lane. Failure here
        # cannot block football singles, Multiples Shadow or football results.
        if self.tennis_engine is not None:
            try:
                tennis = self.tennis_engine.one_cycle()
                self.db.record_collector_run(
                    "TENNIS_SHADOW_MAINT", True,
                    detail=f"cycle={tennis}",
                )
            except Exception as exc:
                self.db.record_collector_run(
                    "TENNIS_SHADOW_MAINT", False, detail=str(exc)
                )

        # v0.8.0 Multi-Sport Shadow is isolated from football and Tennis TS1.
        if self.multisport_engine is not None:
            try:
                multisport = self.multisport_engine.one_cycle()
                self.db.record_collector_run(
                    "MULTISPORT_SHADOW_MAINT", True,
                    detail=f"cycle={multisport}",
                )
            except Exception as exc:
                self.db.record_collector_run(
                    "MULTISPORT_SHADOW_MAINT", False, detail=str(exc)
                )

        # v0.9.0 featured spreads/totals are a separate experiment so line
        # evidence cannot contaminate MSP1 moneyline evidence.
        if self.multisport_lines_engine is not None:
            try:
                lines = self.multisport_lines_engine.one_cycle()
                self.db.record_collector_run(
                    "MULTISPORT_LINES_SHADOW_MAINT", True,
                    detail=f"cycle={lines}",
                )
            except Exception as exc:
                self.db.record_collector_run(
                    "MULTISPORT_LINES_SHADOW_MAINT", False, detail=str(exc)
                )

        # v0.13.0 historical corroboration is intentionally lowest priority.
        # It spends quota only after the live/forward research lanes have had
        # their worker turn and it never mutates forward PRED1/PRED2 rows.
        if self.predictive_historical_engine is not None:
            try:
                hist = self.predictive_historical_engine.one_cycle()
                self.db.record_collector_run(
                    "PREDICTIVE_HISTORICAL_MAINT", True, detail=f"cycle={hist}"
                )
            except Exception as exc:
                self.db.record_collector_run(
                    "PREDICTIVE_HISTORICAL_MAINT", False, detail=str(exc)
                )

        return result

    def _run(self):
        # Recovery/backfill work is intentionally off the FastAPI readiness path.
        # Run it once in the worker so deploys become healthy quickly without
        # losing historical repair coverage.
        try:
            repaired = repair_premature_clv(self.db)
            canonical = backfill_canonical_bets(self.db)
            execution = (
                backfill_execution_shadows(self.db, self.execution_bookmaker_keys)
                if self.execution_shadow_enabled else 0
            )
            self.db.record_collector_run(
                "STARTUP_RECOVERY", True,
                detail=f"premature_clv={repaired},canonical={canonical},execution={execution}",
            )
        except Exception as exc:
            self.db.record_collector_run("STARTUP_RECOVERY", False, detail=str(exc))
        last_discovery = 0.0
        while not self._stop.is_set():
            now = time.time()
            try:
                if now - last_discovery >= self.discovery_interval_seconds:
                    self.collector.discover()
                    last_discovery = now
                self.one_cycle()
            except Exception:
                # Worker failures are audited inside collector calls where possible.
                pass
            self._stop.wait(self.tick_seconds)
