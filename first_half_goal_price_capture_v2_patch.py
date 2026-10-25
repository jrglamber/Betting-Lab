from __future__ import annotations

"""Robust forward-only FHG1 entry-price capture.

The V1 experiment requested only the featured ``totals_h1`` market and then
required a 0.5 goal line. In soccer the featured first-half total is commonly
1.0/1.5, so valid Over 0.5 quotes can live only in ``alternate_totals_h1``.
V1 also retried the same earliest unpriced candidates every heartbeat, allowing
a handful of no-price events to starve the rest of the slate.

V2 fixes both issues:
- request featured + alternate first-half totals in one event-odds call;
- freeze the first available Over 0.5 price from either market;
- rotate/retry candidates with a 30-minute backoff so one no-price event cannot
  block the rest of the forward slate;
- keep all evidence prospective: already-started fixtures are never backfilled.

Research-only. No betting authority.
"""

from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any, Dict, Iterable

import first_half_goal_lab as fhg
import first_half_goal_runtime_patch as fhg_runtime
from db import sanitize_sensitive_text, utc_now_iso
from quota import provider_actual_cost

VERSION = "FHG1_FROZEN_ENTRY_PRICE_V2"
MARKET_KEYS = ("totals_h1", "alternate_totals_h1")
SELECTION = "Over"
POINT = 0.5
PRICE_HORIZON_HOURS = 30.0
PRICE_RETRY_MINUTES = 30.0
MAX_EVENTS_PER_PASS = 8
_INSTALLED = False
_ORIGINAL_MAINTENANCE = None

_EXCLUDED_KEYS = {"paddypower", "paddy_power", "paddy-power"}


def _accessible_books(settings) -> tuple[str, ...]:
    values: list[str] = []
    for name in ("execution_bookmaker_keys", "manual_systems_placeable_bookmaker_keys"):
        for raw in tuple(getattr(settings, name, ()) or ()):
            key = str(raw or "").strip()
            if key and key.lower() not in _EXCLUDED_KEYS and key not in values:
                values.append(key)
    return tuple(values)


