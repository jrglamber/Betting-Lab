from __future__ import annotations

"""Make every HR1/HR2 forward multiple genuinely placeable at one bookmaker.

The original Hit-Rate Multiples Lab already preferred same-book cards, but it
only examined the first N ready candidates and fell back to a
CROSS_BOOK_SYNTHETIC card when that exact group could not be consolidated.
That is useful as a theoretical research construct, but it is not a bet a user
can actually place.

This patch changes forward card assembly only:
- HR1 and HR2 are both covered.
- Yankees (4 legs) and Heinz (6 legs) are both covered.
- The whole price-ready queue is searched bookmaker-first, rather than only the
  first N candidates.
- Fully qualifying one-book cards are preferred.
- If necessary, the existing tightly-bounded price compromise is allowed at a
  single bookmaker (same leg/card retention thresholds as the frozen lab).
- If no same-book card is currently possible, candidates stay queued and keep
  refreshing. A synthetic cross-book card is never created or consumed.

Historical CROSS_BOOK_SYNTHETIC cards are deliberately retained as immutable
research evidence and remain clearly labelled non-placeable on the dashboard.
"""

from collections import defaultdict
from math import prod
from typing import Any, Dict, Mapping, Optional, Sequence

import hit_rate_multiples_lab as hr

VERSION = "HIT_RATE_SAME_BOOK_ONLY_V1"
_INSTALLED = False
_ORIGINAL_CHOOSE = None
_ORIGINAL_FORM = None


def _first_seen(candidate: Mapping[str, Any]) -> str:
    return str(candidate.get("first_seen_at") or candidate.get("source_created_at") or "9999")


def _quote_state(db, ready: Sequence[Mapping[str, Any]], now):
    """Read strict + bounded-compromise quotes once per candidate."""
    states = []
    for raw in ready:
        candidate = dict(raw)
        strict = dict(hr._quotes(db, candidate, now, False))
        if not strict:
            continue
        loose = dict(hr._quotes(db, candidate, now, True))
        best = dict(
            max(
                strict.values(),
                key=lambda q: (float(q.get("price") or 0), str(q.get("bookmaker_key") or "")),
            )
        )
        states.append(
            {
                "candidate": candidate,
                "strict": strict,
                "loose": loose,
                "best": best,
                "best_price": float(best.get("price") or 0),
            }
        )
    return states


def _verified(states, need: int):
    """Find a fully qualifying N-leg card at one bookmaker."""
    books = sorted({str(book) for state in states for book in state["strict"] if str(book)})
    options = []
    for book in books:
        eligible = [state for state in states if book in state["strict"]]
        if len(eligible) < need:
            continue
        # Preserve the lab's queue-first behaviour within a bookmaker. The
        # important change is that we search the whole queue, not just ready[:N].
        chosen = sorted(
            eligible,
            key=lambda state: (
                _first_seen(state["candidate"]),
                int(state["candidate"].get("id") or 0),
            ),
        )[:need]
        candidates = [state["candidate"] for state in chosen]
        quotes = [dict(state["strict"][book]) for state in chosen]
        latest_queue_time = max(_first_seen(candidate) for candidate in candidates)
        card_price = prod(float(quote.get("price") or 0) for quote in quotes)
        options.append((latest_queue_time, -card_price, book, candidates, quotes))

    if not options:
        return None
    options.sort(key=lambda x: (x[0], x[1], x[2]))
    _, _, _, candidates, quotes = options[0]
    return candidates, quotes, "SAME_BOOK_VERIFIED"


