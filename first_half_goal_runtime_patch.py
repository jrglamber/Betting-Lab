from __future__ import annotations

"""Keep FHG1 maintenance live independently of the long research worker cycle."""

import threading
from typing import Any, Dict

from first_half_goal_lab import run_maintenance

VERSION = "FHG1_HEARTBEAT_MAINT_V2_2"
HEARTBEAT_SECONDS = 5 * 60
FIRST_HEARTBEAT_SECONDS = 60
_INSTALLED = False
_MAINT_LOCK = threading.Lock()


def _run_once(db, source: str) -> None:
    # Never overlap startup/worker/heartbeat maintenance. The underlying source
    # modules have their own due-throttles (6h history, 30m API-Football
    # settlement), so a frequent wake-up does not multiply external calls.
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

    original_cycle = worker.one_cycle

    def cycle_with_fhg1_maintenance():
        _run_once(db, "worker_cycle_start")
        return original_cycle()

    worker.one_cycle = cycle_with_fhg1_maintenance
    worker._fhg1_worker_maint = True

    # The startup maintenance pass may land just inside the settlement source's
    # 30-minute due window after a deployment handover. Check once after one
    # minute, then every five minutes thereafter. The source's own due-throttle
    # remains authoritative, so this does not increase provider call cadence.
    def heartbeat() -> None:
        print(
            f"FHG1_HEARTBEAT started first_seconds={FIRST_HEARTBEAT_SECONDS} interval_seconds={HEARTBEAT_SECONDS}",
            flush=True,
        )
        waiter = threading.Event()
        waiter.wait(FIRST_HEARTBEAT_SECONDS)
        _run_once(db, "heartbeat_early")
        while True:
            waiter.wait(HEARTBEAT_SECONDS)
            _run_once(db, "heartbeat")

    thread = threading.Thread(target=heartbeat, daemon=True, name="fhg1-heartbeat")
    thread.start()

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "first_heartbeat_seconds": FIRST_HEARTBEAT_SECONDS,
        "heartbeat_seconds": HEARTBEAT_SECONDS,
        "worker_cycle_dependency": False,
    }
