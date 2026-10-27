from __future__ import annotations

"""FHG hardening: lifecycle safety, same-book price evidence, and FHG2 challenger.

Research-only. FHG1 selection probabilities remain frozen and authoritative.
This patch:
- retires the blocked SofaScore fallback and extends ESPN to Scotland tiers 2-4;
- detects rescheduled/superseded candidates and voids affected theoretical cards;
- preserves all first-seen bookmaker O0.5 H1 quotes and adds price diagnostics;
- captures bounded near-simultaneous same-book 8/9/10-fold snapshots;
- settles placeable cards independently from theoretical cards;
- creates a prospective-only FHG2 challenger without changing FHG1 decisions.
"""

from datetime import datetime, timedelta, timezone
from html import escape
from math import ceil, exp, prod
import json
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

import first_half_goal_lab as fhg
import first_half_goal_runtime_patch as fhg_runtime
import first_half_goal_price_capture_v2_patch as price_v2
import first_half_goal_free_settlement_v2_patch as free_v2
import first_half_goal_espn_settlement_patch as espn
from db import sanitize_sensitive_text, utc_now_iso
from quota import provider_actual_cost

VERSION = "FHG_HARDENING_V1"
FHG2_VERSION = "FHG2_RECENCY_INTERACTION_FORWARD_V1"
SAME_BOOK_HORIZON_HOURS = 24.0
SAME_BOOK_MAX_ATTEMPTS = 5
SAME_BOOK_MAX_LEGS = 10
SAME_BOOK_RETRY_FAR_MINUTES = 180
SAME_BOOK_RETRY_NEAR_MINUTES = 60
RESCHEDULE_DRIFT_MINUTES = 180
_INSTALLED = False
_ORIGINAL_MAINTENANCE = None
_ORIGINAL_CREATE_CARDS = None
_ORIGINAL_SETTLE_CARDS = None
_ORIGINAL_SCOREBOARD = None
_ORIGINAL_DASHBOARD = None
_ORIGINAL_CURATED_SECTION = None


def _ensure_schema(db) -> None:
    iid = "BIGSERIAL PRIMARY KEY" if getattr(db, "is_postgres", False) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    statements = (
        """CREATE TABLE IF NOT EXISTS first_half_goal_lifecycle(
            candidate_id INTEGER PRIMARY KEY,
            lifecycle_status TEXT NOT NULL,
            replacement_event_id TEXT,
            detected_at TEXT NOT NULL,
            detail TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS first_half_goal_bookmaker_quotes(
            candidate_id INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            sport_key TEXT NOT NULL,
            bookmaker_key TEXT NOT NULL,
            bookmaker_title TEXT,
            market_key TEXT NOT NULL,
            selection TEXT NOT NULL,
            point REAL NOT NULL,
            entry_odds REAL NOT NULL,
            captured_at TEXT NOT NULL,
            model_probability REAL NOT NULL,
            PRIMARY KEY(candidate_id,bookmaker_key)
        )""",
        """CREATE TABLE IF NOT EXISTS first_half_goal_price_diagnostics(
            candidate_id INTEGER PRIMARY KEY,
            last_attempt_at TEXT NOT NULL,
            market_present INTEGER NOT NULL DEFAULT 0,
            target_present INTEGER NOT NULL DEFAULT 0,
            books_seen INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            attempts INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS first_half_goal_same_book_snapshots(
            snapshot_id TEXT NOT NULL,
            candidate_id INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            slate_key TEXT NOT NULL,
            bookmaker_key TEXT NOT NULL,
            bookmaker_title TEXT,
            market_key TEXT NOT NULL,
            entry_odds REAL NOT NULL,
            captured_at TEXT NOT NULL,
            PRIMARY KEY(snapshot_id,candidate_id,bookmaker_key)
        )""",
        """CREATE TABLE IF NOT EXISTS first_half_goal_same_book_attempts(
            slate_key TEXT PRIMARY KEY,
            last_attempt_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            no_common_book_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            last_snapshot_id TEXT
        )""",
        f"""CREATE TABLE IF NOT EXISTS first_half_goal_placeable_cards(
            id {iid},
            slate_key TEXT NOT NULL,
            card_size INTEGER NOT NULL,
            model_version TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            kickoff_time TEXT NOT NULL,
            bookmaker_key TEXT NOT NULL,
            bookmaker_title TEXT,
            combined_odds REAL NOT NULL,
            event_ids_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            settled_legs INTEGER NOT NULL DEFAULT 0,
            hit_legs INTEGER NOT NULL DEFAULT 0,
            all_hit INTEGER,
            settled_at TEXT,
            UNIQUE(slate_key,card_size,model_version)
        )""",
        """CREATE TABLE IF NOT EXISTS first_half_goal_challenger_predictions(
            candidate_id INTEGER PRIMARY KEY,
            event_id TEXT NOT NULL,
            sport_key TEXT NOT NULL,
            commence_time TEXT NOT NULL,
            created_at TEXT NOT NULL,
            model_version TEXT NOT NULL,
            estimated_probability REAL NOT NULL,
            tier TEXT NOT NULL,
            home_n INTEGER NOT NULL,
            away_n INTEGER NOT NULL,
            home_score_rate REAL NOT NULL,
            home_concede_rate REAL NOT NULL,
            away_score_rate REAL NOT NULL,
            away_concede_rate REAL NOT NULL,
            raw_interaction_probability REAL NOT NULL,
            league_rate REAL NOT NULL,
            xg_probability REAL,
            evidence_mode TEXT NOT NULL DEFAULT 'FORWARD'
        )""",
    )
    for sql in statements:
        db.execute(sql)
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_fhg_quote_candidate ON first_half_goal_bookmaker_quotes(candidate_id,captured_at)",
        "CREATE INDEX IF NOT EXISTS idx_fhg_snapshot_slate ON first_half_goal_same_book_snapshots(slate_key,captured_at)",
        "CREATE INDEX IF NOT EXISTS idx_fhg_placeable_status ON first_half_goal_placeable_cards(status,kickoff_time)",
        "CREATE INDEX IF NOT EXISTS idx_fhg2_event ON first_half_goal_challenger_predictions(event_id,created_at)",
    ):
        try:
            db.execute(sql)
        except Exception:
            pass


