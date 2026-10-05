from __future__ import annotations

"""Deterministic offline reconstruction of manually-placeable system bets.

This tool is intentionally research-only. It reads Betting Lab research-export ZIPs
and reconstructs what same-book Yankee / Heinz / Goliath cards could have been
formed from selected candidate pools using bookmaker-specific historical quotes.
It does not place bets and has no runtime execution authority.

Default candidate pools:
  * football outcome-edge selections priced 4.00-4.99
  * PRED4 football selections
  * NFL H2H selections
  * EuroLeague basketball H2H selections

The reconstruction keeps the three systems separate, deduplicates identical picks,
uses at most one selection per event per system card, and requires every leg to be
available at the same approved bookmaker inside the configured quote-spread window.
"""

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from io import TextIOWrapper
import json
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zipfile import ZipFile

APPROVED_BOOKS = ("williamhill", "ladbrokes_uk", "betfred_uk", "boylesports")
SYSTEM_SPECS = {"YANKEE": 4, "HEINZ": 6, "GOLIATH": 8}
DEFAULT_MAX_QUOTE_AGE_MINUTES = 45.0
DEFAULT_MAX_QUOTE_SPREAD_MINUTES = 15.0


@dataclass(frozen=True)
class Candidate:
    pool: str
    family: str
    event_id: str
    sport_key: str
    market_key: str
    selection: str
    point: Optional[float]
    fair_probability: Optional[float]
    source_odds: Optional[float]
    source_time: datetime
    commence_time: Optional[datetime]
    result: Optional[str]

    @property
    def pick_key(self) -> Tuple[str, str, str, Optional[float]]:
        return (self.event_id, self.market_key, self.selection, self.point)


@dataclass(frozen=True)
class Quote:
    event_id: str
    bookmaker: str
    market_key: str
    selection: str
    point: Optional[float]
    captured_at: datetime
    odds: float


@dataclass
class Card:
    system: str
    bookmaker: str
    formed_at: datetime
    legs: List[Tuple[Candidate, Quote]]

    def as_dict(self) -> dict:
        return {
            "system": self.system,
            "bookmaker": self.bookmaker,
            "formed_at": self.formed_at.isoformat(),
            "leg_count": len(self.legs),
            "legs": [
                {
                    "pool": c.pool,
                    "family": c.family,
                    "event_id": c.event_id,
                    "sport_key": c.sport_key,
                    "market_key": c.market_key,
                    "selection": c.selection,
                    "point": c.point,
                    "fair_probability": c.fair_probability,
                    "source_odds": c.source_odds,
                    "bookmaker_odds": q.odds,
                    "quote_at": q.captured_at.isoformat(),
                    "commence_time": c.commence_time.isoformat() if c.commence_time else None,
                    "result": c.result,
                }
                for c, q in self.legs
            ],
        }


def _dt(value: object) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _float(value: object) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _norm_point(value: object) -> Optional[float]:
    val = _float(value)
    return None if val is None else round(val, 8)


def _read_csv(zf: ZipFile, filename: str) -> List[dict]:
    names = set(zf.namelist())
    candidates = (filename, f"tables/{filename}")
    target = next((name for name in candidates if name in names), None)
    if target is None:
        return []
    with zf.open(target) as raw:
        return list(csv.DictReader(TextIOWrapper(raw, encoding="utf-8-sig", newline="")))


def _event_maps(zf: ZipFile) -> Tuple[Dict[str, dict], Dict[str, dict]]:
    football = {str(r.get("event_id") or ""): r for r in _read_csv(zf, "events.csv")}
    multisport = {str(r.get("event_id") or ""): r for r in _read_csv(zf, "multisport_events.csv")}
    return football, multisport


def _candidate(pool: str, family: str, row: Mapping[str, object], event: Mapping[str, object], *, market_override: Optional[str] = None) -> Optional[Candidate]:
    event_id = str(row.get("event_id") or "")
    source_time = _dt(row.get("created_at") or row.get("placed_at") or row.get("evaluated_at"))
    if not event_id or source_time is None:
        return None
    market_key = market_override or str(row.get("market_key") or "h2h")
    selection = str(row.get("selection") or row.get("outcome_name") or "")
    if not selection:
        return None
    sport_key = str(row.get("sport_key") or event.get("sport_key") or "")
    return Candidate(
        pool=pool,
        family=family,
        event_id=event_id,
        sport_key=sport_key,
        market_key=market_key,
        selection=selection,
        point=_norm_point(row.get("point")),
        fair_probability=_float(row.get("fair_probability") or row.get("model_probability")),
        source_odds=_float(row.get("offered_odds") or row.get("entry_odds") or row.get("odds")),
        source_time=source_time,
        commence_time=_dt(event.get("commence_time") or row.get("commence_time")),
        result=str(row.get("result") or "") or None,
    )


