"""Forward-only, read-only HR2 cohort snapshots (no provider calls or alerts).

Designed to run after existing HR2 maintenance. Writes to separate research
tables only; never consumes queue items or modifies Telegram delivery.
"""
import hashlib
import json
from datetime import datetime, timezone
from math import comb

def capture(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hr2_forward_pool_snapshots (
        snapshot_key TEXT PRIMARY KEY, observed_at TEXT NOT NULL,
        experiment_key TEXT NOT NULL, bookmaker_key TEXT NOT NULL,
        candidate_ids_json TEXT NOT NULL, legs_json TEXT NOT NULL,
        eligible_singles INTEGER NOT NULL, eligible_doubles INTEGER NOT NULL,
        eligible_yankees INTEGER NOT NULL
    )""")
    observed = datetime.now(timezone.utc)
    now = observed.isoformat()
    candidates = db.fetchall("""SELECT * FROM hit_rate_candidates
        WHERE experiment_key='HR2' AND status='QUEUED'
        ORDER BY first_seen_at,id""")
    from hit_rate_multiples_lab import _quotes
    # Price each distinct event from the already-collected manual bookmaker quotes.
    # No odds provider requests and no queue consumption.
    by_book = {}
    for c in candidates:
        if not c.get("event_id"):
            continue
        start = c.get("commence_time")
        try:
            start_at = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            if start_at.tzinfo is None or start_at <= observed:
                continue
        except (ValueError, TypeError):
            continue
        for book, quote in _quotes(db, c, observed, False).items():
            group = by_book.setdefault(book, {})
            event = str(c["event_id"])
            candidate = {"candidate_id": int(c["id"]), "event_id": event,
                         "market_key": c.get("market_key"), "selection": c.get("selection"),
                         "point": c.get("point"), "odds": float(quote["price"]),
                         "quote_captured_at": quote.get("captured_at"),
                         "first_seen_at": c.get("first_seen_at"),
                         "commence_time": c.get("commence_time")}
            if event not in group or candidate["odds"] > group[event]["odds"]:
                group[event] = candidate
    recorded = 0
    for book, event_map in by_book.items():
        legs = sorted(event_map.values(), key=lambda x: (str(x["first_seen_at"]), x["event_id"]))[:24]
        # Persist each observed priced pool, not just cards that the production
        # assembler chose. Same total 1u per hypothetical card at replay time.
        ids = [x["candidate_id"] for x in legs]
        # Identical priced pools are captured once, regardless of polling frequency.
        signature = book + "|" + json.dumps(
            [(x["candidate_id"], x["odds"], x["quote_captured_at"]) for x in legs])
        key = hashlib.sha256(signature.encode()).hexdigest()
        db.execute("""INSERT INTO hr2_forward_pool_snapshots(
            snapshot_key,observed_at,experiment_key,bookmaker_key,
            candidate_ids_json,legs_json,eligible_singles,eligible_doubles,eligible_yankees)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(snapshot_key) DO NOTHING""",
            (key, now, "HR2", book, json.dumps(ids),
             json.dumps(legs, default=str), len(legs),
             comb(len(legs), 2) if len(legs) >= 2 else 0,
             comb(len(legs), 4) if len(legs) >= 4 else 0))
        recorded += 1
    return {"priced_same_book_pools": recorded,
            "note": "Forward quote capture only; settlement replay pending."}
