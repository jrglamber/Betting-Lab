from __future__ import annotations

"""Make HR1/HR2 forward multiples placeable and notify Telegram.

Forward assembly policy:
- HR1 and HR2 are both covered.
- Yankees (4 legs) and Heinz (6 legs) are both covered.
- The whole price-ready queue is searched bookmaker-first.
- Fully qualifying one-book cards are preferred.
- The frozen bounded same-book price compromise is allowed when needed.
- If no same-book card is possible, candidates remain queued.
- No new CROSS_BOOK_SYNTHETIC card is created.

Telegram policy:
- Every new placeable HR1/HR2 Yankee or Heinz created after this patch is
  deployed is sent through the existing Betting Lab Telegram bot.
- Historical cards are marked PRE_WIRE and are not replayed/spammed.
- Failed sends are retried on later maintenance cycles (up to five attempts).
- No bookmaker bet is placed automatically; Telegram is a manual bet-ready
  notification only.
"""

from math import prod
from typing import Any, Dict, Mapping, Sequence

import hit_rate_multiples_lab as hr
import web as web_module
from db import sanitize_sensitive_text, utc_now_iso
from telegram_alerts import _send_telegram

VERSION = "HIT_RATE_SAME_BOOK_ONLY_V2_TELEGRAM"
_INSTALLED = False
_ORIGINAL_CHOOSE = None
_ORIGINAL_FORM = None
_ORIGINAL_MAINTENANCE = None

YANKEE_LINES = 11
HEINZ_LINES = 57


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
                    "anchor": str(state["best"].get("bookmaker_key") or "") == book,
                }
            )

        if len(options) < need or sum(int(x["anchor"]) for x in options) < min_anchor:
            continue

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
    """Defensive replacement for direct callers of the original _choose."""
    picked = _pick_from_ready(db, candidates, len(candidates), now)
    if picked is None:
        return None
    chosen, quotes, mode = picked
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

        for _ in range(3):
            ready = hr._ready(db, policy, exp, system, now)
            if len(ready) < need:
                break
            picked = _pick_from_ready(db, ready, need, now)
            if picked is None:
                blocked += 1
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


def _ensure_telegram_schema(db) -> None:
    """Create a separate notification ledger without mutating frozen card data."""
    db.execute(
        """CREATE TABLE IF NOT EXISTS hit_rate_telegram_state (
            card_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            sent_at TEXT,
            error TEXT,
            created_at TEXT NOT NULL
        )"""
    )


def _seed_existing_cards(db) -> int:
    """Prevent pre-wire historical cards from being replayed to Telegram."""
    _ensure_telegram_schema(db)
    seeded = 0
    for row in db.fetchall("SELECT id FROM hit_rate_cards ORDER BY id"):
        card_id = int(row["id"])
        try:
            seeded += int(
                db.execute(
                    """INSERT INTO hit_rate_telegram_state(card_id,status,attempts,created_at)
                       VALUES(?,?,?,?) ON CONFLICT(card_id) DO NOTHING""",
                    (card_id, "PRE_WIRE", 0, utc_now_iso()),
                )
                > 0
            )
        except Exception:
            pass
    return seeded


def _market_label(leg: Mapping[str, Any]) -> str:
    market = str(leg.get("market_key") or "h2h").lower()
    selection = str(leg.get("selection") or "").strip()
    point = leg.get("point")
    if market == "btts":
        return f"BTTS {selection}".strip()
    if market == "totals":
        if point is not None and str(point) not in selection:
            return f"{selection} {point}".strip()
        return selection
    if market == "h2h":
        return selection
    return f"{market.replace('_', ' ').title()} {selection}".strip()


def _fmt_kickoff(value: Any) -> str:
    dt = hr._parse(value)
    if dt is None:
        return str(value or "—")
    return dt.strftime("%d %b %H:%M UTC")


def _telegram_message(card: Mapping[str, Any], legs: Sequence[Mapping[str, Any]]) -> str:
    exp = str(card.get("experiment_key") or "HR?")
    system = str(card.get("system_type") or "MULTIPLE").upper()
    lines_count = YANKEE_LINES if system == "YANKEE" else HEINZ_LINES
    stake = float(card.get("reference_stake_gbp") or hr.REFERENCE_STAKE_GBP)
    bookmaker = str(legs[0].get("bookmaker_title") or legs[0].get("bookmaker_key") or "—") if legs else "—"
    assembly = str(card.get("assembly_mode") or "SAME_BOOK")

    out = [
        f"🟢 {exp} {system} — BET READY",
        "",
        f"🏦 Bookmaker: {bookmaker}",
        f"✅ Assembly: {assembly}",
        f"💷 Reference stake: £{stake:.2f} total · {lines_count} lines (~£{stake/lines_count:.2f}/line)",
        "",
    ]
    for i, leg in enumerate(legs, 1):
        fixture = f"{leg.get('home_team') or ''} v {leg.get('away_team') or ''}".strip()
        out.extend(
            [
                f"{i}. [{leg.get('source_lane')}] {_market_label(leg)} @ {float(leg.get('entry_odds') or 0):.2f}",
                f"   {fixture} · {_fmt_kickoff(leg.get('commence_time'))}",
            ]
        )
    out.extend(["", "Manual placement only — no automatic bookmaker order has been submitted."])
    return "\n".join(out)


