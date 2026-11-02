from __future__ import annotations

"""Retry guard for tennis result refreshes.

A failed rich-provider result refresh must never advance the successful-refresh
clock. This keeps the refresh due so the next TPRED rich-maintenance cycle can
retry automatically. The guard also inspects recent provider failures before
starting a retry so quota/subscription failures are not hammered repeatedly.
"""

import json
import threading
from typing import Any, Dict, List

import tennis_result_pipeline_fix as pipeline

VERSION = "TENNIS_RESULT_RETRY_GUARD_V2_DIAGNOSTIC"
_INSTALLED = False


def _recent_failures(db, limit: int = 20) -> List[Dict[str, Any]]:
    try:
        return db.fetchall(
            """
            SELECT requested_at,purpose,endpoint,status_code,provider_remaining,error
            FROM tennis_rich_api_ledger
            WHERE ok=0
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        ) or []
    except Exception:
        return []


def _blocking_provider_state(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    recent = rows[:8]
    statuses = [int(r.get("status_code")) for r in recent if r.get("status_code") is not None]
    # Repeated 429 means provider-side quota exhaustion. Repeated 401/403 means
    # auth/subscription access. Neither is helped by immediate retries.
    if statuses and all(s == 429 for s in statuses):
        return {"blocked": True, "reason": "PROVIDER_QUOTA_429", "statuses": statuses}
    if statuses and all(s in {401, 403} for s in statuses):
        return {"blocked": True, "reason": "PROVIDER_AUTH_OR_PLAN", "statuses": statuses}
    return {"blocked": False, "reason": None, "statuses": statuses}


def install(db) -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    original = pipeline._refresh_recent_rich_results

    def guarded_refresh(*args, **kwargs):
        result = original(*args, **kwargs)
        if isinstance(result, dict) and not result.get("skipped") and not result.get("ok"):
            db.execute(
                "UPDATE tennis_rich_sync_state SET last_current_results_at=NULL WHERE singleton_id=1"
            )
        return result

    pipeline._refresh_recent_rich_results = guarded_refresh

    failures = _recent_failures(db)
    provider_state = _blocking_provider_state(failures)
    print(
        "TENNIS_RESULT_PROVIDER_DIAGNOSTIC "
        + json.dumps({"version": VERSION, "state": provider_state, "recent_failures": failures[:12]}, sort_keys=True, default=str),
        flush=True,
    )

    def retry_once():
        try:
            current_failures = _recent_failures(db)
            current_state = _blocking_provider_state(current_failures)
            if current_state.get("blocked"):
                bridge = pipeline.bridge_cached_results(db)
                print(
                    "TENNIS_RESULT_RETRY_GUARD_RUN "
                    + json.dumps({
                        "version": VERSION,
                        "refresh": {"ok": False, "skipped": True, "reason": current_state.get("reason"), "requests": 0},
                        "bridge": bridge,
                        "provider_state": current_state,
                    }, sort_keys=True, default=str),
                    flush=True,
                )
                return
            refresh = guarded_refresh(
                db,
                force=True,
                lookback_days=pipeline.STARTUP_REPAIR_DAYS,
                max_requests=16,
            )
            bridge = pipeline.bridge_cached_results(db)
            payload = {
                "version": VERSION,
                "refresh": refresh,
                "bridge": bridge,
                "provider_failures_after": _recent_failures(db, 8),
            }
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
        "provider_state": provider_state,
        "betting_authority_changed": False,
        "staking_changed": False,
        "alerts_changed": False,
    }
