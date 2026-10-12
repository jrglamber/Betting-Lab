from __future__ import annotations

"""Keep the historical settlement rescue queue live during normal worker cycles."""

from typing import Any, Dict
from historical_rescue_patch import refresh_rescue_queue

VERSION = "HISTORICAL_RESCUE_QUEUE_GUARD_V1"
_INSTALLED = False


def install(web_module) -> Dict[str, Any]:
    global _INSTALLED
    worker = web_module.worker
    if _INSTALLED or getattr(worker, "_historical_rescue_queue_guard", False):
        return {"installed": True, "already_installed": True, "version": VERSION}

    original_cycle = worker.one_cycle

    def cycle_with_rescue_queue():
        # Refresh before and after the normal cycle: pre-cycle surfaces old debt;
        # post-cycle removes anything the normal result collectors just resolved.
        try:
            refresh_rescue_queue(web_module.db)
        except Exception as exc:
            try:
                web_module.db.record_collector_run(
                    "HISTORICAL_RESCUE_QUEUE", False,
                    detail=f"pre_cycle_error={type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
        result = original_cycle()
        try:
            queue = refresh_rescue_queue(web_module.db)
            web_module.db.record_collector_run(
                "HISTORICAL_RESCUE_QUEUE", True,
                detail=f"pending={int(queue.get('pending') or 0)}",
            )
        except Exception as exc:
            try:
                web_module.db.record_collector_run(
                    "HISTORICAL_RESCUE_QUEUE", False,
                    detail=f"post_cycle_error={type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
        return result

    worker.one_cycle = cycle_with_rescue_queue
    worker._historical_rescue_queue_guard = True
    _INSTALLED = True
    return {"installed": True, "already_installed": False, "version": VERSION}
