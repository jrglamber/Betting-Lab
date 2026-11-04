from __future__ import annotations

"""Read-only, stake-matched singles versus multiples audit.

Every settled multiple card is matched against hypothetical equal-staked singles
from that card's own frozen legs and entry prices. A £10 multiple is therefore
compared with £10 in total singles, NOT £10 on each leg. Repeated selections
are retained in the matched comparison because they would be staked again;
a separate deduplicated reference is reported without price hindsight.
"""

from collections import defaultdict
import json

VERSION = "SINGLES_VS_MULTIPLES_MATCHED_V1"


def _norm_result(result):
    text = str(result or "").strip().upper()
    if text in ("WIN", "WON", "WINNER"):
        return "WIN"
    if text in ("LOSS", "LOSE", "LOST", "LOSER"):
        return "LOSS"
    if text in ("PUSH", "VOID", "REFUND", "CANCELLED", "CANCELED"):
        return "VOID"
    return None


def _leg_pnl(leg):
    result = _norm_result(leg.get("result"))
    if result is None:
        return None
    if result == "WIN":
        odds = float(leg["entry_odds"])
        return odds - 1.0 if odds >= 1.0 else None
    if result == "LOSS":
        return -1.0
    return 0.0


def _identity(leg):
    point = leg.get("point")
    if point is not None:
        try:
            point = round(float(point), 8)
        except (TypeError, ValueError):
            point = str(point)
    return (str(leg.get("event_id") or ""),
            str(leg.get("market_key") or "").lower(),
            str(leg.get("selection") or "").strip().lower(), point)


def _summary(cards):
    """Cards: each holds a frozen set of legs and one observed multiple P&L."""
    measured = []
    skipped = 0
    all_legs = []
    for card in cards:
        legs = [dict(l) for l in card["legs"]]
        values = [_leg_pnl(leg) for leg in legs]
        if not legs or any(v is None for v in values):
            skipped += 1
            continue
        matched_single = sum(values) / len(legs)
        measured.append((float(card["multiple_pnl"]), matched_single))
        all_legs.extend(legs)
    n = len(measured)
    multi_units = sum(a for a, _ in measured)
    singles_units = sum(b for _, b in measured)
    by_identity = {}
    for leg in all_legs:
        # First observed quote in card/leg order, not the retrospectively
        # best price. This is descriptive only, not a feasible global bet plan.
        by_identity.setdefault(_identity(leg), leg)
    unique_values = [_leg_pnl(leg) for leg in by_identity.values()]
    unique_values = [x for x in unique_values if x is not None]
    return {
        "settled_cards": n,
        "skipped_incomplete": skipped,
        "leg_appearances": len(all_legs),
        "unique_selections": len(by_identity),
        "duplicates_removed_in_unique_view": len(all_legs) - len(by_identity),
        "multiples_pnl_per_10_gbp": round(10 * multi_units, 2),
        "singles_pnl_per_10_gbp": round(10 * singles_units, 2),
        "multiples_roi_pct": round(100 * multi_units/n, 2) if n else None,
        "matched_singles_roi_pct": round(100 * singles_units/n, 2) if n else None,
        "difference_multiples_minus_singles_pp":
            round(100 * (multi_units - singles_units)/n, 2) if n else None,
        "unique_singles_roi_pct":
            round(100 * sum(unique_values)/len(unique_values), 2)
            if unique_values else None,
        "unique_singles_n": len(unique_values),
        "stake_basis": "Each settled card = 1 equal total unit in each approach",
    }


def _load_cards(db, parent_table, leg_table, join_col, *, extra_where="",
                filters=(), group_labels=("system_type",)):
    rows = db.fetchall(
        f"""SELECT * FROM {parent_table}
            WHERE status='SETTLED' AND pnl_units IS NOT NULL
            {extra_where} ORDER BY id""", filters
    )
    parent_ids = [int(r["id"]) for r in rows]
    if not parent_ids:
        return []
    placeholders = ",".join("?" for _ in parent_ids)
    legs = db.fetchall(
        f"""SELECT * FROM {leg_table} WHERE {join_col} IN ({placeholders})
            ORDER BY {join_col},leg_order,id""", tuple(parent_ids)
    )
    by_parent = defaultdict(list)
    for leg in legs:
        by_parent[int(leg[join_col])].append(dict(leg))
    return [
        {
            "labels": tuple(str(r.get(k) or "") for k in group_labels),
            "multiple_pnl": float(r["pnl_units"]),
            "legs": by_parent[int(r["id"])],
        }
        for r in rows
    ]


def _segment(cards, names):
    if not cards:
        return {"ALL": _summary([])}
    grouped = defaultdict(list)
    for c in cards:
        grouped[("ALL",)].append(c)
        grouped[c["labels"]].append(c)
    return {
        "|".join(k): _summary(v) for k, v in sorted(grouped.items())
    }


def _hit_rate(db):
    all_cards = _load_cards(
        db, "hit_rate_cards", "hit_rate_legs", "card_id",
        extra_where="AND system_type IN ('YANKEE','DOUBLE','TRIXIE')",
        group_labels=("experiment_key", "system_type"),
    )
    placeable = _load_cards(
        db, "hit_rate_cards", "hit_rate_legs", "card_id",
        extra_where="""AND system_type IN ('YANKEE','DOUBLE','TRIXIE')
                       AND assembly_mode!='CROSS_BOOK_SYNTHETIC'""",
        group_labels=("experiment_key", "system_type"),
    )
    return {
        "placeable": _segment(placeable, ("experiment_key", "system_type")),
        "all_including_synthetic": _segment(all_cards, ("experiment_key", "system_type")),
        "synthetic_excluded": len(all_cards) - len(placeable),
    }


