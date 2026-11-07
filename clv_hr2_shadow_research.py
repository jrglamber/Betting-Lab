"""Read-only CLV-first basketball and HR2 overlap research.

No provider calls, writes, Telegram changes, or betting authority.
Only forward evidence is eligible; joins require exact event/market/side/line.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timezone


def _dt(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _norm(value):
    return str(value or "").strip().casefold()


def _point(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        return round(float(value), 5)
    except (TypeError, ValueError):
        return None


def key(row):
    return (_norm(row.get("event_id")), _norm(row.get("market_key")),
            _norm(row.get("selection")), _point(row.get("point")))


def eligible(row):
    created, starts = _dt(row.get("created_at")), _dt(row.get("commence_time"))
    return (row.get("evidence_mode") == "FORWARD"
            and created is not None and starts is not None and created < starts
            and row.get("decision") in ("ACCEPT", "OBSERVE")
            and float(row.get("entry_odds") or 0) > 1)


def score(rows):
    settled = [r for r in rows if r.get("pnl_units") is not None]
    units = sum(float(r["pnl_units"]) for r in settled)
    return {"settled": len(settled), "pnl_units": round(units, 4),
            "roi_pct": round(100 * units / len(settled), 3) if settled else None,
            "distinct_events": len({_norm(r.get("event_id")) for r in settled})}


def analyze(clv_rows, hr_rows):
    """Compare exact-match HR candidates with pre-existing CLV decisions.

    Only CLV snapshots created before the HR candidate's first_seen_at qualify.
    Multiple matching CLV records are reduced to the most recent prior record;
    opposite decisions or unqualified observations never become a positive signal.
    """
    forward = [r for r in clv_rows if eligible(r)]
    basketball = [r for r in forward if _norm(r.get("sport_key")).startswith("basketball")
                  or "basketball" in _norm(r.get("source_lane"))]
    buckets = defaultdict(list)
    for r in basketball:
        buckets[r["decision"]].append(r)
    indexed = defaultdict(list)
    for r in forward:
        indexed[key(r)].append(r)
    for records in indexed.values():
        records.sort(key=lambda r: _dt(r["created_at"]))
    overlap = defaultdict(list)
    for candidate in hr_rows:
        when = _dt(candidate.get("first_seen_at"))
        if when is None:
            continue
        previous = [r for r in indexed.get(key(candidate), [])
                    if _dt(r["created_at"]) <= when]
        decision = previous[-1]["decision"] if previous else "NO_PRIOR_MATCH"
        overlap[decision].append(candidate)
    return {"basketball": {decision: score(rows) for decision, rows in buckets.items()},
            "hr2_overlap_counts": {name: len(rows) for name, rows in overlap.items()},
            "limitations": ["No odds/execution inference from candidate-only overlap",
                            "Overlapping markets on the same event are correlated",
                            "Historical CLV labels must not be used as pre-match signals"]}


def from_database(db):
    """Read-only report; returns an explicit error when tables are unavailable."""
    clv = db.fetchall("SELECT * FROM clv_first_samples WHERE evidence_mode='FORWARD'")
    hr = db.fetchall("SELECT * FROM hit_rate_candidates WHERE experiment_key='HR2'")
    return analyze(clv, hr)
