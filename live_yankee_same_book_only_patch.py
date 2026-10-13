from __future__ import annotations

"""Make the live Yankee feed genuinely placeable: same-book only.

The assembler searches the whole queued pool bookmaker-first instead of taking
an arbitrary first four. It will form a card only when four distinct events can
be placed at one supported bookmaker, either at fully qualifying prices or via
the existing tightly-bounded consolidation tolerance. Cross-book synthetic
cards are never created and therefore never consume queue candidates.

On install, any legacy CROSS_BOOK_SYNTHETIC cards are removed and candidates
consumed only by those cards are returned to QUEUED when still actionable (or
expired normally when their event is already too close/past).
"""

from datetime import datetime, timezone
from itertools import combinations
import json
from math import prod
from typing import Any, Dict, Mapping, Optional, Sequence

import live_yankee_feed as feed
import live_yankee_consolidation_patch as consolidation
import live_yankee_expiry_fallback_patch as expiry
import web as web_module
from db import utc_now_iso

VERSION = "YANKEE_SAME_BOOK_ONLY_V1"
MAX_CONSOLIDATION_EVENT_POOL = 28

_INSTALLED = False
_ORIGINAL_SCOREBOARD = None
_ORIGINAL_PAGE = None


def _book_universe() -> tuple[str, ...]:
    configured = getattr(web_module.settings, "manual_systems_placeable_bookmaker_keys", None)
    books = configured or getattr(feed, "DEFAULT_PLACEABLE_BOOKS", ())
    return tuple(dict.fromkeys(str(x) for x in books if str(x)))


def _queue_entries(db, now: datetime):
    out = []
    for raw in db.fetchall(
        "SELECT * FROM live_yankee_candidates WHERE status='QUEUED' ORDER BY first_seen_at,id"
    ):
        candidate = dict(raw)
        if not feed._actionable(candidate.get("commence_time"), now):
            continue
        quotes = dict(feed._valid_quotes(db, candidate, now))
        if not quotes:
            continue
        out.append((candidate, quotes))
    return out


def _first_seen(candidate: Mapping[str, Any]) -> str:
    return str(candidate.get("first_seen_at") or candidate.get("source_created_at") or "9999")


def _best_verified(entries):
    """Find four distinct queued events that all qualify at one bookmaker."""
    options = []
    books = set(_book_universe())
    for _, quote_map in entries:
        books.update(str(x) for x in quote_map if str(x))

    for book in sorted(books):
        by_event: Dict[str, tuple[Dict[str, Any], Dict[str, Any]]] = {}
        for candidate, quote_map in entries:
            quote = quote_map.get(book)
            if quote is None:
                continue
            eid = str(candidate.get("event_id") or "")
            if not eid:
                continue
            proposal = (candidate, dict(quote))
            previous = by_event.get(eid)
            if previous is None:
                by_event[eid] = proposal
                continue
            # Within one event, favour the older queued opportunity; use the
            # stronger price only as a tie-breaker.
            old_key = (_first_seen(previous[0]), -float(previous[1].get("price") or 0), int(previous[0].get("id") or 0))
            new_key = (_first_seen(candidate), -float(quote.get("price") or 0), int(candidate.get("id") or 0))
            if new_key < old_key:
                by_event[eid] = proposal

        event_rows = sorted(
            by_event.values(),
            key=lambda item: (_first_seen(item[0]), int(item[0].get("id") or 0)),
        )
        if len(event_rows) < 4:
            continue
        chosen = event_rows[:4]
        candidates = [x[0] for x in chosen]
        quotes = [x[1] for x in chosen]
        card_price = prod(float(x.get("price") or 0) for x in quotes)
        latest_queue_time = max(_first_seen(x) for x in candidates)
        options.append((latest_queue_time, -card_price, book, candidates, quotes))

    if not options:
        return None
    options.sort(key=lambda x: (x[0], x[1], x[2]))
    _, _, _, candidates, quotes = options[0]
    return candidates, quotes, "SAME_BOOK_VERIFIED"


