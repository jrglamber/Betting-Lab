from __future__ import annotations

"""Fix curated dashboard API-Sports health count.

The original homepage query used a literal SQL LIKE '2026%' while the database
wrapper always supplies a parameter tuple to psycopg2. A bare percent sign in a
Postgres query with parameters is interpreted as formatting syntax, so the query
raised internally and the broad dashboard fallback reported 0 games.

Use the already-audited API-Sports paid-status snapshot instead. This is read-only
presentation/observability logic: no provider calls, selection, staking,
settlement or execution behaviour is changed.
"""

from typing import Any, Dict

import dashboard_layout_v2 as layout
import api_sports_paid_status_patch as paid_status

VERSION = "DASHBOARD_API_SPORTS_HEALTH_V1"
_INSTALLED = False
_ORIGINAL_SNAPSHOT = None


def _current_counts() -> Dict[str, int]:
    status = paid_status.snapshot()
    counts: Dict[str, int] = {}
    for lane in ("NFL", "NCAAF"):
        lane_status = dict((status.get("lanes") or {}).get(lane) or {})
        active_season = str(lane_status.get("season") or "")
        total = 0
        for row in lane_status.get("season_status_counts") or []:
            if str((row or {}).get("season") or "") == active_season:
                total += int((row or {}).get("n") or 0)
        counts[lane] = total
    return counts


def install() -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_SNAPSHOT
    if _INSTALLED:
        counts = _current_counts()
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
            "counts": counts,
            "total": sum(counts.values()),
        }

    _ORIGINAL_SNAPSHOT = layout._snapshot

    def snapshot_with_api_sports_health(db):
        snapshot = dict(_ORIGINAL_SNAPSHOT(db))
        try:
            counts = _current_counts()
            snapshot["api_current_by_lane"] = counts
            snapshot["api_current"] = sum(counts.values())
        except Exception as exc:
            # Preserve the original snapshot value if the richer status read ever
            # fails; unlike the previous implementation, do not overwrite a good
            # value with a fabricated zero.
            snapshot["api_health_error"] = f"{type(exc).__name__}: {exc}"
        return snapshot

    layout._snapshot = snapshot_with_api_sports_health
    layout._CACHE["at"] = None
    layout._CACHE["snapshot"] = None
    _INSTALLED = True

    counts = _current_counts()
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "counts": counts,
        "total": sum(counts.values()),
    }
