from __future__ import annotations

"""Small process-startup guards for research-only runtime patches.

No betting authority is changed here. The TPRED rich-cache cap remains date
bounded, Challenge Export FHG health is reporting-only, and broad football
coverage remains forward/research-only until its own evidence earns promotion.
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

try:
    import challenge_fhg_health_integrity_patch as _challenge_fhg_health

    _challenge_fhg_state = _challenge_fhg_health.install()
    print(f"CHALLENGE_FHG_HEALTH_INTEGRITY_INSTALL {_challenge_fhg_state}", flush=True)
except Exception as _exc:
    print(f"CHALLENGE_FHG_HEALTH_INTEGRITY_ERROR {type(_exc).__name__}: {_exc}", flush=True)

try:
    import football_coverage_expansion_patch as _football_coverage

    _football_coverage_state = _football_coverage.install()
    print(f"FOOTBALL_COVERAGE_EXPANSION_BOOTSTRAP {_football_coverage_state}", flush=True)
except Exception as _exc:
    print(f"FOOTBALL_COVERAGE_EXPANSION_ERROR {type(_exc).__name__}: {_exc}", flush=True)
