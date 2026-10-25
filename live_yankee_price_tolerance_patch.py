from __future__ import annotations

"""Compatibility entrypoint for Live Yankee price-tolerance research.

Historical production bootstraps import ``install`` from this module. Route that
call through the self-healing V2 runtime guard so source-focus, settlement,
same-book authority, the fast loop, candidate seeding and V2 health monitoring
are installed in the correct order before the clean tolerance experiment runs.

The V1 tolerance tables remain archived and excluded from V2 evidence.
"""

from live_yankee_price_tolerance_v2_patch import (
    VERSION,
    VARIANTS,
    run_research_maintenance,
    scoreboard,
)
from live_yankee_v2_runtime_fix import install

__all__ = [
    "VERSION",
    "VARIANTS",
    "install",
    "run_research_maintenance",
    "scoreboard",
]
