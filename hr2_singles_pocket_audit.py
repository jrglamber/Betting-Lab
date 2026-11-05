from __future__ import annotations

"""Read-only HR2 pocket singles audit from real, frozen, placeable multiple legs.

Do not use theoretical minimum odds as if they were executable singles prices.
An underlying fixture/outcome is counted ONCE across Yankee, Double, Trixie
and Heinz, using the EARLIEST frozen eligible bookmaker quote, not the best
subsequent price. Only the continuously active V3 HR2 policy is evaluated.
This is a conditional sample: legs that actually reached a shadow multiple.
No provider calls, live betting authority, revisions to cards or policy.
"""

from collections import defaultdict
from datetime import datetime
from statistics import mean
from typing import Any

import hit_rate_multiples_lab as hr
import hr_market_integrity as integrity

VERSION = "HR2_UNIQUE_SINGLES_POCKET_AUDIT_V2_BAND_SLICES"
POLICY_VERSION = "HRM_2026-10-18_V3_THEORETICAL_QUEUE"


def _outcome(db, row):
    value = hr._result(row.get("result"))
    if value:
        return value
    # Only local source/canonical-results lookups, never fetch a result
    # provider. Includes the already-installed theoretical-result resolver.
    try:
        return hr._result(hr._source_result(db, row))
    except Exception:
        return None


def _stamp(value):
    return str(value or "")


