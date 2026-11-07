"""Pure, stake-normalized HR2 format comparisons.

All legs must be from one bookmaker and distinct events. Returns are for a
single frozen four-selection cohort, not repeated overlapping snapshots.
"""
from itertools import combinations
from math import prod


def compare_four(legs):
    if len(legs) != 4:
        raise ValueError("Exactly four legs required for matched format comparison")
    books = {str(x["bookmaker_key"]) for x in legs}
    events = {str(x["event_id"]) for x in legs}
    if len(books) != 1 or len(events) != 4:
        raise ValueError("Four distinct events and one bookmaker required")
    for x in legs:
        if float(x["odds"]) <= 1:
            raise ValueError("Invalid decimal odds")
        if str(x["result"]).upper() not in ("WIN", "LOSS", "VOID", "PUSH"):
            raise ValueError("All four legs must be settled")
    def payout(combo):
        return prod(float(x["odds"]) if str(x["result"]).upper() == "WIN"
                    else 1.0 if str(x["result"]).upper() in ("VOID", "PUSH")
                    else 0.0 for x in combo)
    formats = {"SINGLES": (1,), "DOUBLES": (2,), "YANKEE": (2, 3, 4)}
    out = {}
    for name, sizes in formats.items():
        bets = [combo for size in sizes for combo in combinations(legs, size)]
        returned = sum(payout(combo) for combo in bets) / len(bets)
        out[name] = {"bets": len(bets), "return_units": returned,
                     "pnl_units": returned - 1, "roi_pct": 100 * (returned - 1)}
    return out
