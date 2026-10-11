from __future__ import annotations

"""Low-latency companion loop for the forward Live Yankee Feed.

The main Betting Lab worker has become intentionally broad and can take tens of
minutes to complete a full cycle. This companion reacts to newly-written source
rows independently, without speeding up or duplicating the expensive source
research itself. Targeted bookmaker refreshes remain governed by the Yankee
feed's five-minute refresh rule and quota caps.
"""

import threading

import live_yankee_feed
import web as web_module

_INSTALLED = False
_LOCK = threading.Lock()
_STOP = threading.Event()
_THREAD = None
_ORIGINAL_RUN = None


def install() -> dict:
    global _INSTALLED, _THREAD, _ORIGINAL_RUN
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "reaction_seconds": 60}

    worker = web_module.worker
    _ORIGINAL_RUN = live_yankee_feed.run_maintenance

    def serialized_run(db, collector, settings, now=None):
        if not _LOCK.acquire(blocking=False):
            return {"ok": True, "skipped": "live_yankee_maintenance_already_running"}
        try:
            return _ORIGINAL_RUN(db, collector, settings, now=now)
        finally:
            _LOCK.release()

    # The worker wrapper installed by live_yankee_feed resolves this module
    # global at call time, so replacing it here also serializes the slower
    # end-of-cycle maintenance pass against this fast companion.
    live_yankee_feed.run_maintenance = serialized_run

    original_start = worker.start
    original_stop = worker.stop

    def loop():
        print("LIVE_YANKEE_HEARTBEAT started reaction_seconds=60", flush=True)
        # Give application startup a few seconds to finish before the first pass.
        if _STOP.wait(5):
            return
        while not _STOP.is_set():
            try:
                serialized_run(worker.db, worker.collector, web_module.settings)
            except Exception as exc:
                print(f"LIVE_YANKEE_FAST_LOOP_ERROR {type(exc).__name__}: {exc}", flush=True)
            _STOP.wait(60)

    def start_with_yankee_loop():
        global _THREAD
        original_start()
        if _THREAD is None or not _THREAD.is_alive():
            _STOP.clear()
            _THREAD = threading.Thread(
                target=loop,
                daemon=True,
                name="live-yankee-feed",
            )
            _THREAD.start()

    def stop_with_yankee_loop():
        _STOP.set()
        original_stop()

    worker.start = start_with_yankee_loop
    worker.stop = stop_with_yankee_loop
    worker._live_yankee_fast_loop_installed = True
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "reaction_seconds": 60,
        "bookmaker_refresh_minutes": live_yankee_feed.QUOTE_REFRESH_MINUTES,
        "quote_freshness_minutes": live_yankee_feed.QUOTE_FRESHNESS_MINUTES,
    }
