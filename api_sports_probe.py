from __future__ import annotations

import json
import os
from typing import Any, Dict

import requests

API_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
TARGETS = {
    "NFL": "https://v1.american-football.api-sports.io/status",
    "BASKETBALL": "https://v1.basketball.api-sports.io/status",
}


def probe(name: str, url: str) -> Dict[str, Any]:
    if not API_KEY:
        return {"ok": False, "reason": "API_FOOTBALL_KEY_NOT_SET"}
    try:
        response = requests.get(
            url,
            timeout=20,
            headers={
                "x-apisports-key": API_KEY,
                "User-Agent": "Project-Exit-Plan-Betting-Lab/api-sports-probe",
            },
        )
        payload = response.json() if response.content else {}
        errors = payload.get("errors") if isinstance(payload, dict) else None
        account = payload.get("response") if isinstance(payload, dict) else None
        return {
            "ok": bool(response.ok and not errors),
            "http_status": response.status_code,
            "errors": errors or None,
            "response": account,
            "remaining": response.headers.get("x-ratelimit-requests-remaining"),
            "limit": response.headers.get("x-ratelimit-requests-limit"),
        }
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    results = {name: probe(name, url) for name, url in TARGETS.items()}
    print("API_SPORTS_CONNECTIVITY " + json.dumps(results, sort_keys=True), flush=True)
    return 0 if all(item.get("ok") for item in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