def load_candidates(zf: ZipFile) -> List[Candidate]:
    football_events, multisport_events = _event_maps(zf)
    out: List[Candidate] = []

    # Outcome-edge 4.00-4.99 pool from executable football singles. The actual
    # historical source price defines membership in this cohort.
    for row in _read_csv(zf, "execution_shadow_bets.csv"):
        odds = _float(row.get("offered_odds") or row.get("entry_odds"))
        if odds is None or not (4.0 <= odds < 5.0):
            continue
        c = _candidate("FOOTBALL_4.00_4.99", "FOOTBALL", row, football_events.get(str(row.get("event_id") or ""), {}))
        if c:
            out.append(c)

    # PRED4 H2H and derived-market bets.
    for filename in ("football_predictive4_bets.csv", "football_predictive4_market_bets.csv"):
        for row in _read_csv(zf, filename):
            c = _candidate("PRED4", "FOOTBALL", row, football_events.get(str(row.get("event_id") or ""), {}))
            if c:
                out.append(c)

    # NFL / EuroLeague from multisport H2H execution ledger.
    for row in _read_csv(zf, "multisport_execution_bets.csv"):
        sport = str(row.get("sport_key") or "").lower()
        if "nfl" in sport:
            pool = "NFL"
        elif "euroleague" in sport:
            pool = "EUROLEAGUE"
        else:
            continue
        c = _candidate(pool, "MULTISPORT", row, multisport_events.get(str(row.get("event_id") or ""), {}), market_override="h2h")
        if c:
            out.append(c)

    # Exact-pick dedupe across overlapping pools. Keep all pool labels by choosing
    # a stable representative; the card builder only needs one occurrence of a pick.
    dedup: Dict[Tuple[str, str, str, Optional[float]], Candidate] = {}
    rank = {"PRED4": 4, "FOOTBALL_4.00_4.99": 3, "NFL": 2, "EUROLEAGUE": 2}
    for c in sorted(out, key=lambda x: (x.source_time, rank.get(x.pool, 0)), reverse=True):
        dedup.setdefault(c.pick_key, c)
    return sorted(dedup.values(), key=lambda c: c.source_time)


def load_quotes(zf: ZipFile) -> Dict[Tuple[str, str, str, str, Optional[float]], List[Quote]]:
    out: Dict[Tuple[str, str, str, str, Optional[float]], List[Quote]] = {}

    for row in _read_csv(zf, "odds_snapshots.csv"):
        book = str(row.get("bookmaker_key") or "")
        if book not in APPROVED_BOOKS:
            continue
        captured = _dt(row.get("captured_at"))
        odds = _float(row.get("price"))
        event_id = str(row.get("event_id") or "")
        market = str(row.get("market_key") or "")
        selection = str(row.get("outcome_name") or row.get("selection") or "")
        if not (captured and odds and event_id and market and selection):
            continue
        q = Quote(event_id, book, market, selection, _norm_point(row.get("point")), captured, odds)
        out.setdefault((event_id, book, market, selection, q.point), []).append(q)

    for row in _read_csv(zf, "multisport_odds_snapshots.csv"):
        book = str(row.get("bookmaker_key") or "")
        if book not in APPROVED_BOOKS:
            continue
        captured = _dt(row.get("captured_at"))
        odds = _float(row.get("price"))
        event_id = str(row.get("event_id") or "")
        selection = str(row.get("selection") or row.get("outcome_name") or "")
        if not (captured and odds and event_id and selection):
            continue
        q = Quote(event_id, book, "h2h", selection, None, captured, odds)
        out.setdefault((event_id, book, "h2h", selection, None), []).append(q)

    for values in out.values():
        values.sort(key=lambda q: q.captured_at)
    return out