def _ensure_schema(db) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS first_half_goal_entry_prices(
            candidate_id INTEGER PRIMARY KEY,
            event_id TEXT NOT NULL,
            sport_key TEXT NOT NULL,
            market_key TEXT NOT NULL,
            selection TEXT NOT NULL,
            point REAL NOT NULL,
            bookmaker_key TEXT NOT NULL,
            bookmaker_title TEXT,
            entry_odds REAL NOT NULL,
            captured_at TEXT NOT NULL,
            model_probability REAL NOT NULL,
            implied_probability REAL NOT NULL,
            model_edge_pct REAL NOT NULL,
            model_version TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS first_half_goal_entry_price_attempts(
            candidate_id INTEGER PRIMARY KEY,
            last_attempt_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            no_price_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            last_market_keys TEXT
        )"""
    )
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_fhg_entry_event ON first_half_goal_entry_prices(event_id,captured_at)",
        "CREATE INDEX IF NOT EXISTS idx_fhg_entry_attempt_due ON first_half_goal_entry_price_attempts(last_attempt_at)",
    ):
        try:
            db.execute(sql)
        except Exception:
            pass


def _event_payload(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return {}


def _quotes(data: Any, allowed: Iterable[str]) -> list[dict[str, Any]]:
    allowed_set = {str(x) for x in allowed}
    event = _event_payload(data)
    out: list[dict[str, Any]] = []
    for book in event.get("bookmakers") or []:
        if not isinstance(book, dict):
            continue
        book_key = str(book.get("key") or "")
        if not book_key or book_key not in allowed_set or book_key.lower() in _EXCLUDED_KEYS:
            continue
        for market in book.get("markets") or []:
            if not isinstance(market, dict):
                continue
            market_key = str(market.get("key") or "")
            if market_key not in MARKET_KEYS:
                continue
            for outcome in market.get("outcomes") or []:
                if not isinstance(outcome, dict):
                    continue
                if str(outcome.get("name") or "").strip().lower() != SELECTION.lower():
                    continue
                try:
                    point = float(outcome.get("point"))
                    price = float(outcome.get("price"))
                except Exception:
                    continue
                if abs(point - POINT) > 1e-9 or price <= 1.0:
                    continue
                out.append(
                    {
                        "market_key": market_key,
                        "bookmaker_key": book_key,
                        "bookmaker_title": str(book.get("title") or book_key),
                        "price": price,
                    }
                )
    return out


def _record_attempt(
    db,
    candidate_id: int,
    now: datetime,
    *,
    no_price: bool = False,
    error: str = "",
) -> None:
    db.execute(
        """INSERT INTO first_half_goal_entry_price_attempts(
               candidate_id,last_attempt_at,attempts,no_price_count,last_error,last_market_keys
           ) VALUES(?,?,?,?,?,?)
           ON CONFLICT(candidate_id) DO UPDATE SET
               last_attempt_at=excluded.last_attempt_at,
               attempts=first_half_goal_entry_price_attempts.attempts+1,
               no_price_count=first_half_goal_entry_price_attempts.no_price_count+excluded.no_price_count,
               last_error=excluded.last_error,
               last_market_keys=excluded.last_market_keys""",
        (
            int(candidate_id),
            now.isoformat(),
            1,
            1 if no_price else 0,
            str(error)[:500] if error else None,
            ",".join(MARKET_KEYS),
        ),
    )


def capture_entry_prices(web_module, db, now: datetime | None = None) -> Dict[str, Any]:
    _ensure_schema(db)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    books = _accessible_books(web_module.settings)
    if not books:
        return {
            "ok": True,
            "priced": 0,
            "polled": 0,
            "reason": "no_accessible_books",
            "version": VERSION,
        }

    end = now + timedelta(hours=PRICE_HORIZON_HOURS)
    retry_before = now - timedelta(minutes=PRICE_RETRY_MINUTES)
    rows = db.fetchall(
        """SELECT c.*,a.last_attempt_at,a.attempts,a.no_price_count
           FROM first_half_goal_candidates c
           LEFT JOIN first_half_goal_entry_prices p ON p.candidate_id=c.id
           LEFT JOIN first_half_goal_entry_price_attempts a ON a.candidate_id=c.id
           WHERE c.status='OPEN' AND c.estimated_probability>=?
             AND c.commence_time>? AND c.commence_time<=?
             AND p.candidate_id IS NULL
             AND (a.last_attempt_at IS NULL OR a.last_attempt_at<=?)
           ORDER BY
             CASE WHEN a.last_attempt_at IS NULL THEN 0 ELSE 1 END,
             COALESCE(a.last_attempt_at,'1970-01-01T00:00:00+00:00'),
             c.commence_time,c.estimated_probability DESC,c.id""",
        (
            float(fhg.THRESHOLD),
            now.isoformat(),
            end.isoformat(),
            retry_before.isoformat(),
        ),
    )

    waiting_row = db.fetchone(
        """SELECT COUNT(*) AS n
           FROM first_half_goal_candidates c
           LEFT JOIN first_half_goal_entry_prices p ON p.candidate_id=c.id
           WHERE c.status='OPEN' AND c.estimated_probability>=?
             AND c.commence_time>? AND c.commence_time<=?
             AND p.candidate_id IS NULL""",
        (float(fhg.THRESHOLD), now.isoformat(), end.isoformat()),
    ) or {}

    polled = priced = no_price = blocked = 0
    errors: list[str] = []
    requested_markets = ",".join(MARKET_KEYS)
    estimated = max(1, ceil(len(books) / 10)) * len(MARKET_KEYS)

    for raw in rows[:MAX_EVENTS_PER_PASS]:
        candidate = dict(raw)
        decision = web_module.quota.decide(estimated)
        if not decision.allowed:
            blocked += 1
            break

        actual = 0
        try:
            result = web_module.api.event_odds(
                str(candidate["sport_key"]),
                str(candidate["event_id"]),
                str(web_module.settings.odds_region),
                MARKET_KEYS,
                bookmaker_keys=books,
            )
            actual = provider_actual_cost(result.last_cost, estimated)
            web_module.quota.update(
                remaining=result.remaining,
                used=result.used,
                last_cost=result.last_cost,
            )
            polled += 1
            options = _quotes(result.data, books)
            if not options:
                no_price += 1
                _record_attempt(db, int(candidate["id"]), now, no_price=True)
                web_module.db.record_collector_run(
                    "FHG1_ENTRY_PRICE",
                    True,
                    event_id=str(candidate["event_id"]),
                    sport_key=str(candidate["sport_key"]),
                    requested_markets=requested_markets,
                    estimated_cost=estimated,
                    actual_cost=actual,
                    detail=(
                        f"books={','.join(books)}; quote_rows=0; "
                        f"retry_minutes={PRICE_RETRY_MINUTES:.0f}"
                    ),
                )
                continue

            best = max(
                options,
                key=lambda x: (
                    float(x["price"]),
                    str(x["bookmaker_key"]),
                    str(x["market_key"]),
                ),
            )
            price = float(best["price"])
            model_p = float(candidate["estimated_probability"])
            implied = 1.0 / price
            edge = (model_p * price - 1.0) * 100.0
            stamp = utc_now_iso()
            inserted = db.execute(
                """INSERT INTO first_half_goal_entry_prices(
                    candidate_id,event_id,sport_key,market_key,selection,point,
                    bookmaker_key,bookmaker_title,entry_odds,captured_at,
                    model_probability,implied_probability,model_edge_pct,model_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(candidate_id) DO NOTHING""",
                (
                    int(candidate["id"]),
                    str(candidate["event_id"]),
                    str(candidate["sport_key"]),
                    str(best["market_key"]),
                    SELECTION,
                    POINT,
                    str(best["bookmaker_key"]),
                    str(best["bookmaker_title"]),
                    price,
                    stamp,
                    model_p,
                    implied,
                    edge,
                    str(candidate.get("model_version") or fhg.MODEL_VERSION),
                ),
            )
            _record_attempt(db, int(candidate["id"]), now)
            priced += int(inserted > 0)
            web_module.db.record_collector_run(
                "FHG1_ENTRY_PRICE",
                True,
                event_id=str(candidate["event_id"]),
                sport_key=str(candidate["sport_key"]),
                requested_markets=requested_markets,
                estimated_cost=estimated,
                actual_cost=actual,
                detail=(
                    f"book={best['bookmaker_key']}; market={best['market_key']}; "
                    f"entry_odds={price:.4f}; model_p={model_p:.6f}; frozen=1"
                ),
            )
        except Exception as exc:
            msg = f"{type(exc).__name__}: {sanitize_sensitive_text(exc)}"
            errors.append(msg[:400])
            try:
                _record_attempt(db, int(candidate["id"]), now, error=msg)
                web_module.db.record_collector_run(
                    "FHG1_ENTRY_PRICE",
                    False,
                    event_id=str(candidate.get("event_id") or ""),
                    sport_key=str(candidate.get("sport_key") or ""),
                    requested_markets=requested_markets,
                    estimated_cost=estimated,
                    actual_cost=actual,
                    detail=f"error={msg}",
                )
            except Exception:
                pass

    return {
        "ok": not errors,
        "version": VERSION,
        "qualified_waiting": int(waiting_row.get("n") or 0),
        "due_waiting": len(rows),
        "polled": polled,
        "priced": priced,
        "no_price": no_price,
        "quota_blocked": blocked,
        "books": list(books),
        "markets": list(MARKET_KEYS),
        "target": f"{SELECTION} {POINT} first-half total",
        "retry_minutes": PRICE_RETRY_MINUTES,
        "errors": errors[:3],
        "betting_authority": False,
    }


def install(base_app_module) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_MAINTENANCE
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    web_module = base_app_module.web_module
    db = base_app_module.db
    _ensure_schema(db)
    _ORIGINAL_MAINTENANCE = fhg.run_maintenance

    def maintenance_with_entry_prices(db_obj, now=None):
        state = dict(_ORIGINAL_MAINTENANCE(db_obj, now))
        state["entry_price_capture"] = capture_entry_prices(web_module, db_obj, now)
        return state

    fhg.run_maintenance = maintenance_with_entry_prices
    fhg_runtime.run_maintenance = maintenance_with_entry_prices

    startup = capture_entry_prices(web_module, db)
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "startup": startup,
        "frozen_first_price": True,
        "prospective_only": True,
        "alternate_half_total_enabled": True,
        "fair_retry_rotation": True,
        "betting_authority": False,
    }


__all__ = ["VERSION", "MARKET_KEYS", "capture_entry_prices", "install"]
