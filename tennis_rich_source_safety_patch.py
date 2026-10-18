from __future__ import annotations

"""Safety guards for TPRED3 rich-source collection.

This patch is deliberately plumbing-only. It does not alter TPRED3 features,
weights, forecasts or betting authority. It prevents two avoidable sources of
provider waste:

1. Count each attempted HTTP request exactly once, including invalid JSON.
2. Cache a genuine 404 from a per-match stats endpoint as a terminal
   "no stats available" result, while leaving transient/network/429/5xx errors
   retryable on a later maintenance pass.
"""

import json
from typing import Any, Dict, Mapping, Optional

import tennis_rich_source as source

VERSION = "TPRED_RICH_SOURCE_SAFETY_V1"
TERMINAL_NO_STATS = "NO_STATS_AVAILABLE"


def _safe_get(self, path: str, *, params: Optional[Mapping[str, Any]] = None, purpose: str) -> Optional[Any]:
    if not self.available:
        return None

    endpoint = path if path.startswith("/") else "/" + path
    status = None
    ok = False
    remaining = None
    error = None
    payload = None

    # Diagnostics for the caller. These are reset on every attempted request.
    self.last_status_code = None
    self.last_error = None

    # Increment once per attempted network request, regardless of how parsing
    # or response handling subsequently ends.
    self.requests += 1
    try:
        response = source.requests.get(
            source.BASE_URL + endpoint,
            headers={"X-RapidAPI-Key": self.key, "X-RapidAPI-Host": source.HOST},
            params=dict(params or {}),
            timeout=18,
        )
        status = int(response.status_code)
        self.last_status_code = status
        remaining = source._as_int(response.headers.get("x-ratelimit-requests-remaining"))
        ok = 200 <= status < 300
        if ok:
            # 204/empty bodies are successful terminal responses, not JSON
            # parsing failures and not reasons to consume another request.
            if status == 204 or not getattr(response, "content", b""):
                payload = {}
            else:
                try:
                    payload = response.json()
                except Exception as exc:
                    self.errors += 1
                    error = f"{type(exc).__name__}: {exc}"
        else:
            self.errors += 1
            error = f"HTTP {status}"
    except Exception as exc:
        self.errors += 1
        error = f"{type(exc).__name__}: {exc}"

    self.last_error = error
    self.db.execute(
        """
        INSERT INTO tennis_rich_api_ledger(
            requested_at,purpose,endpoint,status_code,ok,provider_remaining,error
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (source._iso(source._now()), purpose, endpoint, status, int(ok), remaining, error),
    )
    return payload


def _safe_sync_missing_match_stats(db, client, limit: int = 100) -> Dict[str, int]:
    rows = db.fetchall(
        """
        SELECT tour,provider_match_id FROM tennis_rich_matches
        WHERE stats_json IS NULL AND COALESCE(result_type,'completed') NOT IN ('walkover','w/o')
        ORDER BY match_date DESC,provider_match_id DESC LIMIT ?
        """,
        (int(limit),),
    )
    fetched = 0
    nonempty = 0
    terminal_missing = 0
    for row in rows:
        if not client.available:
            break
        tour = str(row.get("tour") or "").lower()
        mid = source._as_int(row.get("provider_match_id"))
        if tour not in {"atp", "wta"} or mid is None:
            continue

        payload = client.get(
            f"/tennis/v2/matches/{mid}/stats",
            params={"type": tour, "source": "game"},
            purpose=f"match_stats_{tour}",
        )
        fetched += 1

        if payload is None:
            # A 404 from this specific immutable historical stats resource is a
            # genuine terminal absence. Cache it so the same match is not paid
            # for again every maintenance cycle. Other failures remain NULL and
            # therefore retryable.
            if int(getattr(client, "last_status_code", 0) or 0) == 404:
                marker = json.dumps(
                    {"_terminal": TERMINAL_NO_STATS, "status": 404},
                    separators=(",", ":"),
                )
                db.execute(
                    """
                    UPDATE tennis_rich_matches SET stats_json=?,stats_fetched_at=?
                    WHERE tour=? AND provider_match_id=?
                    """,
                    (marker, source._iso(source._now()), tour, mid),
                )
                terminal_missing += 1
            continue

        compact = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        has_data = bool(payload) and payload != {"data": {}}
        nonempty += int(has_data)
        db.execute(
            """
            UPDATE tennis_rich_matches SET stats_json=?,stats_fetched_at=?
            WHERE tour=? AND provider_match_id=?
            """,
            (compact, source._iso(source._now()), tour, mid),
        )

    return {
        "fetched": fetched,
        "nonempty": nonempty,
        "terminal_missing": terminal_missing,
    }


def install() -> Dict[str, Any]:
    if getattr(source, "_tpred_rich_source_safety_installed", False):
        return {"installed": True, "already_installed": True, "version": VERSION}

    source.RichClient.get = _safe_get
    source.sync_missing_match_stats = _safe_sync_missing_match_stats
    source._tpred_rich_source_safety_installed = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "exact_request_counting": True,
        "terminal_404_cached": True,
        "transient_errors_retryable": True,
        "model_changed": False,
        "betting_authority": False,
    }