def latest_quote(quotes: Mapping[Tuple[str, str, str, str, Optional[float]], Sequence[Quote]], candidate: Candidate, book: str, decision_time: datetime, max_age_minutes: float) -> Optional[Quote]:
    key = (candidate.event_id, book, candidate.market_key, candidate.selection, candidate.point)
    rows = quotes.get(key, ())
    valid = [q for q in rows if q.captured_at <= decision_time and 0 <= (decision_time - q.captured_at).total_seconds() / 60.0 <= max_age_minutes]
    return valid[-1] if valid else None


def price_still_eligible(candidate: Candidate, quote: Quote) -> bool:
    # Preserve the proven odds-band definition at the actual placing bookmaker.
    if candidate.pool == "FOOTBALL_4.00_4.99":
        return 4.0 <= quote.odds < 5.0

    # For model-driven pools, require the target bookmaker price to retain at
    # least non-negative gross model EV when a fair probability is available.
    if candidate.fair_probability and candidate.fair_probability > 0:
        return candidate.fair_probability * quote.odds >= 1.0

    # If the source row carries no fair probability, do not invent one. Require
    # the bookmaker quote to be no worse than 97% of the frozen source price.
    if candidate.source_odds and candidate.source_odds > 0:
        return quote.odds >= candidate.source_odds * 0.97
    return False


def reconstruct(candidates: Sequence[Candidate], quotes: Mapping[Tuple[str, str, str, str, Optional[float]], Sequence[Quote]], *, max_age_minutes: float, max_spread_minutes: float) -> Dict[str, List[Card]]:
    by_system: Dict[str, List[Card]] = {name: [] for name in SYSTEM_SPECS}

    # Every historical quote time becomes a legitimate decision instant. This
    # avoids hindsight: a card can only use information captured on or before t.
    decision_times = sorted({q.captured_at for rows in quotes.values() for q in rows})

    for system, n in SYSTEM_SPECS.items():
        used_picks = set()
        for t in decision_times:
            active = [
                c for c in candidates
                if c.pick_key not in used_picks
                and c.source_time <= t
                and (c.commence_time is None or t < c.commence_time)
            ]
            if len(active) < n:
                continue

            best: Optional[Tuple[float, str, List[Tuple[Candidate, Quote]]]] = None
            for book in APPROVED_BOOKS:
                placeable: List[Tuple[Candidate, Quote]] = []
                seen_events = set()
                for c in active:
                    if c.event_id in seen_events:
                        continue
                    q = latest_quote(quotes, c, book, t, max_age_minutes)
                    if q is None or not price_still_eligible(c, q):
                        continue
                    placeable.append((c, q))
                    seen_events.add(c.event_id)

                if len(placeable) < n:
                    continue
                placeable.sort(key=lambda pair: ((pair[0].fair_probability or 0.0) * pair[1].odds, pair[1].odds), reverse=True)
                chosen = placeable[:n]
                quote_times = [q.captured_at for _, q in chosen]
                spread = (max(quote_times) - min(quote_times)).total_seconds() / 60.0
                if spread > max_spread_minutes:
                    continue
                score = sum(((c.fair_probability or 0.0) * q.odds) for c, q in chosen)
                candidate_best = (score, book, chosen)
                if best is None or candidate_best[0] > best[0]:
                    best = candidate_best

            if best is None:
                continue

            _, book, legs = best
            by_system[system].append(Card(system, book, t, legs))
            used_picks.update(c.pick_key for c, _ in legs)

    return by_system


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconstruct same-book manual Yankee/Heinz/Goliath opportunities from a Betting Lab research export ZIP.")
    parser.add_argument("export_zip", type=Path)
    parser.add_argument("--max-quote-age", type=float, default=DEFAULT_MAX_QUOTE_AGE_MINUTES)
    parser.add_argument("--max-quote-spread", type=float, default=DEFAULT_MAX_QUOTE_SPREAD_MINUTES)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    with ZipFile(args.export_zip) as zf:
        candidates = load_candidates(zf)
        quotes = load_quotes(zf)
    cards = reconstruct(candidates, quotes, max_age_minutes=args.max_quote_age, max_spread_minutes=args.max_quote_spread)

    payload = {
        "source_export": str(args.export_zip),
        "approved_books": list(APPROVED_BOOKS),
        "candidate_count_after_dedupe": len(candidates),
        "systems": {name: [card.as_dict() for card in rows] for name, rows in cards.items()},
        "counts": {name: len(rows) for name, rows in cards.items()},
    }
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.json_out:
        args.json_out.write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
