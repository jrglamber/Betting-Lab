from __future__ import annotations

"""Global bookmaker exclusions for Betting Lab research and shadow execution.

The user cannot access Paddy Power.  Filtering at The Odds API response boundary
prevents its prices from influencing consensus, qualification, comparison,
manual multiples, first-half-goal price capture, or any later research lane that
reuses provider responses.

This patch never places bets and does not alter provider request cost.
"""

from copy import deepcopy
from typing import Any

from the_odds_api import ApiResult, TheOddsApi

VERSION = "BOOKMAKER_EXCLUSION_2026_10_20_V1"
EXCLUDED_KEYS = {"paddypower", "paddy_power", "paddy-power"}
EXCLUDED_TITLES = {"paddy power", "paddypower"}
_INSTALLED = False
_ORIGINALS: dict[str, Any] = {}


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").replace("-", " ").split())


def _excluded(book: dict[str, Any]) -> bool:
    key = str(book.get("key") or "").strip().lower()
    title = _norm(book.get("title"))
    return key in EXCLUDED_KEYS or title in EXCLUDED_TITLES


def _filter_payload(payload: Any) -> Any:
    """Return a copy with excluded bookmaker blocks removed at every event level."""
    if isinstance(payload, list):
        return [_filter_payload(item) for item in payload]
    if not isinstance(payload, dict):
        return payload

    out = deepcopy(payload)
    books = out.get("bookmakers")
    if isinstance(books, list):
        out["bookmakers"] = [book for book in books if not (isinstance(book, dict) and _excluded(book))]
    return out


def _wrap(name: str) -> None:
    original = getattr(TheOddsApi, name)
    _ORIGINALS[name] = original

    def wrapped(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if not isinstance(result, ApiResult):
            return result
        return ApiResult(
            data=_filter_payload(result.data),
            remaining=result.remaining,
            used=result.used,
            last_cost=result.last_cost,
        )

    setattr(TheOddsApi, name, wrapped)


def install() -> dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
            "excluded": sorted(EXCLUDED_KEYS),
        }

    for method in ("sport_odds", "event_odds", "historical_event_odds"):
        _wrap(method)

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "excluded": sorted(EXCLUDED_KEYS),
        "scope": "all The Odds API bookmaker responses",
        "betting_authority": False,
    }


INSTALL_STATE = install()
