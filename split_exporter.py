from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, Mapping, Sequence

from db import sanitize_sensitive_text
from exporter import _json_safe, _rows_to_csv, _safe_settings, _table_exists
from canonical import canonical_scoreboard
from execution_shadow import execution_scoreboard, execution_funnel
from multiples_shadow import multiples_scoreboard
from manual_systems_shadow import manual_systems_scoreboard
from cohort_systems_shadow import cohort_systems_scoreboard
from tennis_shadow import tennis_scoreboard
from multisport_shadow import multisport_scoreboard
from multisport_lines_shadow import line_scoreboard
from predictive_football import predictive_scoreboard
from predictive_football_pred2 import predictive2_scoreboard
from predictive_football_pred3 import predictive3_scoreboard
from predictive_football_pred4 import predictive4_scoreboard
from outcome_edge import outcome_edge_report
from meta_edge import meta_edge_scoreboard
from meta_edge_model import meta_model_status
from proxy_xg import proxy_xg_status

# Phone-friendly research export: intentionally omits the very large raw price/
# odds/training histories. Lifetime scoreboards remain in analysis_summary.json.
WEEKLY_TABLES = (
    "events", "signals", "canonical_bets", "execution_shadow_bets",
    "multiple_shadow_bets", "multiple_shadow_legs",
    "manual_system_shadow_bets", "manual_system_shadow_legs", "manual_system_shadow_lines",
    "cohort_system_shadow_state", "cohort_system_shadow_bets",
    "cohort_system_shadow_legs", "cohort_system_shadow_lines",
    "cross_sport_system_state", "cross_sport_system_bets", "cross_sport_system_legs", "cross_sport_system_lines",
    "event_results", "research_reports",
    "tennis_execution_bets", "tennis_results",
    "multisport_execution_bets", "multisport_results",
    "multisport_line_bets",
    "football_predictive_predictions", "football_predictive_bets",
    "football_predictive_market_predictions", "football_predictive_market_bets",
    "football_predictive2_predictions", "football_predictive2_bets",
    "football_predictive2_market_predictions", "football_predictive2_market_bets",
    "football_predictive3_predictions", "football_predictive3_bets",
    "football_predictive3_market_predictions", "football_predictive3_market_bets",
    "football_predictive4_predictions", "football_predictive4_bets",
    "football_predictive4_market_predictions", "football_predictive4_market_bets",
    "outcome_edge_watch_cohorts", "meta_edge_samples", "meta_edge_model_runs",
    "meta_edge_model_scores", "football_predictive_historical_validations",
    "football_predictive_historical_bets",
)

MANUAL_TABLES = (
    "manual_system_shadow_bets", "manual_system_shadow_legs",
    "manual_system_shadow_lines", "cross_sport_system_state", "cross_sport_system_bets",
    "cross_sport_system_legs", "cross_sport_system_lines", "events", "event_results",
)

def _secrets(settings):
    return tuple(x for x in (
        str(getattr(settings, "odds_api_key", "") or ""),
        str(getattr(settings, "proxy_xg_api_football_key", "") or ""),
    ) if x)

def _summary(db, settings):
    return {
        "execution_scoreboard": execution_scoreboard(db),
        "canonical_scoreboard": canonical_scoreboard(db),
        "execution_funnel": execution_funnel(db),
        "multiples_shadow": multiples_scoreboard(db),
        "manual_systems_shadow": manual_systems_scoreboard(db),
        "cohort_systems_shadow": cohort_systems_scoreboard(db),
        "tennis_shadow": tennis_scoreboard(db),
        "multisport_shadow": multisport_scoreboard(db),
        "multisport_lines_shadow": line_scoreboard(db),
        "predictive_football": predictive_scoreboard(db),
        "predictive_football_pred2": predictive2_scoreboard(db),
        "predictive_football_pred3": predictive3_scoreboard(db),
        "predictive_football_pred4": predictive4_scoreboard(db),
        "outcome_edge": outcome_edge_report(db),
        "meta_edge": meta_edge_scoreboard(db, int(getattr(settings, "meta_edge_min_clean_labels", 200))),
        "meta_edge_model": meta_model_status(db),
        "proxy_xg": proxy_xg_status(db, settings),
    }

def _build(db, settings, version: str, tables, kind: str, include_summary: bool = True):
    generated = datetime.now(timezone.utc)
    stamp = generated.strftime("%Y%m%dT%H%M%SZ")
    filename = f"betting-lab-{kind}-{stamp}.zip"
    secrets = _secrets(settings)
    row_counts: Dict[str, int] = {}
    payload = BytesIO()

    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for table in tables:
            if not _table_exists(db, table):
                row_counts[table] = 0
                z.writestr(f"tables/{table}.csv", "")
                continue
            rows = db.fetchall(f"SELECT * FROM {table}")
            row_counts[table] = len(rows)
            # Convert/write/release one table at a time: avoids retaining every
            # CSV payload simultaneously as the legacy full exporter does.
            z.writestr(f"tables/{table}.csv", _rows_to_csv(rows, secrets=secrets))
            del rows

        manifest = {
            "app": "Project Exit Plan — Betting Lab",
            "version": version,
            "export_kind": kind,
            "generated_at_utc": generated.isoformat(),
            "contains_secrets": False,
            "row_counts": row_counts,
            "settings": _safe_settings(settings),
        }
        z.writestr("manifest.json", json.dumps(_json_safe(manifest), indent=2))
        if include_summary:
            z.writestr("analysis_summary.json", json.dumps(_json_safe(_summary(db, settings)), indent=2))
        z.writestr("README.txt",
            "Project Exit Plan — Betting Lab\n"
            f"Export: {kind}\nGenerated UTC: {generated.isoformat()}\n\n"
            + ("PHONE-FRIENDLY RESEARCH EXPORT\nThis intentionally excludes bulky raw odds/price/training histories. "
               "It retains research bets, predictions, results, systems/cohorts and lifetime scoreboards. "
               "Use /export/research.zip for occasional complete historical audits.\n"
               if kind == "research-weekly" else
               "MANUAL SYSTEMS EXPORT\nContains all manual-system cards, constituent legs/lines, events and results "
               "for deduplication and system-vs-singles analysis.\n")
        )
    return payload.getvalue(), filename

def build_weekly_research_export(db, settings, version: str):
    return _build(db, settings, version, WEEKLY_TABLES, "research-weekly", True)

def build_manual_systems_export(db, settings, version: str):
    return _build(db, settings, version, MANUAL_TABLES, "manual-systems", True)
