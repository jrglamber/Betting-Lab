from __future__ import annotations

"""Safely accelerate the research-only rich tennis cache fill.

The provider PRO plan allows 10 requests/second and the app-level daily guard is
configured separately. The original rich maintenance pass hard-capped detailed
match-stat work at 100 requests, which could prevent a 7,500/day allowance from
being used even when a large backlog remained. This patch:

* keeps all existing daily and per-cycle guards;
* raises only the detailed-stat batch to an env-configured ceiling;
* throttles provider calls below the PRO per-second limit;
* leaves all betting/execution authority unchanged (this source is research-only).
"""

from datetime import datetime
import os
import threading
import time
from typing import Any, Dict, Optional

import tennis_rich_source as rich

VERSION = "TPRED_RICH_THROUGHPUT_V1"
STATS_PER_CYCLE = max(1, int(os.getenv("TPRED_RICH_STATS_PER_CYCLE", "500")))
MAX_REQUESTS_PER_SECOND = max(0.5, float(os.getenv("TPRED_RICH_MAX_RPS", "8.0")))
MIN_REQUEST_INTERVAL = 1.0 / MAX_REQUESTS_PER_SECOND

_ORIGINAL_GET = rich.RichClient.get


def _rate_limited_get(self, path: str, *, params=None, purpose: str):
    lock = getattr(self, "_tpred_rate_lock", None)
    if lock is None:
        lock = threading.Lock()
        self._tpred_rate_lock = lock
        self._tpred_last_request_started = 0.0

    with lock:
        now = time.monotonic()
        last = float(getattr(self, "_tpred_last_request_started", 0.0) or 0.0)
        wait = MIN_REQUEST_INTERVAL - (now - last)
        if wait > 0:
            time.sleep(wait)
        self._tpred_last_request_started = time.monotonic()
    return _ORIGINAL_GET(self, path, params=params, purpose=purpose)


def _maintenance(db, now: Optional[datetime] = None, max_requests: int = rich.PER_CYCLE_CAP) -> Dict[str, Any]:
    rich.install_schema(db)
    now = now or rich._now()
    client = rich.RichClient(db, max_requests=max_requests)
    if not client.key:
        return {
            "ok": False,
            "version": rich.VERSION,
            "reason": "TENNIS_RAPIDAPI_KEY_missing",
            "requests": 0,
            "betting_authority": False,
        }
    if client.max_requests <= 0:
        return {
            "ok": True,
            "version": rich.VERSION,
            "reason": "daily_cap_reached",
            "requests": 0,
            "betting_authority": False,
        }

    state = db.fetchone("SELECT * FROM tennis_rich_sync_state WHERE singleton_id=1") or {}
    rankings: Dict[str, Any] = {}
    last_rank = str(state.get("last_rankings_at") or "")
    if not last_rank or last_rank[:10] != now.date().isoformat():
        for tour in ("atp", "wta"):
            rankings[tour] = rich.sync_current_rankings(db, client, tour, now)
            if not client.available:
                break
        if rankings:
            db.execute(
                "UPDATE tennis_rich_sync_state SET last_rankings_at=? WHERE singleton_id=1",
                (rich._iso(now),),
            )

    cursor, end = rich._initial_window(db, now)
    backfill = []
    while client.available and cursor <= end:
        chunk_end = min(end, cursor + rich.timedelta(days=rich.RESULT_CHUNK_DAYS - 1))
        item: Dict[str, Any] = {
            "start": cursor.isoformat(),
            "end": chunk_end.isoformat(),
            "tours": {},
        }
        for tour in ("atp", "wta"):
            if not client.available:
                break
            item["tours"][tour] = rich.sync_results_range(db, client, tour, cursor, chunk_end)
        backfill.append(item)
        cursor = chunk_end + rich.timedelta(days=1)
        db.execute(
            "UPDATE tennis_rich_sync_state SET history_cursor=? WHERE singleton_id=1",
            (cursor.isoformat(),),
        )
        if len(backfill) >= 2:
            break

    remaining_cycle = max(0, client.max_requests - client.requests)
    stats_budget = max(0, min(STATS_PER_CYCLE, remaining_cycle))
    stats = (
        rich.sync_missing_match_stats(db, client, limit=stats_budget)
        if stats_budget
        else {"fetched": 0, "nonempty": 0}
    )
    db.execute(
        "UPDATE tennis_rich_sync_state SET last_maintenance_at=? WHERE singleton_id=1",
        (rich._iso(now),),
    )

    counts = {
        "players": int((db.fetchone("SELECT COUNT(*) AS n FROM tennis_rich_players") or {}).get("n") or 0),
        "rankings": int((db.fetchone("SELECT COUNT(*) AS n FROM tennis_rich_rankings") or {}).get("n") or 0),
        "matches": int((db.fetchone("SELECT COUNT(*) AS n FROM tennis_rich_matches") or {}).get("n") or 0),
        "matches_with_stats": int((db.fetchone("SELECT COUNT(*) AS n FROM tennis_rich_matches WHERE stats_json IS NOT NULL") or {}).get("n") or 0),
    }
    return {
        "ok": True,
        "version": rich.VERSION,
        "requests": client.requests,
        "errors": client.errors,
        "daily_used": rich._used_today(db),
        "daily_cap": rich.DAILY_REQUEST_CAP,
        "rankings": rankings,
        "backfill_chunks": backfill,
        "stats": stats,
        "counts": counts,
        "throughput_patch": {
            "version": VERSION,
            "stats_per_cycle": STATS_PER_CYCLE,
            "max_requests_per_second": MAX_REQUESTS_PER_SECOND,
        },
        "betting_authority": False,
    }


def install() -> Dict[str, Any]:
    if getattr(rich, "_tpred_rich_throughput_installed", False):
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
        }

    rich.RichClient.get = _rate_limited_get
    rich.maintenance = _maintenance
    rich._tpred_rich_throughput_installed = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "daily_cap": int(rich.DAILY_REQUEST_CAP),
        "per_cycle_cap": int(rich.PER_CYCLE_CAP),
        "stats_per_cycle": STATS_PER_CYCLE,
        "max_requests_per_second": MAX_REQUESTS_PER_SECOND,
        "betting_authority": False,
    }


INSTALL_RESULT = install()
print(f"TPRED_RICH_THROUGHPUT_PATCH {INSTALL_RESULT}", flush=True)
