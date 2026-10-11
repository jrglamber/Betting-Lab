from __future__ import annotations

"""Prefer a genuinely placeable same-book Yankee when cross-book prices can be
consolidated with only a small, explicit price sacrifice.

This does not broaden the frozen source pool or auto-place bets. It only changes
how four already-qualified Yankee candidates are priced across the supported
manual books.
"""

from datetime import timedelta
from math import prod
from typing import Any, Dict, Mapping, Optional

import live_yankee_feed as feed
import web as web_module

# A bookmaker is only a consolidation candidate when it already owns at least
# two of the four best cross-book leg prices. Replacement prices must remain
# close to the cross-book best and close to the source lane's minimum price.
MIN_ANCHOR_LEGS = 2
MIN_LEG_PRICE_RETENTION = 0.975       # <=2.5% loss vs best current leg price
MIN_REQUIRED_ODDS_RETENTION = 0.985  # <=1.5% below the source minimum odds
MIN_CARD_PRICE_RETENTION = 0.95       # <=5% loss in combined four-leg price

_INSTALLED = False
_ORIGINAL_PICK_FOUR = None
_ORIGINAL_FORM = None
_ORIGINAL_MESSAGE = None
_ORIGINAL_SCOREBOARD = None
_ORIGINAL_PAGE_HTML = None


def _fresh_quote_for_book(
    db,
    candidate: Mapping[str, Any],
    bookmaker_key: str,
    now,
) -> Optional[Dict[str, Any]]:
    """Latest fresh exact-market quote, even if just below the source min odds."""
    rows = db.fetchall(
        """SELECT * FROM live_yankee_quotes
           WHERE event_id=? AND market_key=? AND selection=? AND bookmaker_key=?
           ORDER BY captured_at DESC,id DESC LIMIT 200""",
        (
            candidate["event_id"],
            candidate["market_key"],
            candidate["selection"],
            bookmaker_key,
        ),
    )
    cutoff = now - timedelta(minutes=feed.QUOTE_FRESHNESS_MINUTES)
    source_at = feed._parse_iso(candidate.get("source_created_at"))
    for raw in rows:
        row = dict(raw)
        if not feed._same_point(row.get("point"), candidate.get("point")):
            continue
        ts = feed._parse_iso(row.get("captured_at"))
        try:
            price = float(row.get("price") or 0)
        except Exception:
            continue
        if ts is None or ts < cutoff or ts > now or (source_at and ts < source_at):
            continue
        if price <= 1:
            continue
        # Preserve the NCAAF research pocket itself even when allowing a tiny
        # source-minimum price miss for consolidation.
        if candidate.get("source_lane") == "NCAAF_3_TO_4_99" and not (3.0 <= price < 5.0):
            continue
        row["quote_age_minutes"] = max(0.0, (now - ts).total_seconds() / 60.0)
        return row
    return None


def _consolidated_pick(db, now):
    """Wrap the frozen selector and rescue cross-book cards onto one book."""
    picked = _ORIGINAL_PICK_FOUR(db, now)
    if not picked:
        return None
    candidates, synthetic_quotes, mode = picked
    if mode != "CROSS_BOOK_SYNTHETIC":
        return picked

    anchor_counts: Dict[str, int] = {}
    for quote in synthetic_quotes:
        book = str(quote.get("bookmaker_key") or "")
        if book:
            anchor_counts[book] = anchor_counts.get(book, 0) + 1

    synthetic_prices = [float(q.get("price") or 0) for q in synthetic_quotes]
    synthetic_product = prod(synthetic_prices)
    if synthetic_product <= 0:
        return picked

    options = []
    for book, anchor_count in anchor_counts.items():
        if anchor_count < MIN_ANCHOR_LEGS:
            continue
        proposed = []
        leg_retentions = []
        valid = True
        for candidate, best_quote, best_price in zip(candidates, synthetic_quotes, synthetic_prices):
            # First use the already-qualified quote if this bookmaker had one.
            qualified = feed._valid_quotes(db, candidate, now).get(book)
            alt = dict(qualified) if qualified is not None else _fresh_quote_for_book(db, candidate, book, now)
            if alt is None:
                valid = False
                break
            alt_price = float(alt.get("price") or 0)
            if alt_price <= 1 or best_price <= 1:
                valid = False
                break
            price_retention = alt_price / best_price
            min_odds = float(candidate.get("min_odds") or 0)
            required_retention = (alt_price / min_odds) if min_odds > 1 else 1.0
            if price_retention + 1e-12 < MIN_LEG_PRICE_RETENTION:
                valid = False
                break
            if required_retention + 1e-12 < MIN_REQUIRED_ODDS_RETENTION:
                valid = False
                break
            proposed.append(alt)
            leg_retentions.append(price_retention)
        if not valid or len(proposed) != 4:
            continue
        combined_retention = prod(float(q["price"]) for q in proposed) / synthetic_product
        if combined_retention + 1e-12 < MIN_CARD_PRICE_RETENTION:
            continue
        options.append(
            (
                anchor_count,
                combined_retention,
                min(leg_retentions),
                prod(float(q["price"]) for q in proposed),
                book,
                proposed,
            )
        )

    if not options:
        return picked

    # Prefer the bookmaker already carrying the most best-price legs, then the
    # smallest total price sacrifice, then the strongest worst individual leg.
    options.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]), reverse=True)
    _, _, _, _, _, proposed = options[0]
    return candidates, proposed, "SAME_BOOK_CONSOLIDATED"


