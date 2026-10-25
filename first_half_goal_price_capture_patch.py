from __future__ import annotations

"""Compatibility entrypoint for robust forward-only FHG1 price capture V2."""

from first_half_goal_price_capture_v2_patch import (
    VERSION,
    MARKET_KEYS,
    capture_entry_prices,
    install,
)

__all__ = ["VERSION", "MARKET_KEYS", "capture_entry_prices", "install"]
