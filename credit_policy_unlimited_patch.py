from __future__ import annotations

"""Make provider quota the only credit ceiling for quote-refresh research lanes.

The Odds API plan has its own real remaining-credit guard.  These two newer
forward labs had additional hard-coded 500-credit/day caps layered on top of
that provider limit, which could leave eligible selections stuck at WAIT even
while most of the monthly quota remained unused.

100,000/day is deliberately equal to the full monthly provider allowance and
therefore functions as "no local daily cap" without weakening the provider's
own remaining-credit check.  This patch changes collection capacity only; it
does not alter selection, model, staking, settlement, or execution authority.
"""

import hit_rate_multiples_lab
import live_yankee_feed

VERSION = "CREDIT_POLICY_PROVIDER_QUOTA_ONLY_V1"
FUNCTIONAL_UNCAPPED_DAILY_CREDITS = 100_000
_INSTALLED = False


def install() -> dict:
    global _INSTALLED
    before = {
        "hit_rate_multiples": int(hit_rate_multiples_lab.DAILY_REFRESH_CREDIT_BUDGET),
        "live_yankee": int(live_yankee_feed.DAILY_REFRESH_CREDIT_BUDGET),
    }

    hit_rate_multiples_lab.DAILY_REFRESH_CREDIT_BUDGET = FUNCTIONAL_UNCAPPED_DAILY_CREDITS
    live_yankee_feed.DAILY_REFRESH_CREDIT_BUDGET = FUNCTIONAL_UNCAPPED_DAILY_CREDITS

    result = {
        "installed": True,
        "already_installed": _INSTALLED,
        "version": VERSION,
        "policy": "provider_quota_only",
        "functional_daily_ceiling": FUNCTIONAL_UNCAPPED_DAILY_CREDITS,
        "before": before,
        "after": {
            "hit_rate_multiples": hit_rate_multiples_lab.DAILY_REFRESH_CREDIT_BUDGET,
            "live_yankee": live_yankee_feed.DAILY_REFRESH_CREDIT_BUDGET,
        },
    }
    _INSTALLED = True
    return result
