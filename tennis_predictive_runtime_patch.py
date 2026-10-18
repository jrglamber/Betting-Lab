from __future__ import annotations

"""Attach TPRED maintenance and rich-source research workers to tennis runtime."""

import threading
import time
from typing import Any, Dict

from tennis_predictive import run_maintenance
import tennis_predictive3 as tpred3
from tennis_predictive3_safety_patch import install as install_tpred3_safety
from tennis_rich_context import maintenance as run_rich_context_maintenance
from tennis_rich_source import maintenance as run_rich_maintenance
from tennis_rich_source import status as rich_status

VERSION = "TPRED_RUNTIME_PATCH_V5"
RICH_FIRST_DELAY_SECONDS = 20
RICH_BACKFILL_INTERVAL_SECONDS = 15 * 60
RICH_IDLE_INTERVAL_SECONDS = 4 * 60 * 60


def _stats_backlog(db) -> int:
    """Rich rows still missing a terminal match-stat cache entry."""
    try:
        return int((db.fetchone(
            """
            SELECT COUNT(*) AS n FROM tennis_rich_matches
            WHERE stats_json IS NULL
              AND COALESCE(result_type,'completed') NOT IN ('walkover','w/o')
            """
        ) or {}).get("n") or 0)
    except Exception:
        return 0


def _run_tpred3(db) -> Dict[str, Any]:
    try:
        return tpred3.run_maintenance(db)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "betting_authority": False,
        }


def _start_rich_worker(tennis_engine, db) -> bool:
    if getattr(tennis_engine, "_tpred_rich_worker_started", False):
        return False
    tennis_engine._tpred_rich_worker_started = True

    def loop() -> None:
        time.sleep(RICH_FIRST_DELAY_SECONDS)
        while True:
            try:
                # Base collector: result universe + cached per-match stats.
                result = run_rich_maintenance(db)
                print(f"TPRED_RICH_MAINTENANCE {result}", flush=True)

                # Separate chronological context: dated rankings + upcoming
                # fixture surface. This deliberately never uses today's rank
                # for a historical prediction.
                context = run_rich_context_maintenance(db)
                print(f"TPRED_RICH_CONTEXT {context}", flush=True)

                # TPRED3 is isolated from TPRED1/2 and runs only after the rich
                # cache/context passes. Its safety patch removes the provider's
                # completed-result winner-first ordering before prediction.
                tpred3_result = _run_tpred3(db)
                print(f"TPRED3_MAINTENANCE {tpred3_result}", flush=True)

                state = rich_status(db)
                cursor = state.get("history_cursor")
                end = state.get("history_end")
                history_complete = bool(cursor and end and str(cursor) > str(end))
                stats_backlog = _stats_backlog(db)

                # Do not drop to the four-hour idle cadence merely because the
                # result calendar is complete. During the paid-data month keep
                # using the 15-minute backfill cadence until per-match rich
                # stats are also cached (subject to the provider daily cap).
                complete = history_complete and stats_backlog == 0
                delay = RICH_IDLE_INTERVAL_SECONDS if complete else RICH_BACKFILL_INTERVAL_SECONDS
            except Exception as exc:
                print(
                    f"TPRED_RICH_MAINTENANCE {{'ok': False, 'error': '{type(exc).__name__}: {exc}', 'betting_authority': False}}",
                    flush=True,
                )
                delay = RICH_BACKFILL_INTERVAL_SECONDS
            time.sleep(delay)

    threading.Thread(target=loop, name="tpred-rich-source", daemon=True).start()
    return True


def install(tennis_engine, db) -> Dict[str, Any]:
    if tennis_engine is None:
        return {"installed": False, "version": VERSION, "reason": "tennis_engine_missing"}
    if getattr(tennis_engine, "_tpred_runtime_installed", False):
        return {"installed": True, "already_installed": True, "version": VERSION}

    # Install the leakage/stat-schema guard before any TPRED3 maintenance can
    # run. This bumps TPRED3 to V2 and keeps old experimental rows versioned.
    tpred3_safety = install_tpred3_safety()

    original = tennis_engine.one_cycle

    def wrapped_one_cycle(*args, **kwargs):
        result = original(*args, **kwargs)
        try:
            tpred = run_maintenance(db)
        except Exception as exc:
            tpred = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "betting_authority": False}

        # TPRED3 also gets a lightweight normal-cycle pass. This makes forward
        # forecast creation and result settlement independent of the slower rich
        # backfill thread; it performs no RapidAPI calls itself.
        tpred3_result = _run_tpred3(db)

        if isinstance(result, dict):
            result = dict(result)
            result["tpred"] = tpred
            result["tpred3"] = tpred3_result
        return result

    tennis_engine.one_cycle = wrapped_one_cycle
    tennis_engine._tpred_runtime_installed = True
    rich_worker_started = _start_rich_worker(tennis_engine, db)
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "trigger": "after_tennis_shadow_cycle",
        "rich_source_worker": rich_worker_started,
        "rich_source_schedule": {
            "first_delay_seconds": RICH_FIRST_DELAY_SECONDS,
            "backfill_interval_seconds": RICH_BACKFILL_INTERVAL_SECONDS,
            "idle_interval_seconds": RICH_IDLE_INTERVAL_SECONDS,
            "idle_requires_stats_backlog_zero": True,
        },
        "tpred3": tpred3_safety,
        "odds_api_provider_credits": 0,
        "tpred3_provider_requests": 0,
        "rich_provider": "RapidAPI Tennis",
        "rich_provider_daily_cap": 2500,
        "betting_authority": False,
    }
