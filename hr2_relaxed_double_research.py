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
    if candidate.get("point") != quote.get("point"):
        return False
    book = str(quote.get("bookmaker_key") or "")
    if not book:
        return False
    try:
        ts = _stamp(quote["captured_at"])
        first = _stamp(candidate["first_seen_at"])
        price = float(quote["price"])
        minimum = float(candidate["min_odds"])
        lower = float(candidate["band_min"])
        upper = float(candidate["band_max"])
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
    age = (now-ts).total_seconds()/60
    if age < 0 or age > max_age or ts < first:
        return False
    if not (lower <= price < upper):
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
