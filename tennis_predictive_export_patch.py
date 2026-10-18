from __future__ import annotations

"""Ensure TPRED1/2/3 and rich-source research are included in exports."""

from typing import Any, Dict

import exporter
import split_exporter
from tennis_predictive import scoreboard
from tennis_predictive3 import scoreboard as scoreboard3
from tennis_rich_source import status as rich_source_status

VERSION = "TPRED_EXPORT_PATCH_V2"

TABLES = (
    # TPRED1/2 controls.
    "tennis_predictive_state",
    "tennis_predictive_feature_snapshots",
    "tennis_predictive_walkforward",
    "tennis_predictive_predictions",
    # Cached RapidAPI source and chronological context.
    "tennis_rich_api_ledger",
    "tennis_rich_players",
    "tennis_rich_rankings",
    "tennis_rich_matches",
    "tennis_rich_sync_state",
    "tennis_rich_fixtures",
    "tennis_rich_context_state",
    # TPRED3 challenger + refresh bookkeeping.
    "tennis_predictive3_state",
    "tennis_predictive3_walkforward",
    "tennis_predictive3_predictions",
    "tennis_predictive3_refresh_state",
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
                payload["tennis_predictive"] = {
                    "error": f"{type(exc).__name__}: {exc}",
                    "betting_authority": False,
                }
            try:
                payload["tennis_predictive3"] = scoreboard3(db)
            except Exception as exc:
                payload["tennis_predictive3"] = {
                    "error": f"{type(exc).__name__}: {exc}",
                    "betting_authority": False,
                }
            try:
                payload["tennis_rich_source"] = rich_source_status(db)
            except Exception as exc:
                payload["tennis_rich_source"] = {
                    "error": f"{type(exc).__name__}: {exc}",
                    "betting_authority": False,
                }
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
        "summary_sections": ["tennis_predictive", "tennis_predictive3", "tennis_rich_source"],
    }
