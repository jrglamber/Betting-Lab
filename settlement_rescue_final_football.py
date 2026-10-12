from __future__ import annotations

"""Final audited football cleanup for the 12 Oct 2026 stale-result review.

These four stored events were still being surfaced by the broad settlement
obligation audit even though none now had a stale primary executable selection.
Each original fixture was independently verified as postponed/rescheduled.
Marking the original event VOID/POSTPONED clears only stale research obligations;
it does not fabricate a score and does not change selection or betting logic.
"""

from typing import Any, Dict

import historical_rescue_patch as rescue

VERSION = "HISTORICAL_RESCUE_FINAL_FOOTBALL_V1"

POSTPONED = {
    "6601d28f93317915788723bc082330fe": "Port Vale v Northampton Town, 26 Sep 2026 - postponed",
    "3baccaa125561b5ba84e20410bdf4bf1": "Walsall v Crawley Town, 3 Oct 2026 - postponed due to international call-ups",
    "8ebfd1b24fe78a65203845279b47595f": "Barnsley v Milton Keynes Dons, 3 Oct 2026 - postponed",
    "d60ded8469d93f050daa8f4b04c2670d": "PAOK Thessaloniki v Kalamata, original 10 Oct 2026 fixture - rescheduled to 12 Oct 2026",
}


def install(db) -> Dict[str, Any]:
    changed = 0
    events = []
    for event_id, note in POSTPONED.items():
        try:
            n = int(rescue._void_football(db, event_id, "POSTPONED", note) or 0)
            changed += n
            events.append({"event_id": event_id, "changed_rows": n, "ok": True})
        except Exception as exc:
            events.append({
                "event_id": event_id,
                "changed_rows": 0,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
    return {
        "installed": True,
        "version": VERSION,
        "events": events,
        "changed_rows": changed,
    }
