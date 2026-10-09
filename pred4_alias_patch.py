from __future__ import annotations

"""Mechanical PRED4 team-name mapping repairs.

These aliases reconcile current Odds-API fixture names with names already
present in PXG1's stored API-Football history. They do not change thresholds,
model coefficients, probabilities, or betting authority.
"""

from predictive_football_pred4 import ALIAS_NORMALIZATION

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
    return {
        "aliases_applied": len(CURRENT_ALIASES),
        "changed": sum(1 for key, value in CURRENT_ALIASES.items() if before.get(key) != value),
        "aliases": dict(CURRENT_ALIASES),
    }
