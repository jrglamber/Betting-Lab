from __future__ import annotations

"""Tiny startup guard for the one-day TPRED rich-cache completion boost.

Research-only: 55k requests on 2026-10-21 UTC, automatically 30k thereafter.
Existing per-cycle and requests/second throttles remain unchanged.
"""

from datetime import datetime, timezone
import os

try:
    import tennis_rich_source as _rich

    _original_rich_init = _rich.RichClient.__init__

    def _tpred_daily_cap() -> int:
        return 55_000 if datetime.now(timezone.utc).date().isoformat() == "2026-10-21" else 30_000

    def _apply_tpred_daily_cap() -> int:
        cap = _tpred_daily_cap()
        _rich.DAILY_REQUEST_CAP = cap
        os.environ["TPRED_RICH_DAILY_CAP"] = str(cap)
        return cap

    def _rich_init_with_date_cap(self, db, max_requests: int):
        _apply_tpred_daily_cap()
        return _original_rich_init(self, db, max_requests)

    _rich.RichClient.__init__ = _rich_init_with_date_cap
    _effective = _apply_tpred_daily_cap()
    print(
        f"TPRED_RICH_DATE_CAP effective={_effective} auto_revert_utc=2026-10-22T00:00:00Z",
        flush=True,
    )
except Exception as _exc:
    print(f"TPRED_RICH_DATE_CAP_ERROR {type(_exc).__name__}: {_exc}", flush=True)
