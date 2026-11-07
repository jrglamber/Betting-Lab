"""Forward-only, read-only HR2 cohort snapshots (no provider calls or alerts).

Designed to run after existing HR2 maintenance. Writes to separate research
tables only; never consumes queue items or modifies Telegram delivery.
"""
import hashlib
import json
from datetime import datetime, timezone

def capture(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hr2_forward_pool_snapshots (
        snapshot_key TEXT PRIMARY KEY, observed_at TEXT NOT NULL,
        experiment_key TEXT NOT NULL, bookmaker_key TEXT NOT NULL,
        candidate_ids_json TEXT NOT NULL, legs_json TEXT NOT NULL,
        eligible_singles INTEGER NOT NULL, eligible_doubles INTEGER NOT NULL,
        eligible_yankees INTEGER NOT NULL
    )""")
    now = datetime.now(timezone.utc).isoformat()
    candidates = db.fetchall("""SELECT * FROM hit_rate_candidates
        WHERE experiment_key='HR2' AND status='QUEUED'
        ORDER BY first_seen_at,id""")
    # Source candidates alone cannot establish bookmaker feasibility. Preserve
    # eligibility as unverified until a priced, same-book quote exists.
    groups = {}
    for c in candidates:
        event = str(c.get("event_id") or "")
        if not event:
            continue
        group = groups.setdefault(str(c.get("system_type") or "UNKNOWN"), {})
        if event not in group:
            group[event] = c
    recorded = 0
    for source_system, by_event in groups.items():
        legs = list(by_event.values())
        ids = [int(x["id"]) for x in legs]
        # Do not label source odds as executable odds or infer a bookmaker.
        payload = [{"candidate_id": int(x["id"]), "event_id": x["event_id"],
                    "market_key": x.get("market_key"), "selection": x.get("selection"),
                    "point": x.get("point"), "source_odds": x.get("source_odds"),
                    "first_seen_at": x.get("first_seen_at"),
                    "commence_time": x.get("commence_time")} for x in legs]
        key = hashlib.sha256((source_system + "|" + ",".join(map(str, ids))).encode()).hexdigest()
        db.execute("""INSERT INTO hr2_forward_pool_snapshots(
            snapshot_key,observed_at,experiment_key,bookmaker_key,
            candidate_ids_json,legs_json,eligible_singles,eligible_doubles,eligible_yankees)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(snapshot_key) DO NOTHING""",
            (key,now,"HR2","UNVERIFIED",json.dumps(ids),json.dumps(payload,default=str),0,0,0))
        recorded += 1
    return {"snapshot_groups_seen": recorded,
            "note": "Source-only queue snapshots. Bookmaker-feasible replay remains pending priced quote capture."}
