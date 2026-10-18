from __future__ import annotations

"""Keep FHG1 maintenance live independently of the long research worker cycle."""

import threading
from typing import Any, Dict

from first_half_goal_lab import run_maintenance

VERSION = "FHG1_HEARTBEAT_MAINT_V2"
HEARTBEAT_SECONDS = 15 * 60
_INSTALLED = False
_MAINT_LOCK = threading.Lock()


def _run_once(db, source: str) -> None:
    # Never overlap startup/worker/heartbeat maintenance. The underlying source
    # modules have their own due-throttles (6h history, 30m API-Football
    # settlement), so a 15m wake-up does not mean a paid call every 15 minutes.
    if not _MAINT_LOCK.acquire(blocking=False):
        return
    try:
        state = run_maintenance(db)
        history = dict(state.get("history") or {})
        changed = any(
            int(state.get(key) or 0) > 0
            for key in (
                "candidates_created",
                "candidates_settled",
                "cards_created",
                "cards_settled",
            )
        ) or int(history.get("attempted") or 0) > 0
        if changed:
            detail = (
                f"source={source} "
                f"history_attempted={int(history.get('attempted') or 0)} "
                f"history_ok={int(history.get('ok') or 0)} "
                f"rows_seen={int(history.get('rows_seen') or 0)} "
                f"candidates_created={int(state.get('candidates_created') or 0)} "
                f"candidates_settled={int(state.get('candidates_settled') or 0)} "
                f"cards_created={int(state.get('cards_created') or 0)} "
                f"cards_settled={int(state.get('cards_settled') or 0)}"
            )
            try:
                db.record_collector_run("FHG1_MAINT", True, detail=detail)
            except Exception:
                pass
            print(f"FHG1_RUNTIME_MAINT source={source} state={state}", flush=True)
    except Exception as exc:
        detail = f"source={source} {type(exc).__name__}: {exc}"
        try:
            db.record_collector_run("FHG1_MAINT", False, detail=detail)
        except Exception:
            pass
        print(f"FHG1_RUNTIME_MAINT_ERROR {detail}", flush=True)
    finally:
        _MAINT_LOCK.release()


def install(web_module, db) -> Dict[str, Any]:
    global _INSTALLED
    worker = web_module.worker
    if _INSTALLED or getattr(worker, "_fhg1_worker_maint", False):
        return {"installed": True, "already_installed": True, "version": VERSION}

    # Keep the worker-cycle hook as a belt-and-braces pass, but run it BEFORE
    # the long research cycle. The previous implementation ran after the cycle,
    # which could defer FHG1 settlement for hours when that cycle was busy.
    original_cycle = worker.one_cycle

    def cycle_with_fhg1_maintenance():
        _run_once(db, "worker_cycle_start")
        return original_cycle()

    worker.one_cycle = cycle_with_fhg1_maintenance
    worker._fhg1_worker_maint = True

    # More importantly, FHG1 now has its own lightweight heartbeat and no longer
    # depends on the large worker cycle finishing. Wait one heartbeat before the
    # first pass because dashboard_app already performs an immediate startup pass.
    def heartbeat() -> None:
        print(
            f"FHG1_HEARTBEAT started interval_seconds={HEARTBEAT_SECONDS}",
            flush=True,
        )
        while True:
            threading.Event().wait(HEARTBEAT_SECONDS)
            _run_once(db, "heartbeat")

    thread = threading.Thread(target=heartbeat, daemon=True, name="fhg1-heartbeat")
    thread.start()

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "heartbeat_seconds": HEARTBEAT_SECONDS,
        "worker_cycle_dependency": False,
    }
