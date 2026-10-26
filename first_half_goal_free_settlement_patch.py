from __future__ import annotations

"""Compatibility entrypoint for current FHG1 free/timely settlement.

Install the canonical/free V2 chain first, then the conservative ESPN tertiary
fallback. Both are settlement-only and carry no betting authority.
"""

from first_half_goal_free_settlement_v2_patch import VERSION as FREE_VERSION, install as install_free
from first_half_goal_espn_settlement_patch import VERSION as ESPN_VERSION, install as install_espn

VERSION = f"{FREE_VERSION}+{ESPN_VERSION}"


def install(db):
    free = install_free(db)
    espn = install_espn(db)
    return {
        "installed": bool(free.get("installed")) and bool(espn.get("installed")),
        "version": VERSION,
        "free": free,
        "espn": espn,
        "betting_authority": False,
    }


__all__ = ["VERSION", "install"]
