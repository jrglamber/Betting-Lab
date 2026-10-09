from __future__ import annotations

"""One-shot diagnostic for API-Sports current-season access.

Research-only. Uses the existing API key and known league ids, makes a few
read-only requests, and prints only sanitized results. No database mutation.
"""

import json
import os

from api_sports_predictive import ApiSportsClient, LANES, sanitize_sensitive_text


def run():
    key = os.getenv("API_FOOTBALL_KEY", "").strip()
    client = ApiSportsClient(key)
    out = {}
    known_ids = {"NFL": 1, "EUROLEAGUE": 120}
    for lane in LANES:
        attempts = []
        for season in ("2026", "2025", "2024"):
            try:
                rows = client.get(
                    lane,
                    "games",
                    {"league": known_ids[lane.key], "season": season, "timezone": "UTC"},
                )
                attempts.append({
                    "season": season,
                    "ok": True,
                    "rows": len(rows),
                    "remaining": client.remaining.get(lane.key),
                })
            except Exception as exc:
                attempts.append({
                    "season": season,
                    "ok": False,
                    "error": sanitize_sensitive_text(str(exc), (key,)),
                    "remaining": client.remaining.get(lane.key),
                })
        out[lane.key] = attempts
    print("API_SPORTS_SEASON_PROBE " + json.dumps(out, sort_keys=True), flush=True)
    return out


if __name__ == "__main__":
    run()
