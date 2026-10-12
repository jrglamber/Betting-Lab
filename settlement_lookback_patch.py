from __future__ import annotations

"""Mechanical settlement hardening.

The Odds API scores endpoint supports a three-day completed-result lookback.
Some older collectors requested only one day, which made a missed poll/outage
able to strand otherwise valid shadow bets. Force all score requests onto the
full three-day recovery window. Research/selection logic is untouched.
"""

from typing import Any, Dict

from the_odds_api import TheOddsApi

VERSION = "SETTLEMENT_LOOKBACK_V1"
APP_VERSION = "0.19.54"
_INSTALLED = False
_ORIGINAL_SCORES = TheOddsApi.scores


def _scores_three_day(self, sport_key: str, *, event_ids=(), days_from: int = 1):
    # Provider supports up to three days; always use the full recovery window.
    return _ORIGINAL_SCORES(self, sport_key, event_ids=event_ids, days_from=3)


def install() -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}
    TheOddsApi.scores = _scores_three_day
    try:
        import web as web_module
        web_module.api.scores = _scores_three_day.__get__(web_module.api, TheOddsApi)
        web_module.VERSION = APP_VERSION
    except Exception:
        pass
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "score_lookback_days": 3,
        "dashboard_version": APP_VERSION,
    }
