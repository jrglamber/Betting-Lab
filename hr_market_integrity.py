from __future__ import annotations

"""Shared, conservative football/tennis/multisport fixture and market identity.

Prospective HR portfolio cards must not contain two selections from the
same underlying fixture even if different upstream feeds use different IDs.
A total/handicap is not manually actionable without its numeric line.
Do not normalize team names aggressively: false fixture merges also matter.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
import unicodedata
from typing import Any, Mapping

LINE_MARKETS = frozenset({
    "totals", "total", "over_under", "alternate_totals",
    "spreads", "spread", "handicap", "asian_handicap",
})


def _normal(value: Any) -> str:
    cleaned = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.findall(r"\w+", cleaned, flags=re.UNICODE))


def _clock(value: Any) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return ""


def fixture_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Conservatively combine same team names, sport and UTC kickoff minute.

    If a fixture cannot be reliably normalized, use the event ID instead.
    Avoid matching MLB double-headers played at different kickoff times.
    """
    sport = _normal(row.get("sport_key"))
    home = _normal(row.get("home_team"))
    away = _normal(row.get("away_team"))
    kickoff = _clock(row.get("commence_time"))
    if sport and home and away and kickoff:
        return ("fixture", sport, home, away, kickoff)
    return ("event", sport, _normal(row.get("event_id")))


def line_value(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return ""
    try:
        n = Decimal(str(value))
        return format(n.normalize(), "f") if n.is_finite() else ""
    except (ValueError, InvalidOperation):
        return ""


def needs_line(row: Mapping[str, Any]) -> bool:
    return _normal(row.get("market_key")) in LINE_MARKETS


def market_actionable(row: Mapping[str, Any]) -> bool:
    return not needs_line(row) or bool(line_value(row.get("point")))


def market_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    market = _normal(row.get("market_key"))
    if market in {"total", "over under", "alternate totals"}:
        market = "totals"
    if market in {"spreads", "asian handicap"}:
        market = "handicap"
    return (market, _normal(row.get("selection")), line_value(row.get("point")))


def selection_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    return fixture_key(row) + market_key(row)


def market_label(row: Mapping[str, Any]) -> str:
    market = _normal(row.get("market_key"))
    sel = str(row.get("selection") or "").strip() or "—"
    line = line_value(row.get("point"))
    if needs_line(row):
        if not line:
            return f"{sel} — LINE MISSING"
        return f"{sel} {line}"
    if market in ("btts", "both teams to score"):
        return f"BTTS {sel}"
    if market == "h2h":
        return f"Winner: {sel}"
    return f"{market or 'Market'}: {sel}"


def opposing_options(rows: list[Mapping[str, Any]]) -> bool:
    """Opposing choices in one *identical* market and point.

    Over 2.5 vs Under 3.5 are not automatically contradictory:
    both can win. Other market families are not compared.
    """
    by_market = {}
    for r in rows:
        m, choice, point = market_key(r)
        if m in {"totals", "btts", "h2h", "handicap"}:
            by_market.setdefault((m, point), set()).add(choice)
    return any(len(options) > 1 for options in by_market.values())

def conflicted_fixture_keys(rows: list[Mapping[str, Any]]) -> set[tuple[str, ...]]:
    """Exclude only fixtures with directly opposing *actionable* options.

    This is a prospective research/alert abstention, not a deletion of raw
    Over/Under predictions. A totals line difference is not a contradiction.
    """
    by_fixture: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
    for row in rows:
        if not market_actionable(row):
            continue
        by_fixture.setdefault(fixture_key(row), []).append(row)
    return {fixture for fixture, candidates in by_fixture.items()
            if opposing_options(candidates)}
