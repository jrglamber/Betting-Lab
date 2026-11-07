"""Read-only, no-API research replay for HR2 and exact-line CLV.

python tools/hr2_line_replay.py betting-lab-research-weekly.zip
Optional ZIP tables/multisport_line_odds_snapshots.csv enables exact-line closing
quote matching. Without it, coverage is explicitly reported as unavailable.
"""
import csv
import io
import json
import sys
import zipfile
from collections import defaultdict
from datetime import datetime, timezone

def rows(z, name):
    path = "tables/" + name + ".csv"
    if path not in z.namelist():
        return []
    with z.open(path) as stream:
        return list(csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig")))

def number(x):
    try:
        return float(x)
    except (ValueError, TypeError):
        return None

def dt(x):
    try:
        d = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d
    except (TypeError, ValueError):
        return None

def identity(r):
    return (r.get("event_id"), r.get("market_key"), r.get("selection"), r.get("point") or "")

def outcome(r):
    x = str(r.get("result") or "").upper()
    return x if x in ("WIN", "LOSS", "PUSH", "VOID") else None

def leg_profit(r):
    odds = number(r.get("entry_odds"))
    result = outcome(r)
    if odds is None or odds <= 1 or result is None:
        return None
    return odds - 1 if result == "WIN" else -1 if result == "LOSS" else 0

def card_profit(legs, fmt):
    """Total 1u per card, equal-staked across constituent combinations."""
    from itertools import combinations
    sizes = {"DOUBLE": (2,), "YANKEE": (2, 3, 4), "TRIXIE": (2, 3), "HEINZ": (2, 3, 4, 5, 6)}
    if fmt not in sizes:
        return None
    combos = [c for n in sizes[fmt] for c in combinations(legs, n)]
    if not combos:
        return None
    returns = []
    for c in combos:
        if any(outcome(x) is None or number(x.get("entry_odds")) is None for x in c):
            return None
        if any(outcome(x) == "LOSS" for x in c):
            returns.append(0)
        else:
            product = 1.0
            for x in c:
                if outcome(x) == "WIN":
                    product *= number(x["entry_odds"])
            returns.append(product)
    return sum(returns) / len(returns) - 1

def hr2(z):
    cards = [r for r in rows(z, "hit_rate_cards") if r.get("experiment_key") == "HR2"]
    legs = rows(z, "hit_rate_legs")
    by_card = defaultdict(list)
    for r in legs:
        by_card[r.get("card_id")].append(r)
    output = {}
    for fmt in ("DOUBLE", "YANKEE", "TRIXIE", "HEINZ"):
        selected = [c for c in cards if c.get("system_type") == fmt and c.get("status") == "SETTLED"]
        verified = [c for c in selected if c.get("assembly_mode") in ("SAME_BOOK_VERIFIED", "SAME_BOOK")]
        def aggregate(items):
            actual = [number(c.get("pnl_units")) for c in items]
            synthetic = [card_profit(by_card[c["id"]], fmt) for c in items]
            return {"cards": len(items), "recorded_units": round(sum(x for x in actual if x is not None), 4),
                    "reconstructed_units": round(sum(x for x in synthetic if x is not None), 4),
                    "reconstructed_coverage": sum(x is not None for x in synthetic)}
        output[fmt] = {"all": aggregate(selected), "same_book_verified": aggregate(verified)}
    # Identical leg pools and bookie availability are essential: do not infer
    # causal format advantage from historical cards built from different pools.
    pools = defaultdict(dict)
    for c in cards:
        if c.get("status") != "SETTLED":
            continue
        ll = by_card[c["id"]]
        if not ll:
            continue
        key = tuple(sorted((identity(l), l.get("bookmaker_key")) for l in ll))
        pools[key][c["system_type"]] = c
    matched = []
    for key, formats in pools.items():
        if len(formats) > 1:
            matched.append({"legs": len(key), "formats": sorted(formats),
                            "card_ids": {fmt: c["id"] for fmt, c in formats.items()}})
    return {"formats": output, "exact_same_leg_pool_cross_format_matches": matched,
            "cross_format_matched_count": len(matched),
            "caution": "A true prospective chronological counterfactual needs historical eligible queue and timestamped same-book prices; completed cards alone cannot establish it."}

def line_clv(z):
    bets = [r for r in rows(z, "multisport_line_bets")
            if r.get("sport_key") == "basketball_nba" and r.get("status") == "SETTLED"]
    snaps = rows(z, "multisport_line_odds_snapshots")
    indexed = defaultdict(list)
    for s in snaps:
        indexed[(s.get("event_id"), s.get("bookmaker_key"), s.get("market_key"),
                 s.get("selection"), s.get("line_point"))].append(s)
    coverage = {"nba_settled": len(bets), "snapshot_table_present": bool(snaps),
                "exact_line_matched": 0, "no_exact_line_close": 0, "no_valid_close_time": 0,
                "price_clv_pct": []}
    for b in bets:
        start = dt(b.get("commence_time"))
        # Weekly bet table does not contain kickoff time. When unavailable,
        # require recorded closing observation as upper time bound.
        cutoff = dt(b.get("closing_observed_at"))
        if cutoff is None:
            coverage["no_valid_close_time"] += 1
            continue
        key = (b.get("event_id"), b.get("bookmaker_key"), b.get("market_key"),
               b.get("selection"), b.get("line_point"))
        candidates = [s for s in indexed[key]
                      if s.get("capture_mode") == "CONVERGENCE"
                      and dt(s.get("captured_at")) is not None
                      and dt(s["captured_at"]) <= cutoff]
        if not candidates:
            coverage["no_exact_line_close"] += 1
            continue
        latest = max(candidates, key=lambda s: dt(s["captured_at"]))
        entry, close = number(b.get("offered_odds")), number(latest.get("price"))
        if entry is None or close is None or close <= 1:
            coverage["no_exact_line_close"] += 1
            continue
        coverage["exact_line_matched"] += 1
        coverage["price_clv_pct"].append(round(100 * (entry / close - 1), 4))
    values = coverage.pop("price_clv_pct")
    coverage["mean_exact_line_clv_pct"] = round(sum(values) / len(values), 3) if values else None
    coverage["warning"] = "No inference from unmatched line points. Snapshot coverage in weekly ZIP is optional; no API backfill is triggered."
    return coverage

def run(path):
    with zipfile.ZipFile(path) as z:
        return {"hr2": hr2(z), "nba_exact_line_clv": line_clv(z),
                "mode": "research-only, no network, no trades, no production writes"}

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(json.dumps(run(sys.argv[1]), indent=2))
