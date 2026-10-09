from __future__ import annotations

"""One-shot schema probe for API-Sports historical game payloads.

Diagnostic only. Prints a compact, non-secret structural sample so the parser can
support the actual NFL and EuroLeague response shapes without guessing.
"""

import json
import os
import requests

KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
HEADERS = {"x-apisports-key": KEY, "User-Agent": "Project-Exit-Plan-Betting-Lab/schema-probe"}

TARGETS = {
    "NFL": ("https://v1.american-football.api-sports.io", 1, "2024"),
    "EUROLEAGUE": ("https://v1.basketball.api-sports.io", 120, "2024"),
}


def compact(value, depth=0):
    if depth >= 3:
        if isinstance(value, dict):
            return {k: type(v).__name__ for k, v in list(value.items())[:12]}
        if isinstance(value, list):
            return f"list[{len(value)}]"
        return value
    if isinstance(value, dict):
        return {str(k): compact(v, depth + 1) for k, v in list(value.items())[:20]}
    if isinstance(value, list):
        return [compact(value[0], depth + 1)] if value else []
    return value


out = {}
for name, (base, league, season) in TARGETS.items():
    try:
        r = requests.get(f"{base}/games", params={"league": league, "season": season, "timezone": "UTC"}, headers=HEADERS, timeout=25)
        payload = r.json() if r.content else {}
        rows = payload.get("response") or [] if isinstance(payload, dict) else []
        out[name] = {
            "status": r.status_code,
            "errors": payload.get("errors") if isinstance(payload, dict) else None,
            "rows": len(rows) if isinstance(rows, list) else None,
            "sample": compact(rows[0]) if isinstance(rows, list) and rows else None,
        }
    except Exception as exc:
        out[name] = {"error": f"{type(exc).__name__}: {exc}"}

print("API_SPORTS_SHAPE_PROBE " + json.dumps(out, sort_keys=True, default=str), flush=True)