def _form(db, now, max_cards: int = 5):
    before = db.fetchone(
        "SELECT COUNT(*) AS n FROM live_yankee_cards WHERE assembly_mode='SAME_BOOK_CONSOLIDATED'"
    ) or {}
    before_n = int(before.get("n") or 0)
    out = dict(_ORIGINAL_FORM(db, now, max_cards=max_cards))
    after = db.fetchone(
        "SELECT COUNT(*) AS n FROM live_yankee_cards WHERE assembly_mode='SAME_BOOK_CONSOLIDATED'"
    ) or {}
    delta = max(0, int(after.get("n") or 0) - before_n)
    out["same_book"] = int(out.get("same_book") or 0) + delta
    out["same_book_consolidated"] = delta
    return out


def _message(card, legs):
    mode = str(card.get("assembly_mode") or "")
    if mode != "SAME_BOOK_CONSOLIDATED":
        return _ORIGINAL_MESSAGE(card, legs)
    shadow = dict(card)
    shadow["assembly_mode"] = "SAME_BOOK_VERIFIED"
    text = _ORIGINAL_MESSAGE(shadow, legs)
    text = text.replace(
        "🟢 YANKEE FEED — SAME-BOOK VERIFIED",
        "🟡 YANKEE FEED — SAME-BOOK CONSOLIDATED",
        1,
    )
    text = text.replace(
        "VERIFY ALL FOUR PRICES/MARKETS BEFORE PLACING.",
        "Consolidated onto one bookmaker within the live price-tolerance guardrail.\n"
        "VERIFY ALL FOUR PRICES/MARKETS BEFORE PLACING.",
        1,
    )
    return text


def _scoreboard(db):
    out = dict(_ORIGINAL_SCOREBOARD(db))
    row = db.fetchone(
        """SELECT
             SUM(CASE WHEN assembly_mode='SAME_BOOK_VERIFIED' THEN 1 ELSE 0 END) AS verified,
             SUM(CASE WHEN assembly_mode='SAME_BOOK_CONSOLIDATED' THEN 1 ELSE 0 END) AS consolidated,
             SUM(CASE WHEN assembly_mode='CROSS_BOOK_SYNTHETIC' THEN 1 ELSE 0 END) AS cross_book
           FROM live_yankee_cards"""
    ) or {}
    verified = int(row.get("verified") or 0)
    consolidated = int(row.get("consolidated") or 0)
    out["same_book_verified"] = verified
    out["same_book_consolidated"] = consolidated
    out["same_book"] = verified + consolidated
    out["cross_book"] = int(row.get("cross_book") or 0)
    out["consolidation_policy"] = {
        "min_anchor_legs": MIN_ANCHOR_LEGS,
        "min_leg_price_retention_pct": MIN_LEG_PRICE_RETENTION * 100.0,
        "min_source_min_odds_retention_pct": MIN_REQUIRED_ODDS_RETENTION * 100.0,
        "min_card_price_retention_pct": MIN_CARD_PRICE_RETENTION * 100.0,
    }
    return out


def _page_html():
    html = _ORIGINAL_PAGE_HTML()
    old = (
        "A common qualifying bookmaker is preferred; otherwise the best fresh quote per leg is used."
    )
    new = (
        "A common qualifying bookmaker is preferred. If the best prices are split, the feed next tries to "
        "consolidate onto a bookmaker already carrying at least two best-price legs, but only inside the "
        "tight price-retention guardrail. Only after that does it emit a cross-book synthetic card."
    )
    return html.replace(old, new, 1)


def install() -> dict:
    global _INSTALLED, _ORIGINAL_PICK_FOUR, _ORIGINAL_FORM, _ORIGINAL_MESSAGE, _ORIGINAL_SCOREBOARD, _ORIGINAL_PAGE_HTML
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": "YANKEE_CONSOLIDATION_V1"}

    _ORIGINAL_PICK_FOUR = feed._pick_four
    _ORIGINAL_FORM = feed._form
    _ORIGINAL_MESSAGE = feed._message
    _ORIGINAL_SCOREBOARD = feed.scoreboard
    _ORIGINAL_PAGE_HTML = feed._page_html

    feed._pick_four = _consolidated_pick
    feed._form = _form
    feed._message = _message
    feed.scoreboard = _scoreboard
    feed._page_html = _page_html

    web_module.VERSION = "0.19.40"
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": "YANKEE_CONSOLIDATION_V1",
        "min_anchor_legs": MIN_ANCHOR_LEGS,
        "min_leg_price_retention_pct": MIN_LEG_PRICE_RETENTION * 100.0,
        "min_required_odds_retention_pct": MIN_REQUIRED_ODDS_RETENTION * 100.0,
        "min_card_price_retention_pct": MIN_CARD_PRICE_RETENTION * 100.0,
        "dashboard_version": web_module.VERSION,
    }
