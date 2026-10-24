from __future__ import annotations

"""Compatibility entrypoint for Live Yankee price-tolerance research.

Historical production bootstraps import ``install`` from this module. Route that
call through the deterministic Live Yankee runtime so source-focus, settlement,
same-book authority and the fast loop are always installed in the correct order
before the clean V2 tolerance experiment is used.

The V1 tolerance tables remain archived and excluded from V2 evidence.
"""

from live_yankee_price_tolerance_v2_patch import (
    VERSION,
    VARIANTS,
    run_research_maintenance,
    scoreboard,
)
from live_yankee_runtime_patch import install

__all__ = [
    "VERSION",
    "VARIANTS",
    "install",
    "run_research_maintenance",
    "scoreboard",
]
