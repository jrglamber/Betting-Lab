"""Read-only multi-lane quality audit for a Betting Lab weekly research ZIP.

Usage: python tools/price_quality_audit.py path/to/research-weekly.zip
Does not fetch odds, modify the database, or alter betting decisions.
"""
import csv
import io
import json
import sys
import zipfile
from collections import Counter, defaultdict
from statistics import mean, median

def num(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return None

def load(z, name):
    path = "tables/" + name + ".csv"
    if path not in z.namelist():
        return []
    with z.open(path) as f:
        return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))

def stats(rows, pnl="pnl_units", clv="actual_clv_pct"):
    settled = [r for r in rows if r.get("status") == "SETTLED" and num(r.get(pnl)) is not None]
    clean = [num(r.get(clv)) for r in rows
             if r.get("clv_quality") in ("A", "B") and num(r.get(clv)) is not None]
    profits = sum(num(r.get(pnl)) or 0 for r in settled)
    return {
        "count": len(rows), "settled": len(settled),
        "net_units": round(profits, 3),
        "roi_pct": round(100 * profits / len(settled), 2) if settled else None,
        "clean_clv_count": len(clean),
        "clean_clv_pct": round(mean(clean), 3) if clean else None,
        "stale_clv": sum(r.get("clv_quality") == "STALE" for r in rows),
    }

def segments(rows, key, pnl="pnl_units", clv="actual_clv_pct"):
    grouped = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key) or "UNKNOWN")].append(row)
    return {k: stats(v, pnl, clv) for k, v in sorted(
        grouped.items(), key=lambda x: -len(x[1]))}

def report(path):
    with zipfile.ZipFile(path) as z:
        clv = load(z, "clv_first_samples")
        clv = [r for r in clv if r.get("evidence_mode") == "FORWARD"
               and r.get("decision") == "ACCEPT"]
        multisport = load(z, "multisport_execution_bets")
        ncaaf = [r for r in multisport if r.get("sport_key") == "americanfootball_ncaaf"
                 and num(r.get("offered_odds")) is not None
                 and 3 <= num(r["offered_odds"]) < 5]
        euro = [r for r in multisport if r.get("sport_key") == "basketball_euroleague"]
        hr = load(z, "hit_rate_cards")
        hr2 = [r for r in hr if r.get("experiment_key") == "HR2"]
        legs = load(z, "hit_rate_legs")
        hr2_ids = {r.get("id") for r in hr2}
        hr2_legs = [r for r in legs if r.get("card_id") in hr2_ids]
        leg_uses = Counter(r.get("event_id") for r in hr2_legs)
        fhg = load(z, "first_half_goal_candidates")
        fhg_prices = load(z, "first_half_goal_entry_prices")
        fhg_diagnostics = load(z, "first_half_goal_price_diagnostics")
        priced_ids = {r.get("candidate_id") for r in fhg_prices}
        return {
            "scope": "read-only research; no live-execution recommendation",
            "clv_first": {
                "overall": stats(clv),
                "by_sport": segments(clv, "sport_key"),
                "by_bookmaker": segments(clv, "bookmaker_key"),
                "by_odds_band": segments(clv, "odds_band"),
                "by_source_lane": segments(clv, "source_lane"),
            },
            "ncaaf_3_to_4_99": {
                "overall": stats(ncaaf, "net_pnl_units", "clv_pct"),
                "by_bookmaker": segments(ncaaf, "bookmaker_key", "net_pnl_units", "clv_pct"),
                "by_clv_quality": segments(ncaaf, "clv_quality", "net_pnl_units", "clv_pct"),
                "median_entry_consensus_age_minutes": median(
                    x for r in ncaaf if (x := num(r.get("consensus_age_minutes"))) is not None
                ) if any(num(r.get("consensus_age_minutes")) is not None for r in ncaaf) else None,
            },
            "euroleague": {
                "overall": stats(euro, "net_pnl_units", "clv_pct"),
                "by_bookmaker": segments(euro, "bookmaker_key", "net_pnl_units", "clv_pct"),
            },
            "hr2": {
                "by_system": segments(hr2, "system_type", "pnl_units", "clv_pct"),
                "cards": len(hr2), "legs": len(hr2_legs),
                "unique_events": len(leg_uses),
                "reused_events": sum(n > 1 for n in leg_uses.values()),
                "maximum_event_reuse": max(leg_uses.values(), default=0),
            },
            "first_half_goal": {
                "candidates": len(fhg),
                "candidates_with_entry_prices": len(priced_ids),
                "price_coverage_pct": round(100 * len(priced_ids) / len(fhg), 2) if fhg else None,
                "diagnostic_reasons": dict(Counter(r.get("reason") or "UNKNOWN" for r in fhg_diagnostics)),
                "by_tier": {tier: {"candidates": len(rows),
                                  "settled": sum(r.get("status") == "SETTLED" for r in rows),
                                  "hits": sum(str(r.get("fhg_hit")).lower() in ("1", "true") for r in rows
                                              if r.get("status") == "SETTLED")}
                            for tier, rows in ((t, [r for r in fhg if r.get("tier") == t])
                                               for t in sorted({r.get("tier") for r in fhg}, key=str))},
            },
            "limitations": [
                "These are observational, overlapping research cohorts, not independent trials.",
                "Quoted prices are not proof of obtainable exchange liquidity.",
                "The weekly ZIP does not include complete intraday odds histories for matched-time replay.",
                "CLV First is assessed only on forward accepted selections.",
                "No odds provider calls or production configuration changes are made.",
            ],
        }

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(json.dumps(report(sys.argv[1]), indent=2, allow_nan=False))