def _consolidation_option(db, candidate, quote_map, book: str, now: datetime):
    best_quote = max(
        quote_map.values(),
        key=lambda x: (float(x.get("price") or 0), str(x.get("bookmaker_key") or "")),
    )
    best_price = float(best_quote.get("price") or 0)
    if best_price <= 1:
        return None

    qualified = quote_map.get(book)
    alt = dict(qualified) if qualified is not None else consolidation._fresh_quote_for_book(db, candidate, book, now)
    if alt is None:
        return None
    alt_price = float(alt.get("price") or 0)
    if alt_price <= 1:
        return None

    price_retention = alt_price / best_price
    min_odds = float(candidate.get("min_odds") or 0)
    required_retention = alt_price / min_odds if min_odds > 1 else 1.0
    if price_retention + 1e-12 < consolidation.MIN_LEG_PRICE_RETENTION:
        return None
    if required_retention + 1e-12 < consolidation.MIN_REQUIRED_ODDS_RETENTION:
        return None

    return {
        "candidate": candidate,
        "quote": alt,
        "best_price": best_price,
        "price_retention": price_retention,
        "anchor": str(best_quote.get("bookmaker_key") or "") == book,
    }


def _best_consolidated(db, entries, now: datetime):
    """Search the whole queue for a four-event same-book consolidation."""
    books = set(_book_universe())
    for _, quote_map in entries:
        books.update(str(x) for x in quote_map if str(x))

    cards = []
    for book in sorted(books):
        by_event: Dict[str, Dict[str, Any]] = {}
        for candidate, quote_map in entries:
            option = _consolidation_option(db, candidate, quote_map, book, now)
            if option is None:
                continue
            eid = str(candidate.get("event_id") or "")
            if not eid:
                continue
            previous = by_event.get(eid)
            if previous is None:
                by_event[eid] = option
                continue
            # For the same event prefer a best-price anchor, then the least
            # sacrifice, then the older queued candidate.
            old_key = (
                int(bool(previous["anchor"])),
                float(previous["price_retention"]),
                float(previous["quote"].get("price") or 0),
                -int(previous["candidate"].get("id") or 0),
            )
            new_key = (
                int(bool(option["anchor"])),
                float(option["price_retention"]),
                float(option["quote"].get("price") or 0),
                -int(option["candidate"].get("id") or 0),
            )
            if new_key > old_key:
                by_event[eid] = option

        event_options = list(by_event.values())
        if len(event_options) < 4:
            continue

        # Keep combinatorics bounded while retaining both the oldest queue
        # entries and the strongest consolidation options.
        oldest = sorted(
            event_options,
            key=lambda x: (_first_seen(x["candidate"]), int(x["candidate"].get("id") or 0)),
        )[:MAX_CONSOLIDATION_EVENT_POOL]
        strongest = sorted(
            event_options,
            key=lambda x: (int(bool(x["anchor"])), float(x["price_retention"])),
            reverse=True,
        )[:MAX_CONSOLIDATION_EVENT_POOL]
        pool_by_event = {}
        for option in oldest + strongest:
            pool_by_event[str(option["candidate"].get("event_id") or "")] = option
        pool = list(pool_by_event.values())[:MAX_CONSOLIDATION_EVENT_POOL]

        for combo in combinations(pool, 4):
            anchor_count = sum(1 for x in combo if x["anchor"])
            if anchor_count < consolidation.MIN_ANCHOR_LEGS:
                continue
            combined_retention = prod(float(x["price_retention"]) for x in combo)
            if combined_retention + 1e-12 < consolidation.MIN_CARD_PRICE_RETENTION:
                continue
            candidates = [x["candidate"] for x in combo]
            quotes = [x["quote"] for x in combo]
            latest_queue_time = max(_first_seen(x) for x in candidates)
            card_price = prod(float(x.get("price") or 0) for x in quotes)
            cards.append(
                (
                    latest_queue_time,
                    -anchor_count,
                    -combined_retention,
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
    # Keep leg ordering deterministic/queue-oriented for Telegram and audit.
    paired = sorted(zip(candidates, quotes), key=lambda x: (_first_seen(x[0]), int(x[0].get("id") or 0)))
    return [x[0] for x in paired], [x[1] for x in paired], "SAME_BOOK_CONSOLIDATED"


def _pick_four(db, now: datetime):
    entries = _queue_entries(db, now)
    if len({str(c.get("event_id") or "") for c, _ in entries}) < 4:
        return None
    verified = _best_verified(entries)
    if verified is not None:
        return verified
    consolidated = _best_consolidated(db, entries, now)
    if consolidated is not None:
        return consolidated
    # Critical policy: no synthetic card and no candidate consumption. The
    # queue simply keeps refreshing until a same-book card becomes placeable or
    # candidates expire under the normal rules.
    return None


def _expiry_choose_quotes(db, candidates, quote_maps, now: datetime):
    common = set(quote_maps[0]) if quote_maps else set()
    for q in quote_maps[1:]:
        common.intersection_update(q)
    if common:
        book = max(
            common,
            key=lambda b: (prod(float(q[b]["price"]) for q in quote_maps), b),
        )
        return [dict(q[book]) for q in quote_maps], "SAME_BOOK_VERIFIED"

    if not candidates:
        return None, None
    synthetic_best = [
        max(q.values(), key=lambda x: (float(x.get("price") or 0), str(x.get("bookmaker_key") or "")))
        for q in quote_maps
    ]
    books = set(_book_universe())
    for q in quote_maps:
        books.update(str(x) for x in q if str(x))

    min_anchor = 1 if len(candidates) == 2 else 2
    options = []
    for book in sorted(books):
        proposed = []
        retentions = []
        anchors = 0
        valid = True
        for candidate, quote_map, best_quote in zip(candidates, quote_maps, synthetic_best):
            best_price = float(best_quote.get("price") or 0)
            qualified = quote_map.get(book)
            alt = dict(qualified) if qualified is not None else consolidation._fresh_quote_for_book(db, candidate, book, now)
            if alt is None or best_price <= 1:
                valid = False
                break
            alt_price = float(alt.get("price") or 0)
            min_odds = float(candidate.get("min_odds") or 0)
            price_retention = alt_price / best_price if best_price > 1 else 0
            required_retention = alt_price / min_odds if min_odds > 1 else 1.0
            if price_retention + 1e-12 < consolidation.MIN_LEG_PRICE_RETENTION:
                valid = False
                break
            if required_retention + 1e-12 < consolidation.MIN_REQUIRED_ODDS_RETENTION:
                valid = False
                break
            anchors += int(str(best_quote.get("bookmaker_key") or "") == book)
            proposed.append(alt)
            retentions.append(price_retention)
        if not valid or len(proposed) != len(candidates) or anchors < min_anchor:
            continue
        combined_retention = prod(retentions)
        if combined_retention + 1e-12 < consolidation.MIN_CARD_PRICE_RETENTION:
            continue
        options.append((anchors, combined_retention, prod(float(q["price"]) for q in proposed), book, proposed))

    if not options:
        return None, None
    options.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
    return options[0][4], "SAME_BOOK_CONSOLIDATED"


def _expiry_rescue(db, now: datetime):
    ready = expiry._ready(db, now)
    if len(ready) < 2:
        return {"formed": 0}
    earliest = min(x[2] for x in ready)
    minutes = (earliest - now).total_seconds() / 60.0
    if minutes > expiry.FALLBACK_TRIGGER_MINUTES:
        return {"formed": 0, "minutes_to_earliest": minutes}

    n = 4 if len(ready) >= 4 else 3 if len(ready) == 3 else 2
    system_type = "YANKEE" if n == 4 else "TRIXIE" if n == 3 else "DOUBLE"
    chosen = ready[:n]
    candidates = [x[0] for x in chosen]
    quote_maps = [x[1] for x in chosen]
    quotes, mode = _expiry_choose_quotes(db, candidates, quote_maps, now)
    if not quotes or not mode:
        return {
            "formed": 0,
            "system_type": system_type,
            "reason": "WAITING_FOR_SAME_BOOK",
            "minutes_to_earliest": minutes,
        }
    cid = expiry._create_card(db, candidates, quotes, system_type, mode, "EXPIRY_RESCUE")
    return {
        "formed": 1 if cid else 0,
        "card_id": cid,
        "system_type": system_type,
        "assembly_mode": mode,
        "minutes_to_earliest": minutes,
    }


def _purge_synthetic_cards(db, now: datetime) -> Dict[str, Any]:
    cards = [dict(x) for x in db.fetchall(
        "SELECT * FROM live_yankee_cards WHERE assembly_mode='CROSS_BOOK_SYNTHETIC' ORDER BY id"
    )]
    removed = 0
    requeued = 0
    expired = 0
    candidate_ids = []
    card_ids = []
    for card in cards:
        cid = int(card["id"])
        card_ids.append(cid)
        legs = db.fetchall(
            "SELECT candidate_id FROM live_yankee_legs WHERE card_id=? ORDER BY leg_order",
            (cid,),
        )
        for leg in legs:
            candidate_id = int(leg.get("candidate_id") or 0)
            if not candidate_id:
                continue
            candidate = db.fetchone(
                "SELECT * FROM live_yankee_candidates WHERE id=?",
                (candidate_id,),
            ) or {}
            candidate_ids.append(candidate_id)
            if feed._actionable(candidate.get("commence_time"), now):
                requeued += db.execute(
                    """UPDATE live_yankee_candidates
                       SET status='QUEUED',consumed_card_id=NULL,expired_at=NULL
                       WHERE id=? AND consumed_card_id=?""",
                    (candidate_id, cid),
                )
            else:
                expired += db.execute(
                    """UPDATE live_yankee_candidates
                       SET status='EXPIRED',consumed_card_id=NULL,expired_at=?
                       WHERE id=? AND consumed_card_id=?""",
                    (utc_now_iso(), candidate_id, cid),
                )
        db.execute("DELETE FROM live_yankee_legs WHERE card_id=?", (cid,))
        removed += db.execute("DELETE FROM live_yankee_cards WHERE id=?", (cid,))

    result = {
        "cards_removed": removed,
        "card_ids": card_ids,
        "candidates_requeued": requeued,
        "candidates_expired": expired,
        "candidate_ids": sorted(set(candidate_ids)),
    }
    if cards:
        try:
            db.record_collector_run(
                "LIVE_YANKEE_SYNTHETIC_PURGE",
                True,
                detail=json.dumps(result, sort_keys=True),
            )
        except Exception:
            pass
        print("LIVE_YANKEE_SYNTHETIC_PURGE " + json.dumps(result, sort_keys=True), flush=True)
    return result


def _scoreboard(db):
    out = dict(_ORIGINAL_SCOREBOARD(db))
    now = datetime.now(timezone.utc)
    picked = _pick_four(db, now)
    out["assembly_policy"] = "SAME_BOOK_ONLY"
    out["cross_book_enabled"] = False
    out["placeable_yankee_ready"] = bool(picked)
    out["placeable_yankee_mode"] = picked[2] if picked else None
    out["placeable_yankee_book"] = (
        str(picked[1][0].get("bookmaker_key") or "") if picked and picked[1] else None
    )
    out["cross_book"] = 0
    return out


def _page_html():
    html = _ORIGINAL_PAGE()
    policy = (
        "<div class='panel'><h2>Placement policy</h2>"
        "<div class='ok'><strong>SAME-BOOK ONLY</strong></div>"
        "<div class='muted'>The assembler searches the entire live queue by bookmaker. "
        "It creates and consumes a card only when four distinct events form a genuinely placeable Yankee at one supported bookmaker. "
        "A tightly controlled same-book price consolidation is allowed; cross-book synthetic cards are disabled. "
        "If no same-book combination exists, every candidate stays in the queue and keeps refreshing until it qualifies or expires normally.</div></div>"
    )
    marker = "<div class='panel'><h2>Queue readiness</h2>"
    if marker in html and "SAME-BOOK ONLY" not in html:
        return html.replace(marker, policy + marker, 1)
    if "SAME-BOOK ONLY" not in html:
        return html.replace("</body>", policy + "</body>", 1)
    return html


def install() -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_SCOREBOARD, _ORIGINAL_PAGE
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    # Install after consolidation/source-focus/readiness/expiry/results so this
    # policy is the final authority for card assembly and presentation.
    feed._pick_four = _pick_four
    expiry._choose_quotes = _expiry_choose_quotes
    expiry._expiry_rescue = _expiry_rescue

    _ORIGINAL_SCOREBOARD = feed.scoreboard
    _ORIGINAL_PAGE = feed._page_html
    feed.scoreboard = _scoreboard
    feed._page_html = _page_html

    cleanup = _purge_synthetic_cards(web_module.db, datetime.now(timezone.utc))
    web_module.VERSION = "0.19.55"
    _INSTALLED = True
    result = {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "assembly_policy": "SAME_BOOK_ONLY",
        "cross_book_enabled": False,
        "whole_queue_bookmaker_first": True,
        "consolidation_enabled": True,
        "cleanup": cleanup,
        "dashboard_version": web_module.VERSION,
    }
    print("YANKEE_SAME_BOOK_ONLY " + json.dumps(result, sort_keys=True), flush=True)
    return result
