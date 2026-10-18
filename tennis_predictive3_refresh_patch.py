from __future__ import annotations

"""Refresh guard for TPRED3 as cached match statistics fill in.

The base TPRED3 rebuild trigger keys off rich-match and dated-ranking counts.
During the paid historical backfill, serve/return statistics can arrive later
for matches that are already present, leaving historical walk-forward metrics
stale. This patch adds stats-count awareness without changing model features,
weights, thresholds, betting authority or provider polling.
"""

import json
from typing import Any, Dict

import tennis_predictive3 as tp3

VERSION = "TPRED3_STATS_REFRESH_V1"


def _stats_count(db) -> int:
    try:
        return int((db.fetchone(
            "SELECT COUNT(*) AS n FROM tennis_rich_matches WHERE stats_json IS NOT NULL"
        ) or {}).get("n") or 0)
    except Exception:
        return 0


def _ensure_schema(db) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS tennis_predictive3_refresh_state (
            singleton_id INTEGER PRIMARY KEY,
            last_stats_count INTEGER NOT NULL DEFAULT 0,
            last_refresh_at TEXT,
            version TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        INSERT INTO tennis_predictive3_refresh_state(singleton_id,last_stats_count,version)
        VALUES(1,0,?)
        ON CONFLICT(singleton_id) DO UPDATE SET version=excluded.version
        """,
        (VERSION,),
    )


def install(db) -> Dict[str, Any]:
    _ensure_schema(db)
    if getattr(tp3, "_stats_refresh_patch_installed", False):
        return {"installed": True, "already_installed": True, "version": VERSION}

    original = tp3.run_maintenance

    def wrapped_run_maintenance(runtime_db, now=None):
        _ensure_schema(runtime_db)
        prior = runtime_db.fetchone(
            "SELECT * FROM tennis_predictive3_refresh_state WHERE singleton_id=1"
        ) or {}
        prior_stats = int(prior.get("last_stats_count") or 0)
        current_stats = _stats_count(runtime_db)

        result = original(runtime_db, now)
        result = dict(result or {})
        rebuilt = bool(result.get("walkforward_rebuilt"))
        rich_count = int((runtime_db.fetchone(
            "SELECT COUNT(*) AS n FROM tennis_rich_matches"
        ) or {}).get("n") or 0)

        stats_refresh = False
        # If match/ranking changes already caused a rebuild, that rebuild also
        # consumed the newly cached stats. Otherwise refresh explicitly when the
        # terminal stats cache has grown.
        if (
            rich_count >= int(tp3.MIN_RICH_MATCHES_TO_BUILD)
            and current_stats != prior_stats
            and not rebuilt
        ):
            walk = tp3.rebuild_walkforward(runtime_db)
            refreshed_at = tp3._iso(now or tp3._now())
            weights = dict(walk.get("weights") or {})
            runtime_db.execute(
                """
                UPDATE tennis_predictive3_state
                SET last_walkforward_at=?,model_weights_json=?
                WHERE singleton_id=1
                """,
                (refreshed_at,json.dumps(weights,separators=(",",":"))),
            )
            result["walkforward_rebuilt_for_stats"] = True
            result["walkforward"] = {k:v for k,v in walk.items() if k != "state"}
            stats_refresh = True

        runtime_db.execute(
            """
            UPDATE tennis_predictive3_refresh_state
            SET last_stats_count=?,last_refresh_at=?,version=?
            WHERE singleton_id=1
            """,
            (current_stats,tp3._iso(now or tp3._now()),VERSION),
        )
        result["rich_stats_count"] = current_stats
        result["stats_refresh_triggered"] = stats_refresh
        return result

    tp3.run_maintenance = wrapped_run_maintenance
    tp3._stats_refresh_patch_installed = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "trigger": "rich_stats_count_change_without_match_or_ranking_change",
        "model_changed": False,
        "provider_calls_added": 0,
        "betting_authority": False,
    }