def report(db) -> dict[str, Any]:
    legs = [dict(r) for r in db.fetchall(
        """SELECT l.*,c.created_at AS card_created_at,
                  c.system_type AS card_format,c.assembly_mode AS card_mode,
                  c.status AS card_status
           FROM hit_rate_legs l JOIN hit_rate_cards c ON c.id=l.card_id
           WHERE c.policy_version=? AND c.experiment_key='HR2'
             AND c.assembly_mode IN ('SAME_BOOK_VERIFIED','SAME_BOOK_CONSOLIDATED')
           ORDER BY c.created_at,l.id""",
        (POLICY_VERSION,),
    )]
    unique: dict[tuple[str, ...], dict] = {}
    duplicate_appearances = 0
    across_pocket_duplicates = 0
    for leg in legs:
        key = integrity.selection_key(leg)
        if key in unique:
            duplicate_appearances += 1
            existing = unique[key]
            existing["formats"].add(str(leg.get("card_format") or ""))
            if existing["lane"] != str(leg.get("source_lane") or ""):
                across_pocket_duplicates += 1
            if existing["result"] is None:
                existing["result"] = _outcome(db, leg)
            continue
        quote = float(leg.get("entry_odds") or 0)
        if not 1.0 < quote < 1000.0:
            continue
        unique[key] = {
            "lane": str(leg.get("source_lane") or "UNKNOWN"),
            "odds": quote,
            "result": _outcome(db, leg),
            "formats": {str(leg.get("card_format") or "")},
            "first_quote_at": _stamp(leg.get("quote_captured_at")),
            "first_card_at": _stamp(leg.get("card_created_at")),
            "source_table": str(leg.get("source_table") or ""),
        }

    grouped: dict[str, list] = defaultdict(list)
    for item in unique.values():
        grouped[item["lane"]].append(item)
    rows = []
    for lane, items in sorted(grouped.items()):
        decided = [x for x in items if x["result"] in ("WIN", "LOSS")]
        refunded = [x for x in items if x["result"] in ("PUSH", "VOID")]
        resolved = decided + refunded
        wins = sum(x["result"] == "WIN" for x in decided)
        lost = len(decided) - wins
        pnl = sum(x["odds"] - 1 if x["result"] == "WIN"
                  else -1 if x["result"] == "LOSS" else 0 for x in resolved)
        first = min((_stamp(x["first_card_at"]) for x in items), default=None)
        last = max((_stamp(x["first_card_at"]) for x in items), default=None)
        rows.append({
            "lane": lane,
            "unique_quoted_selections": len(items),
            "settled_with_known_outcome": len(resolved),
            "unsettled": len(items) - len(resolved),
            "wins": wins, "losses": lost, "voids_or_pushes": len(refunded),
            "hit_rate_pct": round(100 * wins / len(decided), 2) if decided else None,
            "singles_roi_pct": round(100 * pnl / len(resolved), 2) if resolved else None,
            "singles_pnl_units": round(pnl, 4),
            "average_frozen_odds": round(mean(x["odds"] for x in resolved), 3) if resolved else None,
            "first_card_at": first, "last_card_at": last,
            "formats_observed": sorted(set().union(*(x["formats"] for x in items))),
            "sample_note": (
                "INSUFFICIENT_LT_10" if len(resolved) < 10 else
                "EARLY_10_TO_29" if len(resolved) < 30 else
                "OBSERVATIONAL_30_PLUS"
            ),
        })
    rows.sort(key=lambda x: (-x["settled_with_known_outcome"], x["lane"]))

    # Split the inherited broad lanes at actual frozen odds. This is essential
    # to distinguish the HR1 core (e.g. totals <2.00) from the extra HR2
    # price band (2.00-2.99). Old EuroLeague/NFL broad sources are historical,
    # NOT evidence that those deprecated ranges remain in the current pool.
    slice_specs = {
        "FOOTBALL_TOTALS_1_80_2_99": (
            ("Football totals 1.80-1.99", 1.8, 2.0),
            ("Football totals 2.00-2.99", 2.0, 3.0),
        ),
        "TENNIS_H2H_1_80_2_99": (
            ("Tennis H2H 1.80-1.99", 1.8, 2.0),
            ("Tennis H2H 2.00-2.99", 2.0, 3.0),
        ),
        "EUROLEAGUE_H2H_1_50_2_99": (
            ("EuroLeague H2H 1.50-1.99 (historical)", 1.5, 2.0),
            ("EuroLeague H2H 2.00-2.99 (retired)", 2.0, 3.0),
        ),
        "NFL_H2H_1_50_2_99": (
            ("NFL H2H 1.50-1.99 (historical)", 1.5, 2.0),
            ("NFL H2H 2.00-2.99 (historical)", 2.0, 3.0),
        ),
    }
    band_slices = []
    for lane, specs in slice_specs.items():
        for label, lo, hi in specs:
            matches = [x for x in grouped.get(lane, ())
                       if lo <= x["odds"] < hi]
            if not matches:
                continue
            rated = [x for x in matches if x["result"] in ("WIN", "LOSS")]
            refunded = [x for x in matches if x["result"] in ("PUSH", "VOID")]
            settled = rated + refunded
            wins = sum(x["result"] == "WIN" for x in rated)
            pnl = sum(
                x["odds"] - 1 if x["result"] == "WIN"
                else -1 if x["result"] == "LOSS" else 0 for x in settled
            )
            band_slices.append({
                "parent_lane": lane,
                "band": label,
                "min_odds": lo,
                "max_exclusive": hi,
                "unique_quoted": len(matches),
                "settled": len(settled),
                "unsettled": len(matches) - len(settled),
                "wins": wins,
                "losses": len(rated) - wins,
                "hit_rate_pct": round(100 * wins / len(rated), 2) if rated else None,
                "singles_roi_pct": round(100 * pnl / len(settled), 2) if settled else None,
                "singles_pnl_units": round(pnl, 4),
                "first_card_at": min(x["first_card_at"] for x in matches),
                "last_card_at": max(x["first_card_at"] for x in matches),
            })
    all_items = list(unique.values())
    all_done = [x for x in all_items if x["result"] in ("WIN", "LOSS", "PUSH", "VOID")]
    all_decided = [x for x in all_done if x["result"] in ("WIN", "LOSS")]
    total_pnl = sum(
        x["odds"] - 1 if x["result"] == "WIN"
        else -1 if x["result"] == "LOSS" else 0 for x in all_done
    )
    return {
        "version": VERSION,
        "policy_version": POLICY_VERSION,
        "scope": "HR2 legs on actual placeable, non-synthetic shadow multiple cards",
        "coverage_limit": "Does not measure unquoted or unassembled HR2 theoretical candidates",
        "price_rule": "Earliest frozen quoted price per underlying economic selection",
        "dedup_rule": "One underlying outcome across all formats, not one per multiple appearance",
        "clv": "Unavailable for matched same-book HR2 entry quotes; do not substitute source CLV",
        "unique_quoted": len(unique),
        "unique_settled": len(all_done),
        "unique_unsettled": len(unique) - len(all_done),
        "unique_wins": sum(x["result"] == "WIN" for x in all_decided),
        "unique_losses": sum(x["result"] == "LOSS" for x in all_decided),
        "singles_roi_pct": round(100 * total_pnl / len(all_done), 2) if all_done else None,
        "singles_pnl_units": round(total_pnl, 4),
        "raw_multiple_leg_appearances": len(legs),
        "duplicate_multiple_leg_appearances_collapsed": duplicate_appearances,
        "across_pocket_duplicate_appearances": across_pocket_duplicates,
        "pockets": rows,
        "price_band_slices": band_slices,
        "no_api_requests": True, "betting_authority": False,
    }


def install(app, db):
    @app.get("/api/hr2-singles-pocket-audit")
    def hr2_singles_pocket_audit():
        return report(db)
    return {"installed": True, "version": VERSION,
            "route": "/api/hr2-singles-pocket-audit",
            "historical_cards_untouched": True, "provider_calls": 0}
