from __future__ import annotations

"""Clear the final same-day stale tennis result after independent verification."""

import json
from db import utc_now_iso
from tennis_shadow import settle_tennis_event
import settlement_integrity_v2_patch as integrity

EVENT_ID = "3b1a4c9abfd1c51ab97cef7443137a3a"
WINNER = "Marie Bouzkova"
VERSION = "HISTORICAL_RESCUE_V1_FINAL"


def install(db):
    event = db.fetchone("SELECT * FROM tennis_events WHERE event_id=?", (EVENT_ID,))
    if not event:
        return {"installed": False, "reason": "event_not_found"}
    existing = db.fetchone("SELECT event_id FROM tennis_results WHERE event_id=?", (EVENT_ID,))
    if not existing:
        provenance = {
            "rescue": VERSION,
            "verified_on": "2026-10-12",
            "note": "Marie Bouzkova led Clara Tauson 6-1 2-0 when Tauson retired; existing tennis settlement rule grades provider winner",
        }
        db.execute(
            """
            INSERT INTO tennis_results(
                event_id,fetched_at,completed_at,winner,player_one_score,player_two_score,
                source,settlement_quality,settlement_provenance,raw_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO NOTHING
            """,
            (EVENT_ID,utc_now_iso(),event.get("commence_time"),WINNER,None,None,
             "historical_rescue","HISTORICAL_VERIFIED",
             json.dumps(provenance,separators=(",", ":")),
             json.dumps(provenance,separators=(",", ":"))),
        )
        db.execute("UPDATE tennis_events SET status='COMPLETED' WHERE event_id=?", (EVENT_ID,))
    settled = settle_tennis_event(db,EVENT_ID,WINNER)
    health = integrity.settlement_health(db)
    return {"installed": True, "settled": settled, "health": health}
