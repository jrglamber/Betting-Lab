from __future__ import annotations

"""Attach TPRED maintenance to the existing tennis shadow cycle."""

from typing import Any, Dict

from tennis_predictive import run_maintenance

VERSION = "TPRED_RUNTIME_PATCH_V1"


def install(tennis_engine, db) -> Dict[str, Any]:
    if tennis_engine is None:
        return {"installed": False, "version": VERSION, "reason": "tennis_engine_missing"}
    if getattr(tennis_engine, "_tpred_runtime_installed", False):
        return {"installed": True, "already_installed": True, "version": VERSION}

    original = tennis_engine.one_cycle

    def wrapped_one_cycle(*args, **kwargs):
        result = original(*args, **kwargs)
        try:
            tpred = run_maintenance(db)
        except Exception as exc:
            tpred = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "betting_authority": False}
        if isinstance(result, dict):
            result = dict(result)
            result["tpred"] = tpred
        return result

    tennis_engine.one_cycle = wrapped_one_cycle
    tennis_engine._tpred_runtime_installed = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "trigger": "after_tennis_shadow_cycle",
        "provider_credits": 0,
        "betting_authority": False,
    }
