from __future__ import annotations

"""Install the complete Live Yankee runtime in one deterministic order.

This exists because several historical Yankee patches wrap functions in
``live_yankee_feed``. Import order therefore changes behaviour unless startup is
explicit. The order below preserves the intended authority chain:

1. base feed / worker hook / routes
2. same-book consolidation helpers
3. focused six-source policy and source-specific price bands
4. queue-readiness presentation
5. expiry Trixie/Double fallback
6. canonical result settlement
7. clean forward-only V2 price-tolerance research
8. same-book-only final card-assembly authority
9. one-minute companion loop, capturing the fully wrapped maintenance function

The runtime remains manual/shadow only. No bookmaker bet is placed here.
"""

from typing import Any, Dict

import live_yankee_feed as feed
import live_yankee_consolidation_patch as consolidation
import live_yankee_source_focus_patch as source_focus
import live_yankee_readiness_patch as readiness
import live_yankee_expiry_fallback_patch as expiry
import live_yankee_results_patch as results
import live_yankee_price_tolerance_v2_patch as tolerance_v2
import live_yankee_same_book_only_patch as same_book
import live_yankee_fast_loop as fast_loop

VERSION = "LIVE_YANKEE_RUNTIME_V2"
_INSTALLED = False


def install(db=None) -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
            "sources": list(getattr(feed, "FROZEN_SOURCES", ())),
            "price_tolerance_version": tolerance_v2.VERSION,
        }

    # Every component is independently idempotent. Keeping the calls explicit is
    # intentional: startup logs show exactly which layer owns each behaviour.
    base_state = feed.install()
    consolidation_state = consolidation.install()
    source_state = source_focus.install()
    readiness_state = readiness.install()
    expiry_state = expiry.install()
    results_state = results.install()

    # Must follow source-focus + results: V2 depends on the six-source policy,
    # per-lane band guards and canonical source-result resolver.
    tolerance_state = tolerance_v2.install(db)

    # Final assembly authority. This deliberately comes after all page/scoreboard
    # wrappers above so its same-book-only policy remains authoritative.
    same_book_state = same_book.install()

    # Must be last. The fast loop captures feed.run_maintenance when installed;
    # installing it here ensures every fast pass includes V2 maintenance.
    fast_loop_state = fast_loop.install()

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "base": base_state,
        "consolidation": consolidation_state,
        "source_focus": source_state,
        "readiness": readiness_state,
        "expiry_fallback": expiry_state,
        "results": results_state,
        "price_tolerance_v2": tolerance_state,
        "same_book_only": same_book_state,
        "fast_loop": fast_loop_state,
        "sources": list(getattr(feed, "FROZEN_SOURCES", ())),
        "price_tolerance_version": tolerance_v2.VERSION,
        "provider_calls_added_by_tolerance": 0,
        "betting_authority_changed": False,
    }


__all__ = ["VERSION", "install"]