def _send_pending(db, settings, limit: int = 8) -> Dict[str, Any]:
    _ensure_telegram_schema(db)
    enabled = bool(getattr(settings, "telegram_alerts_enabled", False))
    if not enabled:
        return {"enabled": False, "eligible": 0, "sent": 0, "failed": 0, "reason": "disabled"}

    token = str(getattr(settings, "telegram_bot_token", "") or "").strip()
    chat = str(getattr(settings, "telegram_chat_id", "") or "").strip()
    if not token or not chat:
        return {"enabled": True, "eligible": 0, "sent": 0, "failed": 0, "reason": "credentials_missing"}

    cards = db.fetchall(
        """SELECT c.*
           FROM hit_rate_cards c
           LEFT JOIN hit_rate_telegram_state t ON t.card_id=c.id
           WHERE t.card_id IS NULL OR (t.status='FAILED' AND t.attempts < 5)
           ORDER BY c.id
           LIMIT ?""",
        (limit,),
    )

    sent = failed = skipped = 0
    for card in cards:
        card_id = int(card["id"])
        mode = str(card.get("assembly_mode") or "")
        legs = db.fetchall("SELECT * FROM hit_rate_legs WHERE card_id=? ORDER BY leg_order", (card_id,))
        expected = int(hr.SYSTEMS.get(str(card.get("system_type") or ""), {}).get("legs") or 0)

        if not mode.startswith("SAME_BOOK") or len(legs) != expected or expected not in (4, 6):
            db.execute(
                """INSERT INTO hit_rate_telegram_state(card_id,status,attempts,created_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(card_id) DO UPDATE SET status=excluded.status""",
                (card_id, "SKIPPED_NON_PLACEABLE", 0, utc_now_iso()),
            )
            skipped += 1
            continue

        prior = db.fetchone("SELECT * FROM hit_rate_telegram_state WHERE card_id=?", (card_id,)) or {}
        attempts = int(prior.get("attempts") or 0)
        try:
            _send_telegram(token, chat, _telegram_message(card, legs))
            db.execute(
                """INSERT INTO hit_rate_telegram_state(card_id,status,attempts,sent_at,error,created_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(card_id) DO UPDATE SET
                     status=excluded.status,attempts=excluded.attempts,
                     sent_at=excluded.sent_at,error=NULL""",
                (card_id, "SENT", attempts + 1, utc_now_iso(), None, utc_now_iso()),
            )
            sent += 1
        except Exception as exc:
            db.execute(
                """INSERT INTO hit_rate_telegram_state(card_id,status,attempts,error,created_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(card_id) DO UPDATE SET
                     status=excluded.status,attempts=excluded.attempts,error=excluded.error""",
                (card_id, "FAILED", attempts + 1, sanitize_sensitive_text(exc), utc_now_iso()),
            )
            failed += 1

    return {
        "enabled": True,
        "eligible": len(cards),
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
    }


def _maintenance_with_telegram(db, collector, settings, now=None):
    out = _ORIGINAL_MAINTENANCE(db, collector, settings, now)
    try:
        out["telegram"] = _send_pending(db, settings)
    except Exception as exc:
        out["telegram"] = {
            "enabled": bool(getattr(settings, "telegram_alerts_enabled", False)),
            "sent": 0,
            "failed": 1,
            "error": sanitize_sensitive_text(exc),
        }
        print(
            f"HIT_RATE_TELEGRAM_ERROR {type(exc).__name__}: {sanitize_sensitive_text(exc)}",
            flush=True,
        )
    return out


def install() -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_CHOOSE, _ORIGINAL_FORM, _ORIGINAL_MAINTENANCE
    if _INSTALLED:
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
        }

    _ORIGINAL_CHOOSE = hr._choose
    _ORIGINAL_FORM = hr._form
    _ORIGINAL_MAINTENANCE = hr.run_maintenance

    hr._choose = _choose_same_book
    hr._form = _form_same_book

    # Mark every existing card as pre-wire before the maintenance loop can send
    # notifications. Use the shared production web DB explicitly; the HR module
    # itself intentionally does not expose a web_module attribute.
    try:
        prewire_seeded = _seed_existing_cards(web_module.db)
    except Exception as exc:
        prewire_seeded = 0
        print(
            f"HIT_RATE_TELEGRAM_SEED_ERROR {type(exc).__name__}: {sanitize_sensitive_text(exc)}",
            flush=True,
        )

    hr.run_maintenance = _maintenance_with_telegram
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
        "telegram_enabled_for_new_cards": True,
        "telegram_manual_only": True,
        "telegram_prewire_cards_seeded": prewire_seeded,
        "telegram_retry_attempts": 5,
        "thresholds": {
            "min_leg_price_retention": hr.MIN_LEG_PRICE_RETENTION,
            "min_required_odds_retention": hr.MIN_REQUIRED_ODDS_RETENTION,
            "min_card_price_retention": hr.MIN_CARD_PRICE_RETENTION,
        },
    }
