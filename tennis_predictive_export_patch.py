from __future__ import annotations

"""Ensure TPRED outputs are included in both research exports."""

from typing import Any, Dict

import exporter
import split_exporter
from tennis_predictive import scoreboard

VERSION = "TPRED_EXPORT_PATCH_V1"

TABLES = (
    "tennis_predictive_state",
    "tennis_predictive_feature_snapshots",
    "tennis_predictive_walkforward",
    "tennis_predictive_predictions",
)

_INSTALLED = False


def install() -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    exporter.EXPORT_TABLES = tuple(dict.fromkeys(tuple(exporter.EXPORT_TABLES) + TABLES))
    split_exporter.WEEKLY_TABLES = tuple(dict.fromkeys(tuple(split_exporter.WEEKLY_TABLES) + TABLES))

    original_summary = split_exporter._summary
    if not getattr(original_summary, "_tpred_wrapped", False):
        def wrapped_summary(db, settings):
            payload = dict(original_summary(db, settings))
            try:
                payload["tennis_predictive"] = scoreboard(db)
            except Exception as exc:
                payload["tennis_predictive"] = {"error": f"{type(exc).__name__}: {exc}", "betting_authority": False}
            return payload
        wrapped_summary._tpred_wrapped = True
        split_exporter._summary = wrapped_summary

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "full_export_tables": list(TABLES),
        "weekly_export_tables": list(TABLES),
    }