def _consolidated(states, need: int):
    """Find the strongest bounded-compromise N-leg card at one bookmaker."""
    min_anchor = 2 if need == 4 else 3
    books = sorted({str(book) for state in states for book in state["loose"] if str(book)})
    cards = []

    for book in books:
        options = []
        for state in states:
            alt = state["loose"].get(book)
            if alt is None:
                continue
            alt = dict(alt)
            alt_price = float(alt.get("price") or 0)
            best_price = float(state["best_price"] or 0)
            min_odds = float(state["candidate"].get("min_odds") or 0)
            if alt_price <= 1 or best_price <= 1:
                continue

            price_retention = alt_price / best_price
            required_retention = alt_price / min_odds if min_odds > 1 else 1.0
            if price_retention + 1e-12 < hr.MIN_LEG_PRICE_RETENTION:
                continue
            if required_retention + 1e-12 < hr.MIN_REQUIRED_ODDS_RETENTION:
                continue

            options.append(
                {
                    "candidate": state["candidate"],
                    "quote": alt,
                    "price_retention": price_retention,
                    # Match the frozen lab's anchor definition: the target book
                    # is the candidate's best strict price, not merely available.
                    "anchor": str(state["best"].get("bookmaker_key") or "") == book,
                }
            )

        if len(options) < need or sum(int(x["anchor"]) for x in options) < min_anchor:
            continue

        # Start with the least-compromised N legs. If that set lacks the lab's
        # required same-book anchors, replace the weakest non-anchors with the
        # strongest unused anchors. This maximises retained card price subject
        # to the frozen anchor requirement without a combinatorial search.
        ranked = sorted(
            options,
            key=lambda x: (
                float(x["price_retention"]),
                int(bool(x["anchor"])),
                float(x["quote"].get("price") or 0),
            ),
            reverse=True,
        )
        chosen = list(ranked[:need])
        anchors = sum(int(x["anchor"]) for x in chosen)
        if anchors < min_anchor:
            missing = min_anchor - anchors
            unused_anchors = [x for x in ranked[need:] if x["anchor"]]
            replaceable = sorted(
                [x for x in chosen if not x["anchor"]],
                key=lambda x: float(x["price_retention"]),
            )
            if len(unused_anchors) < missing or len(replaceable) < missing:
                continue
            for replacement, victim in zip(unused_anchors[:missing], replaceable[:missing]):
                chosen[chosen.index(victim)] = replacement

        anchor_count = sum(int(x["anchor"]) for x in chosen)
        combined_retention = prod(float(x["price_retention"]) for x in chosen)
        if anchor_count < min_anchor or combined_retention + 1e-12 < hr.MIN_CARD_PRICE_RETENTION:
            continue

        chosen.sort(
            key=lambda x: (
                _first_seen(x["candidate"]),
                int(x["candidate"].get("id") or 0),
            )
        )
        candidates = [x["candidate"] for x in chosen]
        quotes = [x["quote"] for x in chosen]
        latest_queue_time = max(_first_seen(candidate) for candidate in candidates)
        card_price = prod(float(quote.get("price") or 0) for quote in quotes)
        cards.append(
            (
                -combined_retention,
                -anchor_count,
                latest_queue_time,
                -card_price,
                book,
                candidates,
                quotes,
            )
        )

    if not cards:
        return None
    cards.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]))
    _, _, _, _, _, candidates, quotes = cards[0]
    return candidates, quotes, "SAME_BOOK_CONSOLIDATED"


def _pick_from_ready(db, ready: Sequence[Mapping[str, Any]], need: int, now):
    states = _quote_state(db, ready, now)
    if len(states) < need:
        return None
    verified = _verified(states, need)
    if verified is not None:
        return verified
    return _consolidated(states, need)


def _choose_same_book(db, candidates: Sequence[Mapping[str, Any]], now):
    """Defensive replacement for any direct callers of the original _choose."""
    picked = _pick_from_ready(db, candidates, len(candidates), now)
    if picked is None:
        return None
    chosen, quotes, mode = picked
    # A direct _choose caller supplied an exact candidate set, so never silently
    # substitute a different set. _form_same_book is responsible for whole-queue
    # candidate selection.
    supplied = [int(x.get("id") or 0) for x in candidates]
    selected = [int(x.get("id") or 0) for x in chosen]
    if supplied != selected:
        return None
    return quotes, mode


def _form_same_book(db, now) -> Dict[str, int]:
    formed = verified = consolidated = blocked = 0
    streams = db.fetchall(
        """SELECT DISTINCT policy_version,experiment_key,system_type
           FROM hit_rate_candidates WHERE status='QUEUED'
           ORDER BY policy_version,experiment_key,system_type"""
    )
    for stream in streams:
        policy = str(stream["policy_version"])
        exp = str(stream["experiment_key"])
        system = str(stream["system_type"])
        need = int(hr.SYSTEMS[system]["legs"])

        # Keep the existing maximum of three new cards per stream/cycle. Each
        # iteration recomputes the queue after consumed legs are removed.
        for _ in range(3):
            ready = hr._ready(db, policy, exp, system, now)
            if len(ready) < need:
                break
            picked = _pick_from_ready(db, ready, need, now)
            if picked is None:
                blocked += 1
                # Crucial policy: do not consume candidates into a theoretical
                # cross-book card. Leave them queued for the next quote refresh.
                break
            candidates, quotes, mode = picked
            if not hr._make_card(db, policy, exp, system, candidates, quotes, mode):
                break
            formed += 1
            verified += int(mode == "SAME_BOOK_VERIFIED")
            consolidated += int(mode == "SAME_BOOK_CONSOLIDATED")

    return {
        "formed": formed,
        "same_book_verified": verified,
        "same_book_consolidated": consolidated,
        "cross_book": 0,
        "blocked_waiting_same_book": blocked,
    }


def install() -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_CHOOSE, _ORIGINAL_FORM
    if _INSTALLED:
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
        }

    _ORIGINAL_CHOOSE = hr._choose
    _ORIGINAL_FORM = hr._form
    hr._choose = _choose_same_book
    hr._form = _form_same_book
    _INSTALLED = True

    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "experiments": ["HR1", "HR2"],
        "systems": ["YANKEE", "HEINZ"],
        "assembly_policy": "SAME_BOOK_ONLY",
        "whole_queue_bookmaker_first": True,
        "bounded_consolidation": True,
        "cross_book_forward_enabled": False,
        "historical_synthetics_retained": True,
        "thresholds": {
            "min_leg_price_retention": hr.MIN_LEG_PRICE_RETENTION,
            "min_required_odds_retention": hr.MIN_REQUIRED_ODDS_RETENTION,
            "min_card_price_retention": hr.MIN_CARD_PRICE_RETENTION,
        },
    }
