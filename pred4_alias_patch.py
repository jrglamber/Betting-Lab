from __future__ import annotations

"""Mechanical PRED4 aliases plus small runtime/dashboard installs."""

import web as web_module
from predictive_football_pred4 import ALIAS_NORMALIZATION
from multisport_odds_band_patch import install as install_multisport_odds_band_patch
from live_yankee_feed import install as install_live_yankee_feed
from live_yankee_fast_loop import install as install_live_yankee_fast_loop
from live_yankee_consolidation_patch import install as install_live_yankee_consolidation

CURRENT_ALIASES = {
    "stade lavallois": "laval",
    "lommel sk": "lommel united",
    "sjk seinajoki": "sjk",
    "west ham united": "west ham",
    "queens park rangers": "qpr",
}


def apply() -> dict:
    before = dict(ALIAS_NORMALIZATION)
    ALIAS_NORMALIZATION.update(CURRENT_ALIASES)
    multisport_dashboard = install_multisport_odds_band_patch()
    live_yankee = install_live_yankee_feed()
    live_yankee_fast_loop = install_live_yankee_fast_loop()
    live_yankee_consolidation = install_live_yankee_consolidation()
    web_module.VERSION = "0.19.40"
    return {
        "aliases_applied": len(CURRENT_ALIASES),
        "changed": sum(1 for key, value in CURRENT_ALIASES.items() if before.get(key) != value),
        "aliases": dict(CURRENT_ALIASES),
        "multisport_odds_bands": multisport_dashboard,
        "live_yankee_feed": live_yankee,
        "live_yankee_fast_loop": live_yankee_fast_loop,
        "live_yankee_consolidation": live_yankee_consolidation,
        "dashboard_version": web_module.VERSION,
    }
