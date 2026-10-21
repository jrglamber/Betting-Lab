from __future__ import annotations

"""One-day TPRED rich-cache completion boost for 21 Oct 2026.

The rich tennis cache is ~80% complete and has reached the normal 30k/day app
safety cap.  Give the research-only cache another 25k requests today so the
remaining historical match stats can finish, while preserving all existing
per-cycle and requests/second throttles.  The effective cap automatically
returns to 30k from 22 Oct UTC onward, even without a redeploy.

No predictive, selection, staking, settlement or betting-authority logic changes.
"""

from datetime import datetime, timezone
from typing import Any, Dict

import tennis_rich_source as rich

VERSION = "TPRED_RICH_OCT21_FINISH_V1"
BOOST_DATE_UTC = "2026-10-21"
NORMAL_DAILY_CAP = 30_000
BOOST_DAILY_CAP = 55_000

_ORIGINAL_INIT = rich.RichClient.__init__
_INSTALLED = False


def _cap_for_now() -> int:
    return BOOST_DAILY_CAP if datetime.now(timezone.utc).date().isoformat() == BOOST_DATE_UTC else NORMAL_DAILY_CAP


def install() -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    def init_with_daily_cap(self, db, max_requests: int):
        # RichClient reads rich.DAILY_REQUEST_CAP inside its original constructor.
        # Refresh the module value on every client creation so the cap reverts
        # automatically when the UTC date changes without requiring a redeploy.
        rich.DAILY_REQUEST_CAP = _cap_for_now()
        return _ORIGINAL_INIT(self, db, max_requests)

    rich.RichClient.__init__ = init_with_daily_cap
    rich.DAILY_REQUEST_CAP = _cap_for_now()
    _INSTALLED = True

    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "boost_date_utc": BOOST_DATE_UTC,
        "effective_daily_cap": rich.DAILY_REQUEST_CAP,
        "normal_daily_cap": NORMAL_DAILY_CAP,
        "boost_daily_cap": BOOST_DAILY_CAP,
        "automatic_revert": "2026-10-22T00:00:00Z",
        "per_cycle_cap_changed": False,
        "rate_limit_changed": False,
        "betting_authority_changed": False,
    }


INSTALL_RESULT = install()
print(f"TPRED_RICH_OCT21_FINISH_PATCH {INSTALL_RESULT}", flush=True)
