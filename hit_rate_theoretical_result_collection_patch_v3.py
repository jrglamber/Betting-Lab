from __future__ import annotations

"""Corrected installer for HR theoretical result collection V2.

The V2 collector logic is valid, but its multisport installer referenced the
wrong class capitalization. This wrapper installs the same bounded collectors
against the actual runtime classes without duplicating the collection logic.
"""

import hit_rate_theoretical_result_collection_patch_v2 as core
import multisport_shadow
import tennis_shadow

VERSION = "HR_THEORETICAL_RESULT_COLLECTION_V3"
_INSTALLED = False


def install():
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    core._ORIGINAL_TENNIS_COLLECT_RESULTS = tennis_shadow.TennisShadowEngine.collect_results
    core._ORIGINAL_MULTISPORT_COLLECT_RESULTS = multisport_shadow.MultiSportShadowEngine.collect_results
    tennis_shadow.TennisShadowEngine.collect_results = core._tennis_collect_results
    multisport_shadow.MultiSportShadowEngine.collect_results = core._multisport_collect_results
    core._INSTALLED = True
    _INSTALLED = True

    state = {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "collector_core": core.VERSION,
        "sources": ["THEORETICAL_TENNIS_H2H", "THEORETICAL_MULTISPORT_H2H"],
        "exact_event_id_only": True,
        "poll_interval_minutes": core.POLL_INTERVAL_SECONDS / 60,
        "max_events_per_sport_call": core.MAX_EVENTS_PER_SPORT_CALL,
        "canonical_tables": ["tennis_results", "multisport_results"],
        "uses_existing_provider_quota": True,
        "selection_rules_changed": False,
        "pricing_rules_changed": False,
        "card_rules_changed": False,
        "staking_changed": False,
        "alerting_changed": False,
        "betting_authority_changed": False,
    }
    print(f"HR_THEORETICAL_RESULT_COLLECTION_INSTALL {state}", flush=True)
    return state
