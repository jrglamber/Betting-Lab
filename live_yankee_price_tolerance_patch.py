from __future__ import annotations

"""Compatibility entrypoint for Live Yankee price-tolerance research.

The original V1 implementation has been retired because it did not preserve all
of the later source-pocket price-band guards and its local result resolver did
not support every source table used by the focused Live Yankee feed. Existing V1
DB tables are intentionally left untouched as archived evidence.

New deployments import this historical module name from pooled_dashboard_entrypoint,
so re-export the clean forward-only V2 implementation without changing the wider
bootstrap contract.
"""

from live_yankee_price_tolerance_v2_patch import (
    VERSION,
    VARIANTS,
    install,
    run_research_maintenance,
    scoreboard,
)

__all__ = [
    "VERSION",
    "VARIANTS",
    "install",
    "run_research_maintenance",
    "scoreboard",
]
