from __future__ import annotations

"""One-day HR quote-budget boost for 19 Oct 2026.

The user approved 500 additional provider credits today to seed fresh quotes for
same-book price-tolerance forward testing. HR had already consumed 750 credits
before the normal cap was reduced to 500, so today's effective ceiling is 1,250.
From 00:00 UTC on 20 Oct the normal 500-credit daily ceiling is restored
automatically. The price-tolerance challengers themselves still make zero
provider calls; they only reuse HR quotes.
"""

from datetime import datetime, timezone
from typing import Any, Dict

import hit_rate_multiples_lab as hr

VERSION = "HR_ONE_DAY_CREDIT_BOOST_2026_10_19_V1"
BOOST_DATE_UTC = "2026-10-19"
NORMAL_DAILY_CAP = 500
BOOST_DAILY_CAP = 1250

_INSTALLED = False
_ORIGINAL_REFRESH = None


def _effective_cap(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return BOOST_DAILY_CAP if now.date().isoformat() == BOOST_DATE_UTC else NORMAL_DAILY_CAP


def install() -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_REFRESH
    if _INSTALLED:
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
            "effective_daily_cap": _effective_cap(),
        }

    _ORIGINAL_REFRESH = hr._refresh

    def refresh_with_one_day_cap(db, collector, settings, now):
        hr.DAILY_REFRESH_CREDIT_BUDGET = _effective_cap(now)
        return _ORIGINAL_REFRESH(db, collector, settings, now)

    hr._refresh = refresh_with_one_day_cap
    hr.DAILY_REFRESH_CREDIT_BUDGET = _effective_cap()
    _INSTALLED = True

    result = {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "boost_date_utc": BOOST_DATE_UTC,
        "normal_daily_cap": NORMAL_DAILY_CAP,
        "boost_daily_cap": BOOST_DAILY_CAP,
        "effective_daily_cap": _effective_cap(),
        "automatic_revert": "2026-10-20T00:00:00Z",
        "price_tolerance_provider_calls_added": 0,
    }
    print(f"HR_ONE_DAY_CREDIT_BOOST_INSTALL {result}", flush=True)
    return result


install()