def _live_yankee(db):
    cards = _load_cards(
        db, "live_yankee_cards", "live_yankee_legs", "card_id",
        extra_where="AND assembly_mode!='CROSS_BOOK_SYNTHETIC'",
        group_labels=("algorithm_version",),
    )
    return _segment(cards, ("algorithm_version",))


def _portfolio(db):
    import hr_portfolio_shadow as portfolio
    groups = db.fetchall(
        """SELECT * FROM hr_portfolio_groups
           WHERE policy_version=? AND status='SETTLED'
           ORDER BY id""", (portfolio.VERSION,)
    )
    if not groups:
        return {"version": portfolio.VERSION, "groups": 0, "cohorts": {}, "format_comparison": []}
    ids = [int(r["id"]) for r in groups]
    marks = ",".join("?" for _ in ids)
    legs = db.fetchall(f"""SELECT * FROM hr_portfolio_legs
                          WHERE group_id IN ({marks})
                          ORDER BY group_id,leg_order,id""", tuple(ids))
    fmts = db.fetchall(f"""SELECT * FROM hr_portfolio_formats
                          WHERE group_id IN ({marks}) ORDER BY group_id,format""", tuple(ids))
    by_legs, by_format = defaultdict(list), defaultdict(list)
    for l in legs:
        by_legs[int(l["group_id"])].append(dict(l))
    for f in fmts:
        by_format[int(f["group_id"])].append(dict(f))

    cards = []
    by_cohort = defaultdict(list)
    format_totals = defaultdict(list)
    for group in groups:
        gid = int(group["id"])
        gs = by_legs[gid]
        active = [f for f in by_format[gid] if f.get("status") == "SETTLED"]
        if not active or any(_leg_pnl(x) is None for x in gs):
            continue
        multiples = [
            f for f in active if f["format"] in ("DOUBLE", "TRIXIE", "YANKEE")
        ]
        if not multiples:
            continue
        normalized = sum(float(f["allocation_pct"]) * float(f["pnl_units"])
                         for f in multiples)
        item = {"labels": (str(group["cohort"]),),
                "multiple_pnl": normalized, "legs": gs}
        cards.append(item)
        by_cohort[str(group["cohort"])].append(item)
        for f in multiples:
            limit = {"DOUBLE": 2, "TRIXIE": 3, "YANKEE": 4}[str(f["format"])]
            same_legs = gs[:limit]
            if len(same_legs) != limit:
                continue
            single_roi = sum(_leg_pnl(l) for l in same_legs) / limit
            format_totals[(str(group["cohort"]),str(f["format"]))].append(
                (float(f["pnl_units"]), single_roi)
            )

    formats = []
    for key, samples in sorted(format_totals.items()):
        n = len(samples)
        formats.append({
            "cohort": key[0], "format": key[1], "settled_groups": n,
            "multiples_roi_pct": round(100 * sum(v[0] for v in samples)/n, 2),
            "same_group_singles_roi_pct":
                round(100 * sum(v[1] for v in samples)/n, 2),
        })
    return {
        "version": portfolio.VERSION,
        "groups": len(cards),
        "overall": _summary(cards),
        "cohorts": {name: _summary(items) for name, items in sorted(by_cohort.items())},
        "format_comparison": formats,
    }


def _quality(db):
    pairs = db.fetchall("""SELECT * FROM hr_quality_pairs WHERE status='SETTLED'
                          ORDER BY baseline_group_id""")
    if not pairs:
        return {"paired_groups": 0, "benchmark": _summary([])}
    ids = [int(p["baseline_group_id"]) for p in pairs]
    marks = ",".join("?" for _ in ids)
    rows = db.fetchall(f"""SELECT * FROM hr_quality_legs
                         WHERE baseline_group_id IN ({marks})
                         ORDER BY baseline_group_id,leg_order,id""", tuple(ids))
    groups = defaultdict(list)
    for leg in rows:
        groups[int(leg["baseline_group_id"])].append(dict(leg))
    items = [
        {"labels": ("QUALITY",), "multiple_pnl": float(p["quality_pnl_units"]),
         "legs": groups[int(p["baseline_group_id"])]}
        for p in pairs
    ]
    return {"paired_groups": len(items), "benchmark": _summary(items)}


def report(db):
    out = {"version": VERSION, "read_only": True, "new_api_calls": 0}
    for title, fn in (
        ("hit_rate", _hit_rate), ("live_yankee", _live_yankee),
        ("hr_portfolio", _portfolio), ("quality_challenger", _quality),
    ):
        try:
            out[title] = fn(db)
        except Exception as exc:
            out[title] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def install(app, db):
    from fastapi.responses import JSONResponse
    @app.get("/api/singles-vs-multiples/roi")
    def singles_vs_multiples_roi():
        return JSONResponse(report(db))
    audit = report(db)
    print("SINGLES_VS_MULTIPLES_AUDIT " + json.dumps(audit, sort_keys=True, default=str), flush=True)
    return {"installed": True, "version": VERSION, "no_provider_calls": True,
            "no_strategy_changes": True}
