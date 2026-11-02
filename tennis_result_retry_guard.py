from __future__ import annotations

"""Retry guard for tennis result refreshes.

A failed rich-provider result refresh must never advance the successful-refresh
clock. This keeps the refresh due so the next TPRED rich-maintenance cycle can
retry automatically. Also performs one bounded background retry on install.
"""

import json
import threading
from typing import Any, Dict

import tennis_result_pipeline_fix as pipeline

VERSION = "TENNIS_RESULT_RETRY_GUARD_V1"
_INSTALLED = False


def install(db) -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    original = pipeline._refresh_recent_rich_results

    def guarded_refresh(*args, **kwargs):
        result = original(*args, **kwargs)
        if isinstance(result, dict) and not result.get("skipped") and not result.get("ok"):
            # A transport/provider failure is not a successful refresh. Keep the
            # result lane due instead of suppressing retries for four hours.
            db.execute(
                "UPDATE tennis_rich_sync_state SET last_current_results_at=NULL WHERE singleton_id=1"
            )
        return result

    pipeline._refresh_recent_rich_results = guarded_refresh

    def retry_once():
        try:
            refresh = guarded_refresh(
                db,
                force=True,
                lookback_days=pipeline.STARTUP_REPAIR_DAYS,
                max_requests=16,
            )
            bridge = pipeline.bridge_cached_results(db)
            payload = {"version": VERSION, "refresh": refresh, "bridge": bridge}
            print("TENNIS_RESULT_RETRY_GUARD_RUN " + json.dumps(payload, sort_keys=True, default=str), flush=True)
        except Exception as exc:
            print(
                "TENNIS_RESULT_RETRY_GUARD_ERROR "
                + json.dumps({"version": VERSION, "error": f"{type(exc).__name__}: {exc}"}),
                flush=True,
            )

    threading.Thread(
        target=retry_once,
        name="tennis-result-retry-guard",
        daemon=True,
    ).start()

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "failed_refresh_advances_clock": False,
        "background_retry_started": True,
        "betting_authority_changed": False,
        "staking_changed": False,
        "alerts_changed": False,
    }
