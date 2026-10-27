from __future__ import annotations

"""Ensure FHG same-book cards are sportsbook-placeable, never exchange-only."""

from typing import Any, Dict

import first_half_goal_hardening_patch as hardening
import first_half_goal_price_capture_v2_patch as price_v2

VERSION = "FHG_SAME_BOOK_SPORTSBOOK_GUARD_V1"
_INSTALLED = False


def _manual_sportsbooks(settings) -> tuple[str, ...]:
    exchange_keys = {"betfair_ex_uk", "matchbook", "smarkets"}
    excluded = set(getattr(price_v2, "_EXCLUDED_KEYS", set())) | exchange_keys
    values: list[str] = []
    for raw in tuple(getattr(settings, "manual_systems_placeable_bookmaker_keys", ()) or ()):
        key = str(raw or "").strip()
        if key and key.lower() not in excluded and key not in values:
            values.append(key)
    return tuple(values)


def install() -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    original = hardening.capture_same_book_cards

    def guarded_capture(web_module, db, now=None):
        original_books = price_v2._accessible_books
        price_v2._accessible_books = _manual_sportsbooks
        try:
            return original(web_module, db, now)
        finally:
            price_v2._accessible_books = original_books

    hardening.capture_same_book_cards = guarded_capture
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "exchange_books_excluded": ["betfair_ex_uk", "matchbook", "smarkets"],
        "book_source": "manual_systems_placeable_bookmaker_keys",
        "betting_authority": False,
    }


__all__ = ["VERSION", "install"]
