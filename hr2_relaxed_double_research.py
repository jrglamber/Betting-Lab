"""Research-only comparison of strict and relaxed HR2 double quote matching.

No provider calls, production card formation, Telegram, or execution.
Inputs are existing candidate/quote records; quotes must be independently
validated for event, market, selection, line and bookmaker by the caller.
"""
from datetime import datetime, timedelta, timezone
from itertools import combinations
from math import prod

STRICT_AGE_MIN = 15
STRICT_SPREAD_MIN = 5
RELAXED_AGE_MIN = 30
RELAXED_SPREAD_MIN = 20
RELAXED_PRICE_RETENTION = 0.95


def _stamp(value):
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Quote timestamps must be timezone-aware")
    return dt.astimezone(timezone.utc)


def _eligible(candidate, quote, now, max_age, min_retention):
    """Reject unverifiable, stale, pre-candidate and off-market quotes."""
    if any(str(candidate.get(k) or "") != str(quote.get(k) or "")
           for k in ("event_id", "market_key", "selection")):
        return False
    try:
        point_matches = (candidate.get("point") is None and quote.get("point") is None) or (
            candidate.get("point") is not None and quote.get("point") is not None
            and abs(float(candidate["point"])-float(quote["point"])) < 1e-9
        )
    except (TypeError, ValueError):
        return False
    if not point_matches:
        return False
    book = str(quote.get("bookmaker_key") or "")
    if not book:
        return False
    try:
        ts = _stamp(quote["captured_at"])
        first = _stamp(candidate.get("first_seen_at") or candidate["source_created_at"])
        price = float(quote["price"])
        minimum = float(candidate["min_odds"])
        lower = float(candidate["band_min"])
        upper = float(candidate["band_max"])
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
    age = (now-ts).total_seconds()/60
    if age < 0 or age > max_age or ts < first:
        return False
    if not (lower*min_retention <= price < upper):
        return False
    return price >= minimum*min_retention


def compare_doubles(candidates, quotes, now):
    """Return comparable hypothetical doubles, without asserting profitability.

    candidates: unique candidate dicts; quotes: raw quote dicts with market
    identity. Opposing/ambiguous fixtures must be excluded upstream.
    """
    now = _stamp(now)
    output = {"strict": [], "relaxed": []}
    for label, age, spread, retention in (
        ("strict", STRICT_AGE_MIN, STRICT_SPREAD_MIN, 1.0),
        ("relaxed", RELAXED_AGE_MIN, RELAXED_SPREAD_MIN, RELAXED_PRICE_RETENTION),
    ):
        for a,b in combinations(candidates,2):
            if str(a.get("fixture_key") or a.get("event_id")) == str(b.get("fixture_key") or b.get("event_id")):
                continue
            if not a.get("fixture_key") or not b.get("fixture_key"):
                continue
            aq = [q for q in quotes if _eligible(a,q,now,age,retention)]
            bq = [q for q in quotes if _eligible(b,q,now,age,retention)]
            for x in aq:
                for y in bq:
                    if x["bookmaker_key"] != y["bookmaker_key"]:
                        continue
                    if abs((_stamp(x["captured_at"])-_stamp(y["captured_at"])).total_seconds()) > spread*60:
                        continue
                    odds = prod((float(x["price"]),float(y["price"])))
                    output[label].append({
                        "candidate_ids": [a.get("id"),b.get("id")],
                        "bookmaker": x["bookmaker_key"],
                        "decimal_odds": odds,
                        "break_even_hit_rate": 1/odds,
                        "quote_spread_minutes": abs((_stamp(x["captured_at"])-_stamp(y["captured_at"])).total_seconds())/60,
                        "hypothetical_only": True,
                    })
    return output

