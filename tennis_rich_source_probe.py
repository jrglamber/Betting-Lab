from __future__ import annotations

"""Bounded connectivity probe for the optional RapidAPI tennis rich-data source.

This is diagnostics only. It never changes TPRED forecasts, betting authority,
or stored research data. The probe makes at most one small fixtures request per
dataset (ATP/WTA) and never logs the API key or response bodies.
"""

import os
from typing import Any, Dict

import requests

VERSION = "TENNIS_RICH_SOURCE_PROBE_V1"
HOST = "tennis-api-atp-wta-itf.p.rapidapi.com"
BASE_URL = f"https://{HOST}"


def _count_rows(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            for key in ("rows", "fixtures", "matches", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    return len(value)
        for key in ("rows", "fixtures", "matches", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
    return 0


def probe() -> Dict[str, Any]:
    key = os.getenv("TENNIS_RAPIDAPI_KEY", "").strip()
    if not key:
        return {
            "ok": False,
            "version": VERSION,
            "reason": "TENNIS_RAPIDAPI_KEY_missing",
            "requests": 0,
        }

    headers = {
        "X-RapidAPI-Key": key,
        "X-RapidAPI-Host": HOST,
    }
    datasets: Dict[str, Any] = {}
    all_ok = True
    for dataset in ("atp", "wta"):
        url = f"{BASE_URL}/tennis/v2/{dataset}/fixtures"
        try:
            response = requests.get(
                url,
                headers=headers,
                params={"pageNo": 1, "pageSize": 3, "filter": "PlayerGroup:singles"},
                timeout=12,
            )
            status = int(response.status_code)
            row_count = 0
            content_type = str(response.headers.get("content-type") or "")
            if 200 <= status < 300:
                try:
                    row_count = _count_rows(response.json())
                except Exception:
                    row_count = 0
            else:
                all_ok = False
            datasets[dataset] = {
                "ok": 200 <= status < 300,
                "status": status,
                "rows_seen": row_count,
                "json_response": "json" in content_type.lower(),
            }
        except Exception as exc:
            all_ok = False
            datasets[dataset] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    return {
        "ok": all_ok,
        "version": VERSION,
        "host": HOST,
        "requests": 2,
        "datasets": datasets,
        "betting_authority": False,
        "data_written": False,
    }
