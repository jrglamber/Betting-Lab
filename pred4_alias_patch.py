from __future__ import annotations

"""Mechanical PRED4 aliases plus small runtime/dashboard installs."""

import web as web_module
from predictive_football_pred4 import ALIAS_NORMALIZATION
from multisport_odds_band_patch import install as install_multisport_odds_band_patch
from live_yankee_feed import install as install_live_yankee_feed
from live_yankee_fast_loop import install as install_live_yankee_fast_loop
from live_yankee_consolidation_patch import install as install_live_yankee_consolidation
from betting_lab_research_clarity_patch import install as install_research_clarity
from live_yankee_source_focus_patch import install as install_live_yankee_source_focus
from api_sports_walkforward_dashboard_patch import install as install_walkforward_dashboard
from live_yankee_readiness_patch import install as install_live_yankee_readiness

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
    research_clarity = install_research_clarity()
    live_yankee_source_focus = install_live_yankee_source_focus()
    walkforward_dashboard = install_walkforward_dashboard()
    live_yankee_readiness = install_live_yankee_readiness()
    web_module.VERSION = "0.19.44"
    return {
        "aliases_applied": len(CURRENT_ALIASES),
        "changed": sum(1 for key, value in CURRENT_ALIASES.items() if before.get(key) != value),
        "aliases": dict(CURRENT_ALIASES),
        "multisport_odds_bands": multisport_dashboard,
        "live_yankee_feed": live_yankee,
        "live_yankee_fast_loop": live_yankee_fast_loop,
        "live_yankee_consolidation": live_yankee_consolidation,
        "research_clarity": research_clarity,
        "live_yankee_source_focus": live_yankee_source_focus,
        "walkforward_dashboard": walkforward_dashboard,
        "live_yankee_readiness": live_yankee_readiness,
        "dashboard_version": web_module.VERSION,
    }