def _utc(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _event_rows_for_candidate(db, candidate: Mapping[str, Any], ko: datetime) -> list[dict[str, Any]]:
    try:
        rows = db.fetchall(
            """SELECT event_id,sport_key,commence_time,status,home_team,away_team
               FROM events
               WHERE sport_key=? AND commence_time>=? AND commence_time<=?
               ORDER BY commence_time,event_id""",
            (
                str(candidate.get("sport_key") or ""),
                (ko - timedelta(hours=6)).isoformat(),
                (ko + timedelta(days=45)).isoformat(),
            ),
        )
    except Exception:
        return []
    hk = str(candidate.get("home_key") or "")
    ak = str(candidate.get("away_key") or "")
    out = []
    for raw in rows:
        row = dict(raw)
        if fhg._normalize_text(str(row.get("home_team") or "")) != hk:
            continue
        if fhg._normalize_text(str(row.get("away_team") or "")) != ak:
            continue
        out.append(row)
    return out


def reconcile_lifecycle(db, now: Optional[datetime] = None) -> Dict[str, int]:
    """Mark explicit postponements/reschedules without inventing results."""
    _ensure_schema(db)
    now = _utc(now)
    try:
        rows = db.fetchall(
            """SELECT * FROM first_half_goal_candidates
               WHERE status='OPEN' AND commence_time<?
               ORDER BY commence_time,id""",
            ((now - timedelta(hours=2)).isoformat(),),
        )
    except Exception:
        rows = []
    superseded = voided = 0
    terminal_statuses = {"POSTPONED", "CANCELLED", "CANCELED", "VOID", "ABANDONED"}
    for raw in rows:
        c = dict(raw)
        ko = fhg._dt(c.get("commence_time"))
        if ko is None:
            continue
        matches = _event_rows_for_candidate(db, c, ko)
        same_id = next((r for r in matches if str(r.get("event_id") or "") == str(c.get("event_id") or "")), None)
        replacement = None
        for row in matches:
            rko = fhg._dt(row.get("commence_time"))
            if rko is None:
                continue
            drift = (rko - ko).total_seconds() / 60.0
            if drift >= RESCHEDULE_DRIFT_MINUTES and rko > now:
                replacement = row
                break

        lifecycle_status = None
        replacement_id = None
        detail = None
        if replacement is not None:
            lifecycle_status = "SUPERSEDED"
            replacement_id = str(replacement.get("event_id") or "")
            detail = (
                f"kickoff moved from {ko.isoformat()} to {replacement.get('commence_time')}; "
                f"replacement_event_id={replacement_id or 'same/unknown'}"
            )
        elif same_id is not None and str(same_id.get("status") or "").upper() in terminal_statuses:
            lifecycle_status = "VOID"
            detail = f"event source status={str(same_id.get('status') or '').upper()}"

        if lifecycle_status is None:
            continue
        changed = db.execute(
            "UPDATE first_half_goal_candidates SET status=? WHERE id=? AND status='OPEN'",
            (lifecycle_status, int(c["id"])),
        )
        if int(changed or 0) <= 0:
            continue
        db.execute(
            """INSERT INTO first_half_goal_lifecycle(
                   candidate_id,lifecycle_status,replacement_event_id,detected_at,detail
               ) VALUES(?,?,?,?,?)
               ON CONFLICT(candidate_id) DO UPDATE SET
                   lifecycle_status=excluded.lifecycle_status,
                   replacement_event_id=excluded.replacement_event_id,
                   detected_at=excluded.detected_at,
                   detail=excluded.detail""",
            (
                int(c["id"]),
                lifecycle_status,
                replacement_id,
                now.isoformat(),
                detail,
            ),
        )
        superseded += int(lifecycle_status == "SUPERSEDED")
        voided += int(lifecycle_status == "VOID")
    return {"superseded": superseded, "voided": voided}


def create_cards_safe(db, now: Optional[datetime] = None) -> int:
    """FHG1 theoretical cards, but only from still-live OPEN candidates."""
    now = _utc(now)
    created = 0
    try:
        slates = db.fetchall(
            """SELECT DISTINCT slate_key FROM first_half_goal_candidates
               WHERE status='OPEN' AND commence_time>? AND estimated_probability>=?""",
            (now.isoformat(), float(fhg.THRESHOLD)),
        )
    except Exception:
        slates = []
    for s in slates:
        cs = db.fetchall(
            """SELECT * FROM first_half_goal_candidates
               WHERE status='OPEN' AND slate_key=? AND commence_time>? AND estimated_probability>=?
               ORDER BY estimated_probability DESC,event_id""",
            (s["slate_key"], now.isoformat(), float(fhg.THRESHOLD)),
        )
        for size in (8, 9, 10):
            if len(cs) < size:
                continue
            if db.fetchone(
                "SELECT id FROM first_half_goal_cards WHERE slate_key=? AND card_size=? AND model_version=?",
                (s["slate_key"], size, fhg.MODEL_VERSION),
            ):
                continue
            pick = cs[:size]
            db.execute(
                """INSERT INTO first_half_goal_cards(
                       slate_key,card_size,model_version,created_at,kickoff_time,event_ids_json,
                       minimum_probability,status
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    s["slate_key"],
                    size,
                    fhg.MODEL_VERSION,
                    now.isoformat(),
                    pick[0]["commence_time"],
                    json.dumps([x["event_id"] for x in pick]),
                    min(float(x["estimated_probability"]) for x in pick),
                    "OPEN",
                ),
            )
            created += 1
    return created


def settle_cards_safe(db, now: Optional[datetime] = None) -> int:
    """Settle complete cards and VOID any card whose leg became non-runnable."""
    now = _utc(now)
    n = 0
    terminal = {"SUPERSEDED", "VOID", "POSTPONED", "CANCELLED", "CANCELED", "ABANDONED"}
    for card in db.fetchall("SELECT * FROM first_half_goal_cards WHERE status='OPEN'"):
        try:
            ids = list(json.loads(card.get("event_ids_json") or "[]"))
        except Exception:
            ids = []
        rows = []
        for eid in ids:
            rows.append(
                db.fetchone(
                    "SELECT status,fhg_hit FROM first_half_goal_candidates WHERE event_id=?",
                    (eid,),
                )
            )
        if not rows:
            continue
        statuses = {str((r or {}).get("status") or "").upper() for r in rows}
        if statuses & terminal:
            settled_rows = [r for r in rows if r and str(r.get("status") or "") == "SETTLED" and r.get("fhg_hit") is not None]
            hits = sum(int(r.get("fhg_hit") or 0) for r in settled_rows)
            db.execute(
                """UPDATE first_half_goal_cards
                   SET status='VOID',settled_legs=?,hit_legs=?,all_hit=NULL,settled_at=?
                   WHERE id=? AND status='OPEN'""",
                (len(settled_rows), hits, now.isoformat(), card["id"]),
            )
            n += 1
            continue
        if any(not r or str(r.get("status") or "") != "SETTLED" or r.get("fhg_hit") is None for r in rows):
            continue
        hits = sum(int(r.get("fhg_hit") or 0) for r in rows)
        db.execute(
            """UPDATE first_half_goal_cards
               SET status='SETTLED',settled_legs=?,hit_legs=?,all_hit=?,settled_at=?
               WHERE id=? AND status='OPEN'""",
            (len(rows), hits, 1 if hits == len(rows) else 0, now.isoformat(), card["id"]),
        )
        n += 1
    return n


def _payload_market_diagnostics(data: Any, allowed: Iterable[str]) -> tuple[int, int, int]:
    allowed_set = {str(x) for x in allowed}
    event = price_v2._event_payload(data)
    books_seen = market_present = target_present = 0
    for book in event.get("bookmakers") or []:
        if not isinstance(book, Mapping):
            continue
        book_key = str(book.get("key") or "")
        if book_key not in allowed_set:
            continue
        books_seen += 1
        for market in book.get("markets") or []:
            if not isinstance(market, Mapping) or str(market.get("key") or "") not in price_v2.MARKET_KEYS:
                continue
            market_present = 1
            for outcome in market.get("outcomes") or []:
                if not isinstance(outcome, Mapping):
                    continue
                try:
                    point = float(outcome.get("point"))
                    px = float(outcome.get("price"))
                except Exception:
                    continue
                if (
                    str(outcome.get("name") or "").strip().lower() == price_v2.SELECTION.lower()
                    and abs(point - float(price_v2.POINT)) <= 1e-9
                    and px > 1.0
                ):
                    target_present = 1
    return books_seen, market_present, target_present


def _record_price_diag(
    db,
    candidate_id: int,
    now: datetime,
    *,
    books_seen: int,
    market_present: int,
    target_present: int,
    reason: str,
) -> None:
    db.execute(
        """INSERT INTO first_half_goal_price_diagnostics(
               candidate_id,last_attempt_at,market_present,target_present,books_seen,reason,attempts
           ) VALUES(?,?,?,?,?,?,1)
           ON CONFLICT(candidate_id) DO UPDATE SET
               last_attempt_at=excluded.last_attempt_at,
               market_present=excluded.market_present,
               target_present=excluded.target_present,
               books_seen=excluded.books_seen,
               reason=excluded.reason,
               attempts=first_half_goal_price_diagnostics.attempts+1""",
        (
            int(candidate_id),
            now.isoformat(),
            int(market_present),
            int(target_present),
            int(books_seen),
            reason,
        ),
    )


def _freeze_all_book_quotes(db, candidate: Mapping[str, Any], options: Sequence[Mapping[str, Any]], now: datetime) -> int:
    per_book: dict[str, Mapping[str, Any]] = {}
    for option in options:
        key = str(option.get("bookmaker_key") or "")
        if not key:
            continue
        prev = per_book.get(key)
        if prev is None or float(option.get("price") or 0) > float(prev.get("price") or 0):
            per_book[key] = option
    inserted = 0
    for key, option in per_book.items():
        changed = db.execute(
            """INSERT INTO first_half_goal_bookmaker_quotes(
                   candidate_id,event_id,sport_key,bookmaker_key,bookmaker_title,market_key,
                   selection,point,entry_odds,captured_at,model_probability
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(candidate_id,bookmaker_key) DO NOTHING""",
            (
                int(candidate["id"]),
                str(candidate["event_id"]),
                str(candidate["sport_key"]),
                key,
                str(option.get("bookmaker_title") or key),
                str(option.get("market_key") or ""),
                price_v2.SELECTION,
                float(price_v2.POINT),
                float(option["price"]),
                now.isoformat(),
                float(candidate["estimated_probability"]),
            ),
        )
        inserted += int(changed or 0)
    return inserted


def capture_entry_prices_v3(web_module, db, now: Optional[datetime] = None) -> Dict[str, Any]:
    """V2-compatible best-price freeze plus all-book diagnostics, no duplicate API pass."""
    _ensure_schema(db)
    now = _utc(now)
    books = price_v2._accessible_books(web_module.settings)
    if not books:
        return {"ok": True, "priced": 0, "polled": 0, "reason": "no_accessible_books", "version": VERSION}

    end = now + timedelta(hours=float(price_v2.PRICE_HORIZON_HOURS))
    retry_before = now - timedelta(minutes=float(price_v2.PRICE_RETRY_MINUTES))
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
        (float(fhg.THRESHOLD), now.isoformat(), end.isoformat(), retry_before.isoformat()),
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

    polled = priced = no_price = blocked = all_book_quotes = 0
    errors: list[str] = []
    requested_markets = ",".join(price_v2.MARKET_KEYS)
    estimated = max(1, ceil(len(books) / 10)) * len(price_v2.MARKET_KEYS)

    for raw in rows[: max(10, int(price_v2.MAX_EVENTS_PER_PASS))]:
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
                price_v2.MARKET_KEYS,
                bookmaker_keys=books,
            )
            actual = provider_actual_cost(result.last_cost, estimated)
            web_module.quota.update(remaining=result.remaining, used=result.used, last_cost=result.last_cost)
            polled += 1
            books_seen, market_present, target_present = _payload_market_diagnostics(result.data, books)
            options = price_v2._quotes(result.data, books)
            reason = "TARGET_FOUND" if options else "NO_OVER_0_5" if market_present else "NO_H1_MARKET"
            _record_price_diag(
                db,
                int(candidate["id"]),
                now,
                books_seen=books_seen,
                market_present=market_present,
                target_present=target_present,
                reason=reason,
            )
            if not options:
                no_price += 1
                price_v2._record_attempt(db, int(candidate["id"]), now, no_price=True)
                web_module.db.record_collector_run(
                    "FHG1_ENTRY_PRICE",
                    True,
                    event_id=str(candidate["event_id"]),
                    sport_key=str(candidate["sport_key"]),
                    requested_markets=requested_markets,
                    estimated_cost=estimated,
                    actual_cost=actual,
                    detail=f"reason={reason}; books_seen={books_seen}; retry_minutes={price_v2.PRICE_RETRY_MINUTES:.0f}",
                )
                continue

            all_book_quotes += _freeze_all_book_quotes(db, candidate, options, now)
            best = max(
                options,
                key=lambda x: (float(x["price"]), str(x["bookmaker_key"]), str(x["market_key"])),
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
                    price_v2.SELECTION,
                    float(price_v2.POINT),
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
            price_v2._record_attempt(db, int(candidate["id"]), now)
            priced += int(inserted or 0)
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
                    f"entry_odds={price:.4f}; all_books={len({str(x['bookmaker_key']) for x in options})}; frozen=1"
                ),
            )
        except Exception as exc:
            msg = f"{type(exc).__name__}: {sanitize_sensitive_text(exc)}"
            errors.append(msg[:400])
            try:
                price_v2._record_attempt(db, int(candidate["id"]), now, error=msg)
                _record_price_diag(
                    db,
                    int(candidate["id"]),
                    now,
                    books_seen=0,
                    market_present=0,
                    target_present=0,
                    reason="ERROR",
                )
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
        "all_book_quotes_frozen": all_book_quotes,
        "no_price": no_price,
        "quota_blocked": blocked,
        "books": list(books),
        "markets": list(price_v2.MARKET_KEYS),
        "target": f"{price_v2.SELECTION} {price_v2.POINT} first-half total",
        "retry_minutes": price_v2.PRICE_RETRY_MINUTES,
        "errors": errors[:3],
        "betting_authority": False,
    }


def _same_book_due(db, slate: str, now: datetime, kickoff: datetime) -> bool:
    row = db.fetchone("SELECT * FROM first_half_goal_same_book_attempts WHERE slate_key=?", (slate,))
    if not row:
        return True
    attempts = int(row.get("attempts") or 0)
    if attempts >= SAME_BOOK_MAX_ATTEMPTS:
        return False
    last = fhg._dt(row.get("last_attempt_at"))
    if last is None:
        return True
    mins_to_ko = max(0.0, (kickoff - now).total_seconds() / 60.0)
    cooldown = SAME_BOOK_RETRY_NEAR_MINUTES if mins_to_ko <= 240 else SAME_BOOK_RETRY_FAR_MINUTES
    return (now - last).total_seconds() >= cooldown * 60


def _record_same_book_attempt(db, slate: str, now: datetime, snapshot_id: str, *, no_common: bool, error: str = "") -> None:
    db.execute(
        """INSERT INTO first_half_goal_same_book_attempts(
               slate_key,last_attempt_at,attempts,no_common_book_count,last_error,last_snapshot_id
           ) VALUES(?,?,1,?,?,?)
           ON CONFLICT(slate_key) DO UPDATE SET
               last_attempt_at=excluded.last_attempt_at,
               attempts=first_half_goal_same_book_attempts.attempts+1,
               no_common_book_count=first_half_goal_same_book_attempts.no_common_book_count+excluded.no_common_book_count,
               last_error=excluded.last_error,
               last_snapshot_id=excluded.last_snapshot_id""",
        (slate, now.isoformat(), 1 if no_common else 0, error[:500] if error else None, snapshot_id),
    )


def _existing_placeable_sizes(db, slate: str) -> set[int]:
    try:
        rows = db.fetchall(
            "SELECT card_size FROM first_half_goal_placeable_cards WHERE slate_key=? AND model_version=?",
            (slate, fhg.MODEL_VERSION),
        )
    except Exception:
        return set()
    return {int(r.get("card_size") or 0) for r in rows}


def capture_same_book_cards(web_module, db, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Bounded, near-simultaneous same-book quote snapshots for actual card feasibility."""
    _ensure_schema(db)
    now = _utc(now)
    end = now + timedelta(hours=SAME_BOOK_HORIZON_HOURS)
    books = price_v2._accessible_books(web_module.settings)
    if not books:
        return {"ok": True, "attempted_slates": 0, "placeable_cards": 0, "reason": "no_accessible_books"}

    try:
        slates = db.fetchall(
            """SELECT slate_key,MIN(commence_time) AS kickoff,COUNT(*) AS n
               FROM first_half_goal_candidates
               WHERE status='OPEN' AND estimated_probability>=?
                 AND commence_time>? AND commence_time<=?
               GROUP BY slate_key HAVING COUNT(*)>=8
               ORDER BY kickoff""",
            (float(fhg.THRESHOLD), now.isoformat(), end.isoformat()),
        )
    except Exception:
        slates = []

    estimated = max(1, ceil(len(books) / 10)) * len(price_v2.MARKET_KEYS)
    attempted_slates = polled_events = created_cards = quota_blocked = 0
    errors: list[str] = []

    for s in slates:
        slate = str(s.get("slate_key") or "")
        kickoff = fhg._dt(s.get("kickoff"))
        if not slate or kickoff is None:
            continue
        candidate_count = int(s.get("n") or 0)
        theoretical_sizes = {size for size in (8, 9, 10) if candidate_count >= size}
        if theoretical_sizes.issubset(_existing_placeable_sizes(db, slate)):
            continue
        if not _same_book_due(db, slate, now, kickoff):
            continue

        candidates = [
            dict(x)
            for x in db.fetchall(
                """SELECT * FROM first_half_goal_candidates
                   WHERE status='OPEN' AND slate_key=? AND commence_time>?
                     AND estimated_probability>=?
                   ORDER BY estimated_probability DESC,event_id
                   LIMIT ?""",
                (slate, now.isoformat(), float(fhg.THRESHOLD), SAME_BOOK_MAX_LEGS),
            )
        ]
        snapshot_id = f"{now.isoformat()}|{slate}"
        attempted_slates += 1
        per_candidate: dict[int, dict[str, dict[str, Any]]] = {}
        slate_error = ""
        for candidate in candidates:
            decision = web_module.quota.decide(estimated)
            if not decision.allowed:
                quota_blocked += 1
                slate_error = "quota_blocked"
                break
            try:
                result = web_module.api.event_odds(
                    str(candidate["sport_key"]),
                    str(candidate["event_id"]),
                    str(web_module.settings.odds_region),
                    price_v2.MARKET_KEYS,
                    bookmaker_keys=books,
                )
                actual = provider_actual_cost(result.last_cost, estimated)
                web_module.quota.update(remaining=result.remaining, used=result.used, last_cost=result.last_cost)
                polled_events += 1
                options = price_v2._quotes(result.data, books)
                best_by_book: dict[str, dict[str, Any]] = {}
                for raw in options:
                    option = dict(raw)
                    key = str(option.get("bookmaker_key") or "")
                    if not key:
                        continue
                    prev = best_by_book.get(key)
                    if prev is None or float(option["price"]) > float(prev["price"]):
                        best_by_book[key] = option
                per_candidate[int(candidate["id"])] = best_by_book
                for key, option in best_by_book.items():
                    db.execute(
                        """INSERT INTO first_half_goal_same_book_snapshots(
                               snapshot_id,candidate_id,event_id,slate_key,bookmaker_key,bookmaker_title,
                               market_key,entry_odds,captured_at
                           ) VALUES(?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(snapshot_id,candidate_id,bookmaker_key) DO NOTHING""",
                        (
                            snapshot_id,
                            int(candidate["id"]),
                            str(candidate["event_id"]),
                            slate,
                            key,
                            str(option.get("bookmaker_title") or key),
                            str(option.get("market_key") or ""),
                            float(option["price"]),
                            now.isoformat(),
                        ),
                    )
                web_module.db.record_collector_run(
                    "FHG1_SAME_BOOK_CARD_QUOTES",
                    True,
                    event_id=str(candidate["event_id"]),
                    sport_key=str(candidate["sport_key"]),
                    requested_markets=",".join(price_v2.MARKET_KEYS),
                    estimated_cost=estimated,
                    actual_cost=actual,
                    detail=f"slate={slate}; snapshot={snapshot_id}; books={len(best_by_book)}",
                )
            except Exception as exc:
                msg = f"{type(exc).__name__}: {sanitize_sensitive_text(exc)}"
                errors.append(msg[:400])
                slate_error = msg
                per_candidate[int(candidate["id"])] = {}

        made_common = False
        for size in sorted(theoretical_sizes):
            if size in _existing_placeable_sizes(db, slate):
                continue
            selected = candidates[:size]
            if len(selected) < size:
                continue
            common: Optional[set[str]] = None
            for candidate in selected:
                keys = set(per_candidate.get(int(candidate["id"]), {}).keys())
                common = keys if common is None else common & keys
            common = common or set()
            if not common:
                continue
            ranked = []
            for book in common:
                odds = [float(per_candidate[int(c["id"])][book]["price"]) for c in selected]
                ranked.append((prod(odds), book))
            combined, best_book = max(ranked)
            sample_quote = per_candidate[int(selected[0]["id"])][best_book]
            changed = db.execute(
                """INSERT INTO first_half_goal_placeable_cards(
                       slate_key,card_size,model_version,snapshot_id,captured_at,kickoff_time,
                       bookmaker_key,bookmaker_title,combined_odds,event_ids_json,status
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(slate_key,card_size,model_version) DO NOTHING""",
                (
                    slate,
                    size,
                    fhg.MODEL_VERSION,
                    snapshot_id,
                    now.isoformat(),
                    selected[0]["commence_time"],
                    best_book,
                    str(sample_quote.get("bookmaker_title") or best_book),
                    float(combined),
                    json.dumps([str(c["event_id"]) for c in selected]),
                    "OPEN",
                ),
            )
            created_cards += int(changed or 0)
            made_common = made_common or int(changed or 0) > 0

        _record_same_book_attempt(
            db,
            slate,
            now,
            snapshot_id,
            no_common=not made_common,
            error=slate_error,
        )

    return {
        "ok": not errors,
        "attempted_slates": attempted_slates,
        "polled_events": polled_events,
        "placeable_cards": created_cards,
        "quota_blocked": quota_blocked,
        "max_attempts_per_slate": SAME_BOOK_MAX_ATTEMPTS,
        "errors": errors[:3],
        "betting_authority": False,
    }


def settle_placeable_cards(db, now: Optional[datetime] = None) -> int:
    now = _utc(now)
    terminal = {"SUPERSEDED", "VOID", "POSTPONED", "CANCELLED", "CANCELED", "ABANDONED"}
    settled_count = 0
    for card in db.fetchall("SELECT * FROM first_half_goal_placeable_cards WHERE status='OPEN'"):
        try:
            ids = list(json.loads(card.get("event_ids_json") or "[]"))
        except Exception:
            ids = []
        rows = [
            db.fetchone(
                "SELECT status,fhg_hit FROM first_half_goal_candidates WHERE event_id=?",
                (eid,),
            )
            for eid in ids
        ]
        statuses = {str((r or {}).get("status") or "").upper() for r in rows}
        if statuses & terminal:
            decided = [r for r in rows if r and str(r.get("status") or "") == "SETTLED" and r.get("fhg_hit") is not None]
            hits = sum(int(r.get("fhg_hit") or 0) for r in decided)
            db.execute(
                """UPDATE first_half_goal_placeable_cards
                   SET status='VOID',settled_legs=?,hit_legs=?,all_hit=NULL,settled_at=?
                   WHERE id=? AND status='OPEN'""",
                (len(decided), hits, now.isoformat(), card["id"]),
            )
            settled_count += 1
            continue
        if not rows or any(not r or str(r.get("status") or "") != "SETTLED" or r.get("fhg_hit") is None for r in rows):
            continue
        hits = sum(int(r.get("fhg_hit") or 0) for r in rows)
        db.execute(
            """UPDATE first_half_goal_placeable_cards
               SET status='SETTLED',settled_legs=?,hit_legs=?,all_hit=?,settled_at=?
               WHERE id=? AND status='OPEN'""",
            (len(rows), hits, 1 if hits == len(rows) else 0, now.isoformat(), card["id"]),
        )
        settled_count += 1
    return settled_count


def _weighted_profile(db, team_key: str, ko: datetime, limit: int = 20) -> tuple[int, float, float]:
    try:
        rows = db.fetchall(
            """SELECT home_key,away_key,ht_home,ht_away,played_at
               FROM first_half_goal_history
               WHERE (home_key=? OR away_key=?) AND played_at<?
               ORDER BY played_at DESC,id DESC LIMIT ?""",
            (team_key, team_key, ko.isoformat(), int(limit)),
        )
    except Exception:
        rows = []
    if not rows:
        return 0, 0.0, 0.0
    wsum = scored = conceded = 0.0
    for idx, row in enumerate(rows):
        w = exp(-idx / 8.0)
        is_home = str(row.get("home_key") or "") == team_key
        own = int(row.get("ht_home") or 0) if is_home else int(row.get("ht_away") or 0)
        opp = int(row.get("ht_away") or 0) if is_home else int(row.get("ht_home") or 0)
        wsum += w
        scored += w * int(own > 0)
        conceded += w * int(opp > 0)
    return len(rows), scored / wsum if wsum else 0.0, conceded / wsum if wsum else 0.0


def create_fhg2_predictions(db, now: Optional[datetime] = None) -> int:
    """Prospective-only challenger. Never backfills already-started candidates."""
    _ensure_schema(db)
    now = _utc(now)
    rows = db.fetchall(
        """SELECT * FROM first_half_goal_candidates c
           WHERE c.status='OPEN' AND c.commence_time>?
             AND NOT EXISTS(
                 SELECT 1 FROM first_half_goal_challenger_predictions p WHERE p.candidate_id=c.id
             )
           ORDER BY c.commence_time,c.id""",
        (now.isoformat(),),
    )
    created = 0
    for c in rows:
        ko = fhg._dt(c.get("commence_time"))
        if ko is None:
            continue
        hn, h_score, h_concede = _weighted_profile(db, str(c.get("home_key") or ""), ko)
        an, a_score, a_concede = _weighted_profile(db, str(c.get("away_key") or ""), ko)
        if min(hn, an) < 4:
            continue
        league = float(c.get("league_rate") or 0.70)
        home_goal = (h_score + a_concede) / 2.0
        away_goal = (a_score + h_concede) / 2.0
        raw_any = 1.0 - (1.0 - home_goal) * (1.0 - away_goal)
        reliability = min(1.0, (hn + an) / 24.0)
        p = league + 0.55 * reliability * (raw_any - league)

        xg_values = []
        for i in range(1, 5):
            try:
                value = float(c.get(f"pred{i}_total_xg"))
                if value > 0:
                    xg_values.append(value)
            except Exception:
                pass
        xg_p = None
        if xg_values:
            mean_xg = sum(xg_values) / len(xg_values)
            xg_p = 1.0 - exp(-0.45 * mean_xg)
            p = 0.85 * p + 0.15 * xg_p

        p = max(0.01, min(0.99, p))
        tier = "A" if p >= 0.80 else "B" if p >= 0.75 else "C" if p >= 0.70 else "WATCH"
        changed = db.execute(
            """INSERT INTO first_half_goal_challenger_predictions(
                   candidate_id,event_id,sport_key,commence_time,created_at,model_version,
                   estimated_probability,tier,home_n,away_n,home_score_rate,home_concede_rate,
                   away_score_rate,away_concede_rate,raw_interaction_probability,league_rate,
                   xg_probability,evidence_mode
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(candidate_id) DO NOTHING""",
            (
                int(c["id"]),
                str(c["event_id"]),
                str(c["sport_key"]),
                str(c["commence_time"]),
                now.isoformat(),
                FHG2_VERSION,
                p,
                tier,
                hn,
                an,
                h_score,
                h_concede,
                a_score,
                a_concede,
                raw_any,
                league,
                xg_p,
                "FORWARD",
            ),
        )
        created += int(changed or 0)
    return created


def _fhg2_metrics(db) -> Dict[str, Any]:
    try:
        rows = db.fetchall(
            """SELECT p.estimated_probability,p.tier,c.status,c.fhg_hit
               FROM first_half_goal_challenger_predictions p
               JOIN first_half_goal_candidates c ON c.id=p.candidate_id"""
        )
    except Exception:
        rows = []
    settled = [r for r in rows if str(r.get("status") or "") == "SETTLED" and r.get("fhg_hit") is not None]
    qualifying = [r for r in settled if float(r.get("estimated_probability") or 0) >= float(fhg.THRESHOLD)]
    def metrics(group):
        n = len(group)
        hits = sum(int(r.get("fhg_hit") or 0) for r in group)
        avg = sum(float(r.get("estimated_probability") or 0) for r in group) / n if n else None
        brier = (
            sum((float(r.get("estimated_probability") or 0) - int(r.get("fhg_hit") or 0)) ** 2 for r in group) / n
            if n else None
        )
        return {
            "settled": n,
            "hits": hits,
            "hit_rate_pct": 100.0 * hits / n if n else None,
            "avg_estimated_hit_pct": 100.0 * avg if avg is not None else None,
            "calibration_gap_pct_points": 100.0 * (hits / n - avg) if n and avg is not None else None,
            "brier": brier,
        }
    return {
        "predictions": len(rows),
        "all_settled": metrics(settled),
        "qualified": metrics(qualifying),
        "model_version": FHG2_VERSION,
        "selection_authority": False,
    }


def _price_funnel(db) -> Dict[str, Any]:
    def count(sql, params=()):
        try:
            row = db.fetchone(sql, params) or {}
            return int(row.get("n") or 0)
        except Exception:
            return 0
    threshold = float(fhg.THRESHOLD)
    return {
        "qualified": count("SELECT COUNT(*) n FROM first_half_goal_candidates WHERE estimated_probability>=?", (threshold,)),
        "attempted": count(
            """SELECT COUNT(DISTINCT c.id) n FROM first_half_goal_candidates c
               JOIN first_half_goal_price_diagnostics d ON d.candidate_id=c.id
               WHERE c.estimated_probability>=?""",
            (threshold,),
        ),
        "h1_market_found": count(
            """SELECT COUNT(DISTINCT c.id) n FROM first_half_goal_candidates c
               JOIN first_half_goal_price_diagnostics d ON d.candidate_id=c.id
               WHERE c.estimated_probability>=? AND d.market_present=1""",
            (threshold,),
        ),
        "over_0_5_found": count(
            """SELECT COUNT(DISTINCT c.id) n FROM first_half_goal_candidates c
               JOIN first_half_goal_price_diagnostics d ON d.candidate_id=c.id
               WHERE c.estimated_probability>=? AND d.target_present=1""",
            (threshold,),
        ),
        "best_price_frozen": count(
            """SELECT COUNT(DISTINCT c.id) n FROM first_half_goal_candidates c
               JOIN first_half_goal_entry_prices p ON p.candidate_id=c.id
               WHERE c.estimated_probability>=?""",
            (threshold,),
        ),
        "all_book_quote_candidates": count(
            """SELECT COUNT(DISTINCT c.id) n FROM first_half_goal_candidates c
               JOIN first_half_goal_bookmaker_quotes q ON q.candidate_id=c.id
               WHERE c.estimated_probability>=?""",
            (threshold,),
        ),
        "same_book_snapshot_slates": count("SELECT COUNT(DISTINCT slate_key) n FROM first_half_goal_same_book_snapshots"),
        "placeable_cards": count("SELECT COUNT(*) n FROM first_half_goal_placeable_cards"),
    }


def _lifecycle_metrics(db) -> Dict[str, int]:
    out = {"superseded": 0, "void": 0, "overdue_open": 0}
    try:
        rows = db.fetchall("SELECT lifecycle_status,COUNT(*) n FROM first_half_goal_lifecycle GROUP BY lifecycle_status")
        for row in rows:
            key = str(row.get("lifecycle_status") or "").lower()
            if key in out:
                out[key] = int(row.get("n") or 0)
    except Exception:
        pass
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        row = db.fetchone(
            "SELECT COUNT(*) n FROM first_half_goal_candidates WHERE status='OPEN' AND commence_time<?",
            (cutoff,),
        ) or {}
        out["overdue_open"] = int(row.get("n") or 0)
    except Exception:
        pass
    return out


def _placeable_card_rows(db) -> list[dict[str, Any]]:
    try:
        return [
            dict(x)
            for x in db.fetchall(
                """SELECT * FROM first_half_goal_placeable_cards
                   ORDER BY kickoff_time DESC,card_size"""
            )
        ]
    except Exception:
        return []


def scoreboard_hardened(db) -> Dict[str, Any]:
    base = dict(_ORIGINAL_SCOREBOARD(db))
    base["hardening"] = {
        "version": VERSION,
        "lifecycle": _lifecycle_metrics(db),
        "price_funnel": _price_funnel(db),
        "placeable_cards": _placeable_card_rows(db),
        "fhg2": _fhg2_metrics(db),
        "sofascore_disabled": True,
        "espn_leagues": dict(espn.LEAGUE_SLUGS),
        "betting_authority": False,
    }
    return base


def _fmt(v: Any, digits: int = 1) -> str:
    try:
        return f"{float(v):.{digits}f}"
    except Exception:
        return "—"


def dashboard_hardened(db, style):
    html = _ORIGINAL_DASHBOARD(db, style)
    try:
        h = scoreboard_hardened(db)["hardening"]
        funnel = h["price_funnel"]
        life = h["lifecycle"]
        f2 = h["fhg2"]
        rows = []
        for card in h["placeable_cards"][:30]:
            result = str(card.get("status") or "")
            if result == "SETTLED":
                result = "WON" if int(card.get("all_hit") or 0) else "LOST"
            rows.append(
                "<tr>"
                f"<td>{escape(str(card.get('slate_key') or ''))}</td>"
                f"<td>{int(card.get('card_size') or 0)}</td>"
                f"<td>{escape(str(card.get('bookmaker_title') or card.get('bookmaker_key') or '—'))}</td>"
                f"<td>{_fmt(card.get('combined_odds'),2)}</td>"
                f"<td>{int(card.get('hit_legs') or 0)}/{int(card.get('settled_legs') or 0)}</td>"
                f"<td>{escape(result)}</td>"
                "</tr>"
            )
        if not rows:
            rows = ["<tr><td colspan='6'>No same-book placeable FHG card frozen yet.</td></tr>"]
        q2 = dict(f2.get("qualified") or {})
        panel = f"""
        <div class='panel'><h2>FHG pipeline integrity</h2>
          <div class='fhg'>
            <div class='box'>Qualified<br><b>{funnel['qualified']}</b></div>
            <div class='box'>Price attempted<br><b>{funnel['attempted']}</b></div>
            <div class='box'>H1 market found<br><b>{funnel['h1_market_found']}</b></div>
            <div class='box'>O0.5 found<br><b>{funnel['over_0_5_found']}</b></div>
            <div class='box'>Best price frozen<br><b>{funnel['best_price_frozen']}</b></div>
            <div class='box'>Same-book cards<br><b>{funnel['placeable_cards']}</b></div>
            <div class='box'>Superseded / void<br><b>{life['superseded']} / {life['void']}</b></div>
            <div class='box'>Overdue OPEN<br><b>{life['overdue_open']}</b></div>
          </div>
          <p class='muted'>Placeable cards below require every leg to have Over 0.5 first-half-goal odds at the same bookmaker in one bounded quote snapshot. They are separate from theoretical best-leg cards.</p>
          <div class='table-wrap'><table><thead><tr><th>Slate</th><th>Legs</th><th>Book</th><th>Combined odds</th><th>Hits</th><th>Status</th></tr></thead>
          <tbody>{''.join(rows)}</tbody></table></div>
        </div>
        <div class='panel'><h2>FHG2 challenger</h2>
          <p class='muted'>Prospective-only challenger using recency-weighted team first-half scoring/conceding interaction, league shrinkage and a small xG blend when available. It has no selection authority.</p>
          <div class='fhg'>
            <div class='box'>Forward predictions<br><b>{int(f2.get('predictions') or 0)}</b></div>
            <div class='box'>Qualified settled<br><b>{int(q2.get('settled') or 0)}</b></div>
            <div class='box'>Qualified hit rate<br><b>{_fmt(q2.get('hit_rate_pct'))}%</b></div>
            <div class='box'>Calibration gap<br><b>{_fmt(q2.get('calibration_gap_pct_points'))}pp</b></div>
            <div class='box'>Brier<br><b>{_fmt(q2.get('brier'),3)}</b></div>
          </div>
        </div>
        """
        if "</main>" in html:
            html = html.replace("</main>", panel + "</main>", 1)
        else:
            html = html.replace("</body>", panel + "</body>", 1)
    except Exception:
        pass
    return html


def curated_section_hardened(db):
    html = _ORIGINAL_CURATED_SECTION(db)
    try:
        h = scoreboard_hardened(db)["hardening"]
        funnel = h["price_funnel"]
        life = h["lifecycle"]
        f2 = h["fhg2"]
        q2 = dict(f2.get("qualified") or {})
        extra = (
            "<div class='panel' style='margin-top:12px'>"
            "<strong>Execution-quality FHG evidence</strong><br>"
            f"<span class='muted'>Price funnel {funnel['attempted']}/{funnel['qualified']} attempted · "
            f"{funnel['over_0_5_found']} O0.5 found · {funnel['placeable_cards']} same-book placeable cards. "
            f"Lifecycle: {life['superseded']} superseded, {life['void']} void, {life['overdue_open']} overdue open. "
            f"FHG2: {int(f2.get('predictions') or 0)} forward predictions, "
            f"{int(q2.get('settled') or 0)} qualified settled.</span></div>"
        )
        if "</section>" in html:
            left, right = html.rsplit("</section>", 1)
            html = left + extra + "</section>" + right
    except Exception:
        pass
    return html


def _export_tables() -> None:
    tables = (
        "first_half_goal_lifecycle",
        "first_half_goal_bookmaker_quotes",
        "first_half_goal_price_diagnostics",
        "first_half_goal_same_book_snapshots",
        "first_half_goal_same_book_attempts",
        "first_half_goal_placeable_cards",
        "first_half_goal_challenger_predictions",
    )
    fhg.TABLES = tuple(dict.fromkeys(tuple(fhg.TABLES) + tables))
    try:
        import exporter
        exporter.EXPORT_TABLES = tuple(dict.fromkeys(tuple(exporter.EXPORT_TABLES) + tables))
    except Exception:
        pass
    try:
        import split_exporter
        split_exporter.WEEKLY_TABLES = tuple(dict.fromkeys(tuple(split_exporter.WEEKLY_TABLES) + tables))
        split_exporter.MANUAL_TABLES = tuple(dict.fromkeys(tuple(split_exporter.MANUAL_TABLES) + tables))
    except Exception:
        pass


def install(base_app_module) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_MAINTENANCE, _ORIGINAL_CREATE_CARDS, _ORIGINAL_SETTLE_CARDS
    global _ORIGINAL_SCOREBOARD, _ORIGINAL_DASHBOARD, _ORIGINAL_CURATED_SECTION
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    web_module = base_app_module.web_module
    db = base_app_module.db
    _ensure_schema(db)

    # Retire a source that is continuously 403ing; ESPN is the timely fallback.
    def _sofa_disabled(_db, _now):
        return {
            "open_overdue": 0,
            "attempted": 0,
            "ok": 0,
            "failed": 0,
            "settled": 0,
            "disabled": True,
            "reason": "persistent_403_circuit_breaker",
        }
    free_v2._settle_from_sofascore = _sofa_disabled

    # Verified ESPN soccer slugs for the full Scottish FHG universe.
    espn.LEAGUE_SLUGS.update(
        {
            "soccer_scotland_championship": "sco.2",
            "soccer_scotland_league_one": "sco.3",
            "soccer_scotland_league_two": "sco.4",
        }
    )

    # Fix theoretical-card lifecycle semantics.
    _ORIGINAL_CREATE_CARDS = fhg.create_cards
    _ORIGINAL_SETTLE_CARDS = fhg.settle_cards
    fhg.create_cards = create_cards_safe
    fhg.settle_cards = settle_cards_safe

    # Replace the V2 capture function used by its already-installed maintenance
    # closure. This adds diagnostics/all-book freezes without a second API pass.
    price_v2.capture_entry_prices = capture_entry_prices_v3

    _ORIGINAL_MAINTENANCE = fhg.run_maintenance

    def maintenance_hardened(db_obj, now=None):
        now = _utc(now)
        state = dict(_ORIGINAL_MAINTENANCE(db_obj, now))
        state["lifecycle"] = reconcile_lifecycle(db_obj, now)
        # If lifecycle changes void a card, settle that state immediately.
        state["cards_settled_hardened"] = settle_cards_safe(db_obj, now)
        state["fhg2_created"] = create_fhg2_predictions(db_obj, now)
        state["same_book_capture"] = capture_same_book_cards(web_module, db_obj, now)
        state["placeable_cards_settled"] = settle_placeable_cards(db_obj, now)
        state["hardening_version"] = VERSION
        state["betting_authority"] = False
        return state

    fhg.run_maintenance = maintenance_hardened
    fhg_runtime.run_maintenance = maintenance_hardened

    # Presentation wraps the already-installed price-evidence surface.
    import first_half_goal_curated_patch as curated
    _ORIGINAL_SCOREBOARD = fhg.scoreboard
    _ORIGINAL_DASHBOARD = fhg._dashboard
    _ORIGINAL_CURATED_SECTION = curated._section
    fhg.scoreboard = scoreboard_hardened
    fhg._dashboard = dashboard_hardened
    curated.scoreboard = scoreboard_hardened
    curated._section = curated_section_hardened

    _export_tables()

    startup = {
        "lifecycle": reconcile_lifecycle(db),
        "fhg2_created": create_fhg2_predictions(db),
        "same_book_capture": capture_same_book_cards(web_module, db),
        "placeable_cards_settled": settle_placeable_cards(db),
    }
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "fhg1_model_changed": False,
        "sofascore_disabled": True,
        "espn_scotland_lower_leagues": True,
        "reschedule_void_handling": True,
        "all_book_price_freeze": True,
        "same_book_snapshot_cards": True,
        "same_book_max_attempts_per_slate": SAME_BOOK_MAX_ATTEMPTS,
        "fhg2_model_version": FHG2_VERSION,
        "fhg2_selection_authority": False,
        "startup": startup,
        "betting_authority": False,
    }


__all__ = [
    "VERSION",
    "FHG2_VERSION",
    "install",
    "reconcile_lifecycle",
    "capture_entry_prices_v3",
    "capture_same_book_cards",
    "create_fhg2_predictions",
]
