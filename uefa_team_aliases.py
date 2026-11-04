from __future__ import annotations

"""Conservative shared UEFA-to-domestic team-name resolution.

Only apply explicit, recognizable club-name aliases if the destination is
actually present in the relevant stored training/history set. No guessed clubs
or cross-country string-similarity joins.
"""

from difflib import SequenceMatcher
from typing import Optional

from predictive_football import _normalize_text

VERSION = "UEFA_TEAM_NAME_BRIDGE_V1"

# Values are football-data-style alternatives, not synthetic history.
ALTERNATIVES = {
    "borussia dortmund": ("dortmund",),
    "real betis": ("betis",),
    "rc lens": ("lens",),
    "bayer leverkusen": ("leverkusen",),
    "tsg hoffenheim": ("hoffenheim",),
    "brighton and hove albion": ("brighton",),
    "atalanta bc": ("atalanta",),
    "real sociedad": ("sociedad",),
    "celta vigo": ("celta",),
    "as monaco": ("monaco",),
    "nec nijmegen": ("nijmegen",),
    "twente enschede": ("twente",),
    "olympiakos piraeus": ("olympiakos", "olympiacos"),
    "olympiacos piraeus": ("olympiakos", "olympiacos"),
    "st truiden": ("st truiden", "st truidense"),
    "sint truiden": ("st truiden", "st truidense"),
    "sturm graz": ("sturm",),
    "besiktas jk": ("besiktas",),
    "red star belgrade": ("crvena zvezda",),
    "fc nordsjaelland": ("nordsjaelland",),
    "nordsjaelland": ("nordsjaelland",),
}


def resolve_team(name: str, known_keys: set[str], *, allow_fuzzy: bool = True) -> tuple[Optional[str], str]:
    target = _normalize_text(name)
    if target in known_keys:
        return target, "EXACT"
    for alt in ALTERNATIVES.get(target, ()):
        candidate = _normalize_text(alt)
        if candidate in known_keys:
            return candidate, "CURATED_ALIAS"
    if not allow_fuzzy or not target or len(target) < 5:
        return None, "UNMAPPED"
    closest = sorted(
        ((SequenceMatcher(None, target, k).ratio(), k) for k in known_keys),
        reverse=True,
    )[:2]
    if closest and closest[0][0] >= 0.91 and (
        len(closest) == 1 or closest[0][0] - closest[1][0] >= 0.06
    ):
        return closest[0][1], "FUZZY_HIGH_CONFIDENCE"
    return None, "UNMAPPED"


__all__ = ["VERSION", "ALTERNATIVES", "resolve_team"]