def observe(db, candidates, now, max_candidates=30):
    """Persist prospective matched pairs from existing stored quotes only."""
    import hashlib
    import json
    now = _stamp(now)
    id_type = "BIGSERIAL PRIMARY KEY" if db.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db.execute(f"""CREATE TABLE IF NOT EXISTS hr2_relaxed_double_observations (
        id {id_type}, observation_key TEXT NOT NULL UNIQUE,
        observed_at TEXT NOT NULL, variant TEXT NOT NULL,
        candidate_ids_json TEXT NOT NULL, candidate_sources_json TEXT NOT NULL, bookmaker_key TEXT NOT NULL,
        combined_odds REAL NOT NULL, break_even_hit_rate REAL NOT NULL,
        quote_spread_minutes REAL NOT NULL, status TEXT NOT NULL,\n        settled_at TEXT, pnl_units REAL, leg_results_json TEXT
    )""")
    # Restrict to the prospective, actionable HR2 pool, without modifying it.
    pool = list(candidates)[:max_candidates]
    if len(pool) < 2:
        return {"strict_new": 0, "relaxed_new": 0, "reason": "insufficient_candidates"}
    ids = [str(c.get("event_id") or "") for c in pool if c.get("event_id")]
    if not ids:
        return {"strict_new": 0, "relaxed_new": 0, "reason": "no_event_ids"}
    placeholders = ",".join("?" for _ in ids)
    raw = db.fetchall(
        "SELECT * FROM live_yankee_quotes WHERE event_id IN (" + placeholders +
        ") AND captured_at>=? ORDER BY captured_at DESC LIMIT 1000",
        tuple(ids) + ((now-timedelta(minutes=RELAXED_AGE_MIN)).isoformat(),)
    )
    quotes = [dict(row) for row in raw]
    result = compare_doubles(pool, quotes, now)
    counts = {"strict_new": 0, "relaxed_new": 0}
    for variant, matches in result.items():
        for match in matches:
            # One forward observation per ordered pair, book and variant;
            # avoid repeatedly counting identical quotes on each maintenance.
            key_source = [variant, match["candidate_ids"], match["bookmaker"]]
            key = hashlib.sha256(json.dumps(key_source, sort_keys=True).encode()).hexdigest()
            existing = db.fetchone(
                "SELECT id FROM hr2_relaxed_double_observations WHERE observation_key=?", (key,)
            )
            if existing:
                continue
            source_by_id = {str(x.get("id")): x for x in pool}
            sources = []
            for candidate_id in match["candidate_ids"]:
                source = source_by_id.get(str(candidate_id))
                if not source or not source.get("source_table") or source.get("source_id") is None:
                    break
                sources.append({"source_table": source["source_table"],
                                "source_id": source["source_id"]})
            if len(sources) != 2:
                continue
            db.execute("""INSERT INTO hr2_relaxed_double_observations (
                observation_key, observed_at, variant, candidate_ids_json,
                candidate_sources_json, bookmaker_key, combined_odds,
                break_even_hit_rate, quote_spread_minutes, status
            ) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(observation_key) DO NOTHING""", (
                key, now.isoformat(), variant, json.dumps(match["candidate_ids"]),
                json.dumps(sources), match["bookmaker"], match["decimal_odds"],
                match["break_even_hit_rate"], match["quote_spread_minutes"],
                "AWAITING_SETTLEMENT",
            ))
            counts[variant+"_new"] += 1
    return counts

def settle(db, now=None, max_rows=100):
    """Settle prospective doubles from authoritative source results only."""
    import json
    import hit_rate_multiples_lab as hr
    now = _stamp(now or datetime.now(timezone.utc))
    rows = db.fetchall(
        "SELECT * FROM hr2_relaxed_double_observations "
        "WHERE status='AWAITING_SETTLEMENT' ORDER BY id LIMIT ?",
        (max_rows,),
    )
    settled = 0
    for row in rows:
        try:
            sources = json.loads(row["candidate_sources_json"])
            if len(sources) != 2:
                continue
            results = [hr._source_result(db, source) for source in sources]
            normalized = [hr._result(x) for x in results]
            if any(x is None for x in normalized):
                continue
            if any(x in ("VOID", "PUSH") for x in normalized):
                # A void leg changes effective double odds; exclude from
                # comparable double ROI rather than silently treat as a loss.
                status, pnl = "VOID", None
            else:
                status = "SETTLED"
                pnl = float(row["combined_odds"])-1 if all(x == "WIN" for x in normalized) else -1.0
            db.execute(
                "UPDATE hr2_relaxed_double_observations SET "
                "status=?, settled_at=?, pnl_units=?, leg_results_json=? WHERE id=?",
                (status, now.isoformat(), pnl, json.dumps(normalized), row["id"]),
            )
            settled += 1
        except (KeyError, TypeError, ValueError):
            continue
    return {"settled_or_voided": settled}
