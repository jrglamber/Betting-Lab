"""Forward-only CLV basketball chronological same-book replay (research only).

This replays recorded CLV-first exchange quotes, not bookmaker-executable HR2 bets.
No provider calls, live gating, or database mutations.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from math import prod


def dt(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def is_basketball(row):
    return str(row.get("sport_key") or "").upper() == "BASKETBALL"


def replay(rows, decision="ACCEPT", sizes=(1, 2, 3, 4), max_wait_hours=24):
    """Greedy chronological non-overlapping groups, frozen quote at observation.

    First observed eligible quote per event/market/selection/line/bookmaker.
    A group must be simultaneously known before its earliest event starts;
    all legs must have the same bookmaker and distinct event IDs.
    A four-leg group is settled as a Yankee (six doubles, four trebles,
    one fourfold), each component receiving 1/11 of the total stake.
    """
    eligible = []
    for r in rows:
        created, start = dt(r.get("created_at")), dt(r.get("commence_time"))
        if (r.get("evidence_mode") != "FORWARD" or r.get("decision") != decision
                or not is_basketball(r) or not created or not start or created >= start
                or r.get("pnl_units") is None or float(r.get("entry_odds") or 0) <= 1
                or not r.get("bookmaker_key") or not r.get("event_id")):
            continue
        eligible.append(dict(r, _created=created, _start=start))
    eligible.sort(key=lambda r: (r["_created"], str(r.get("sample_key"))))
    seen, unique = set(), []
    for r in eligible:
        identity = tuple(str(r.get(k) or "").lower() for k in
                         ("event_id", "market_key", "selection", "point", "bookmaker_key"))
        if identity not in seen:
            seen.add(identity)
            unique.append(r)
    results = {}
    for size in sizes:
        queues = defaultdict(list)
        for r in unique:
            queues[str(r["bookmaker_key"]).lower()].append(r)
        cards = []
        for book, stream in queues.items():
            remaining = list(stream)
            while len(remaining) >= size:
                group = remaining[:size]
                ready = max(x["_created"] for x in group)
                earliest = min(x["_start"] for x in group)
                if (len({x["event_id"] for x in group}) != size
                        or ready >= earliest
                        or (ready - group[0]["_created"]).total_seconds() > max_wait_hours * 3600):
                    remaining.pop(0)
                    continue
                wins = [float(x["pnl_units"]) > 0 for x in group]
                odds = [float(x["entry_odds"]) for x in group]
                if size == 4:
                    parts = [prod(odds[i] for i in ix) if all(wins[i] for i in ix) else 0
                             for n in (2, 3, 4) for ix in combinations(range(4), n)]
                    returned = sum(parts) / 11
                else:
                    returned = prod(odds) if all(wins) else 0
                cards.append(dict(bookmaker=book, ready_at=ready.isoformat(),
                                  event_ids=[x["event_id"] for x in group],
                                  return_units=round(returned, 6),
                                  pnl_units=round(returned - 1, 6)))
                del remaining[:size]
        pnl = sum(c["pnl_units"] for c in cards)
        results[{1:"singles", 2:"doubles", 3:"trebles", 4:"yankees"}.get(size, str(size))] = {
            "cards": len(cards), "pnl_units": round(pnl, 4),
            "roi_pct": round(100 * pnl / len(cards), 2) if cards else None,
            "cards_detail": cards}
    return {"decision": decision, "eligible_distinct_quotes": len(unique),
            "assumption": "Recorded exchange odds, not verified executable bookmaker prices",
            "results": results}
