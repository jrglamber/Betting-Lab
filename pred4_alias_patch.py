from __future__ import annotations

"""Mechanical PRED4 team-name mapping repairs plus small dashboard installs.

The aliases reconcile current Odds-API fixture names with names already present
in PXG1's stored API-Football history. Dashboard installs are presentation-only.
None of this changes thresholds, model coefficients, probabilities, collection
rules or betting authority.
"""

import web as web_module
from predictive_football_pred4 import ALIAS_NORMALIZATION
from multisport_odds_band_patch import install as install_multisport_odds_band_patch

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
    web_module.VERSION = "0.19.37"
    return {
        "aliases_applied": len(CURRENT_ALIASES),
        "changed": sum(1 for key, value in CURRENT_ALIASES.items() if before.get(key) != value),
        "aliases": dict(CURRENT_ALIASES),
        "multisport_odds_bands": multisport_dashboard,
        "dashboard_version": web_module.VERSION,
    }
