from __future__ import annotations

"""Keep FHG1 history, candidate settlement and cards live during normal worker cycles."""

from typing import Any, Dict

from first_half_goal_lab import run_maintenance

VERSION = "FHG1_WORKER_MAINT_V1"
_INSTALLED = False


def install(web_module, db) -> Dict[str, Any]:
    global _INSTALLED
    worker = web_module.worker
    if _INSTALLED or getattr(worker, "_fhg1_worker_maint", False):
        return {"installed": True, "already_installed": True, "version": VERSION}

    original_cycle = worker.one_cycle

    def cycle_with_fhg1_maintenance():
        # Run after the normal collector cycle so FHG1 sees the freshest fixture
        # universe. football-data.co.uk refreshes remain protected by FHG1's own
        # source-due throttle, so this does not spend Odds API credits or hammer
        # the external history source on every worker tick.
        result = original_cycle()
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
                print(f"FHG1_MAINT {state}", flush=True)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            try:
                db.record_collector_run("FHG1_MAINT", False, detail=detail)
            except Exception:
                pass
            print(f"FHG1_MAINT_ERROR {detail}", flush=True)
        return result

    worker.one_cycle = cycle_with_fhg1_maintenance
    worker._fhg1_worker_maint = True
    _INSTALLED = True
    return {"installed": True, "already_installed": False, "version": VERSION}
