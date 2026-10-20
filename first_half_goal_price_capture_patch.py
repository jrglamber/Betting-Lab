from __future__ import annotations

"""Capture a frozen executable price for every qualified FHG1 candidate.

Research-only.  The model forecast is already frozen by first_half_goal_lab;
this patch adds the missing market-price side needed to evaluate value/ROI.
The first available best price across the user's accessible venues is stored
once and is never improved retrospectively.
"""

from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any, Dict, Iterable

import first_half_goal_lab as fhg
import first_half_goal_runtime_patch as fhg_runtime
from db import sanitize_sensitive_text, utc_now_iso
from quota import provider_actual_cost

VERSION = "FHG1_FROZEN_ENTRY_PRICE_V1"
MARKET_KEY = "totals_h1"
SELECTION = "Over"
POINT = 0.5
PRICE_HORIZON_HOURS = 30.0
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
    try:
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fhg_entry_event ON first_half_goal_entry_prices(event_id,captured_at)"
        )
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
            if not isinstance(market, dict) or str(market.get("key") or "") != MARKET_KEY:
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
                        "bookmaker_key": book_key,
                        "bookmaker_title": str(book.get("title") or book_key),
                        "price": price,
                    }
                )
    return out


def capture_entry_prices(web_module, db, now: datetime | None = None) -> Dict[str, Any]:
    _ensure_schema(db)
    now = now or datetime.now(timezone.utc)
    books = _accessible_books(web_module.settings)
    if not books:
        return {"ok": True, "priced": 0, "polled": 0, "reason": "no_accessible_books", "version": VERSION}

    end = now + timedelta(hours=PRICE_HORIZON_HOURS)
    rows = db.fetchall(
        """SELECT c.*
           FROM first_half_goal_candidates c
           LEFT JOIN first_half_goal_entry_prices p ON p.candidate_id=c.id
           WHERE c.status='OPEN' AND c.estimated_probability>=?
             AND c.commence_time>? AND c.commence_time<=?
             AND p.candidate_id IS NULL
           ORDER BY c.commence_time,c.estimated_probability DESC,c.id""",
        (float(fhg.THRESHOLD), now.isoformat(), end.isoformat()),
    )

    polled = priced = no_price = blocked = 0
    errors: list[str] = []
    for raw in rows[:MAX_EVENTS_PER_PASS]:
        candidate = dict(raw)
        estimated = max(1, ceil(len(books) / 10))
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
                (MARKET_KEY,),
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
                web_module.db.record_collector_run(
                    "FHG1_ENTRY_PRICE", True,
                    event_id=str(candidate["event_id"]),
                    sport_key=str(candidate["sport_key"]),
                    requested_markets=MARKET_KEY,
                    estimated_cost=estimated,
                    actual_cost=actual,
                    detail=f"books={','.join(books)}; quote_rows=0",
                )
                continue

            best = max(options, key=lambda x: (float(x["price"]), str(x["bookmaker_key"])))
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
                    int(candidate["id"]), str(candidate["event_id"]), str(candidate["sport_key"]),
                    MARKET_KEY, SELECTION, POINT, str(best["bookmaker_key"]),
                    str(best["bookmaker_title"]), price, stamp, model_p, implied, edge,
                    str(candidate.get("model_version") or fhg.MODEL_VERSION),
                ),
            )
            priced += int(inserted > 0)
            web_module.db.record_collector_run(
                "FHG1_ENTRY_PRICE", True,
                event_id=str(candidate["event_id"]),
                sport_key=str(candidate["sport_key"]),
                requested_markets=MARKET_KEY,
                estimated_cost=estimated,
                actual_cost=actual,
                detail=(
                    f"book={best['bookmaker_key']}; entry_odds={price:.4f}; "
                    f"model_p={model_p:.6f}; frozen=1"
                ),
            )
        except Exception as exc:
            msg = f"{type(exc).__name__}: {sanitize_sensitive_text(exc)}"
            errors.append(msg[:400])
            try:
                web_module.db.record_collector_run(
                    "FHG1_ENTRY_PRICE", False,
                    event_id=str(candidate.get("event_id") or ""),
                    sport_key=str(candidate.get("sport_key") or ""),
                    requested_markets=MARKET_KEY,
                    estimated_cost=estimated,
                    actual_cost=actual,
                    detail=f"error={msg}",
                )
            except Exception:
                pass

    return {
        "ok": not errors,
        "version": VERSION,
        "qualified_waiting": len(rows),
        "polled": polled,
        "priced": priced,
        "no_price": no_price,
        "quota_blocked": blocked,
        "books": list(books),
        "market": f"{SELECTION} {POINT} {MARKET_KEY}",
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

    # The FHG runtime patch imported run_maintenance by value, so update both
    # module globals. Existing heartbeat/worker wrappers then call this function.
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
        "betting_authority": False,
    }
