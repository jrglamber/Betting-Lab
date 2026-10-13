from __future__ import annotations

"""Clear independently verified same-day stale tennis obligations."""

import json
from db import utc_now_iso
from tennis_shadow import settle_tennis_event
import historical_rescue_patch as rescue
import settlement_integrity_v2_patch as integrity

EVENT_ID = "3b1a4c9abfd1c51ab97cef7443137a3a"
WINNER = "Marie Bouzkova"
VERSION = "HISTORICAL_RESCUE_V1_FINAL_20261013"

# Both fixtures were cancelled before play on 13 Oct 2026. They must therefore
# be VOID research obligations rather than fabricated wins/losses.
CANCELLED_EVENTS = {
    "97d66b459517d34fc704b84d724d1fc9": "Ann Li v Qinwen Zheng - cancelled before play, 13 Oct 2026",
    "9ac3c8445cd97600ad2bfeddb08ab298": "Anna Kalinskaya v Shuai Zhang - cancelled before play after Kalinskaya withdrew, 13 Oct 2026",
}


def _void_cancelled(db, event_id: str, note: str) -> int:
    changed = int(rescue._void_rows(db, "tennis_execution_bets", event_id) or 0)
    db.execute("UPDATE tennis_events SET status='CANCELLED' WHERE event_id=?", (event_id,))
    rescue._audit(
        db,
        event_id,
        "TENNIS",
        "VOID",
        "RESOLVED",
        note,
        {"verified_on": "2026-10-13", "resolution": "CANCELLED_BEFORE_PLAY"},
        True,
    )
    return changed


def install(db):
    settled = 0
    event = db.fetchone("SELECT * FROM tennis_events WHERE event_id=?", (EVENT_ID,))
    if event:
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

    cancelled = []
    voided_rows = 0
    for event_id, note in CANCELLED_EVENTS.items():
        stored = db.fetchone("SELECT event_id FROM tennis_events WHERE event_id=?", (event_id,))
        if not stored:
            cancelled.append({"event_id": event_id, "found": False, "voided_rows": 0})
            continue
        changed = _void_cancelled(db, event_id, note)
        voided_rows += changed
        cancelled.append({"event_id": event_id, "found": True, "voided_rows": changed})

    health = integrity.settlement_health(db)
    return {
        "installed": True,
        "version": VERSION,
        "settled": settled,
        "cancelled": cancelled,
        "voided_rows": voided_rows,
        "health": health,
    }
