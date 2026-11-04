from __future__ import annotations

"""Read-only rolling queue snapshots shared by the HR Portfolio and challenger.

Populated from candidates and quoted odds the baseline has ALREADY retrieved
during its normal maintenance. No provider calls, no new selection decisions,
no changes to historical cards, Telegram, stakes or research accounting.
Quality order is a current preview, NOT a frozen challenger card.
"""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any
import json

import hit_rate_multiples_lab as hr
import hr_portfolio_shadow as portfolio
import hr_market_integrity as integrity
from db import utc_now_iso, sanitize_sensitive_text

VERSION = "HR_PORTFOLIO_QUEUE_VISIBILITY_V1"
VISIBLE_CANDIDATES = 120
MIN_REFRESH_SECONDS = 90
DISPLAY_LIMIT = 60
QUOTE_SPREAD_MINUTES = 5.0

BOOK_LABELS = {
    "williamhill": "William Hill",
    "ladbrokes_uk": "Ladbrokes",
}


def _schema(db):
    ident = "BIGSERIAL PRIMARY KEY" if db.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db.execute("""CREATE TABLE IF NOT EXISTS hr_portfolio_queue_state (
        id INTEGER PRIMARY KEY, snapshot_at TEXT NOT NULL,
        version TEXT NOT NULL, source_candidates INTEGER NOT NULL,
        eligible_candidates INTEGER NOT NULL,
        quoted_candidates INTEGER NOT NULL,
        waiting_price_candidates INTEGER NOT NULL,
        excluded_candidates INTEGER NOT NULL,
        visible_candidates INTEGER NOT NULL,
        truncated_candidates INTEGER NOT NULL,
        bookmaker_summary_json TEXT NOT NULL
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS hr_portfolio_queue_rows (
        id {ident}, snapshot_at TEXT NOT NULL, source_lane TEXT NOT NULL,
        source_status TEXT, event_id TEXT NOT NULL,
        sport_key TEXT, market_key TEXT NOT NULL,
        selection TEXT NOT NULL, point REAL,
        home_team TEXT, away_team TEXT, commence_time TEXT,
        source_first_seen_at TEXT, source_odds REAL, min_odds REAL,
        bookmaker_key TEXT, quote_price REAL, quote_captured_at TEXT,
        quote_age_minutes REAL, source_eligible INTEGER NOT NULL,
        queue_status TEXT NOT NULL, baseline_rank INTEGER,
        quality_rank INTEGER, quality_score REAL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_hrqvis_snapshot ON hr_portfolio_queue_rows(snapshot_at)")


def _identity(c):
    return integrity.selection_key(c)


def _quote_time(quote):
    return hr._parse(quote.get("captured_at"))


def _first_seen(c):
    return str(c.get("source_created_at") or c.get("first_seen_at") or "")


def _build_rows(candidates, eligible_lanes, by_book, now):
    """Pure, deterministic assembly from an already-fetched quote cache."""
    # A source candidate can be quoted by more than one bookmaker. Such rows
    # are displayed separately, not conflated into an imaginary one-book price.
    by_identity: dict[tuple[str, ...], dict[str, dict[str, Any]]] = {}
    quote_origin: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}
    for book in portfolio.MANUAL_BOOKS:
        for c, q in by_book.get(book, []):
            key = _identity(c)
            quotes = by_identity.setdefault(key, {})
            ts = _quote_time(q)
            old = quotes.get(book)
            if not old or (ts and ts > _quote_time(old)):
                quotes[book] = dict(q)
                quote_origin[(key, book)] = dict(c)

    # The visibility cap never changes strategy formation. Reserve space
    # for priced legs AND waiting/paused candidates so neither queue section
    # appears empty just because older candidates fill the first 120 slots.
    raw_universe = sorted(candidates, key=lambda c: (
        _first_seen(c), str(c.get("event_id") or ""),
        str(c.get("source_lane") or ""),
    ))
    # Different upstream event IDs can represent exactly the same match and
    # outcome. Display only one economic selection, retain all raw research.
    # Prefer an eligible source when an identical candidate appears in both.
    canonical = {}
    for candidate in raw_universe:
        key = _identity(candidate)
        previous = canonical.get(key)
        if previous is None or (
            str(previous.get("source_lane") or "") not in eligible_lanes
            and str(candidate.get("source_lane") or "") in eligible_lanes
        ):
            canonical[key] = candidate
    full_universe = sorted(canonical.values(), key=lambda c: (
        _first_seen(c), str(c.get("event_id") or ""),
        str(c.get("source_lane") or ""),
    ))
    merged_rows = len(raw_universe)-len(full_universe)
    priced_candidates = [c for c in full_universe
                         if str(c.get("source_lane") or "") in eligible_lanes
                         and integrity.market_actionable(c)
                         and by_identity.get(_identity(c))]
    waiting_candidates = [c for c in full_universe
                          if str(c.get("source_lane") or "") in eligible_lanes
                          and (not integrity.market_actionable(c)
                               or not by_identity.get(_identity(c)))]
    excluded_candidates = [c for c in full_universe
                           if str(c.get("source_lane") or "") not in eligible_lanes]
    wait_kept = waiting_candidates[:min(len(waiting_candidates), 30)]
    paused_kept = excluded_candidates[:min(len(excluded_candidates), 10)]
    room = VISIBLE_CANDIDATES - len(wait_kept) - len(paused_kept)
    priced_kept = priced_candidates[:max(0, room)]
    universe = priced_kept + wait_kept + paused_kept
    if len(universe) < VISIBLE_CANDIDATES:
        seen = {_identity(c) for c in universe}
        for c in full_universe:
            if len(universe) >= VISIBLE_CANDIDATES:
                break
            if _identity(c) not in seen:
                universe.append(c)
                seen.add(_identity(c))
    priced = waiting = excluded = 0
    rows = []
    for c in universe:
        identity = _identity(c)
        books = by_identity.get(identity, {})
        lane_ok = str(c.get("source_lane") or "") in eligible_lanes
        if not lane_ok:
            excluded += 1
            choices = [(None, None, "WATCH_OR_PAUSED")]
        elif not integrity.market_actionable(c):
            waiting += 1
            choices = [(None, None, "MISSING_MARKET_LINE")]
        elif not books:
            waiting += 1
            choices = [(None, None, "WAITING_VALID_QUOTE")]
        else:
            priced += 1
            choices = [(b, q, "FRESH_BOOK_QUOTE") for b, q in books.items()]
        for book, quote, state in choices:
            # A canonical option may come from several upstream IDs. Use the
            # actual source of each bookmaker's qualifying quote for ranking
            # and minimum-odds attribution, not an unrelated duplicate row.
            source = quote_origin.get((identity, book), c) if book else c
            ts = _quote_time(quote) if quote else None
            rows.append({
                "snapshot_at": now.isoformat(),
                "source_lane": str(source.get("source_lane") or ""),
                "source_status": str(source.get("status") or "UPCOMING"),
                "event_id": str(source.get("event_id") or ""),
                "sport_key": source.get("sport_key"),
                "market_key": str(source.get("market_key") or ""),
                "selection": str(source.get("selection") or ""),
                "point": source.get("point"),
                "home_team": source.get("home_team"),
                "away_team": source.get("away_team"),
                "commence_time": source.get("commence_time"),
                "source_first_seen_at": _first_seen(source),
                "source_odds": source.get("source_odds"),
                "min_odds": source.get("min_odds"),
                "bookmaker_key": book,
                "quote_price": float(quote["price"]) if quote else None,
                "quote_captured_at": quote.get("captured_at") if quote else None,
                "quote_age_minutes": max(0, (now-ts).total_seconds()/60) if ts else None,
                "source_eligible": int(lane_ok),
                "queue_status": state,
                "baseline_rank": None, "quality_rank": None, "quality_score": None,
            })

    # Two presentations of precisely the same priced and eligible candidates:
    # FIFO is the baseline; quality is the experimental ranking.
    per_book: dict[str, dict[str, Any]] = {}
    from hr_rolling_quality_challenger import _price_score
    for book in portfolio.MANUAL_BOOKS:
        priced_rows = [r for r in rows
                       if r["bookmaker_key"] == book and r["queue_status"] == "FRESH_BOOK_QUOTE"]
        # Remove duplicate feed entries from readiness statistics and never
        # count ambiguous totals/handicaps as a quoted, actionable selection.
        full_book_pairs = []
        seen_prices = set()
        for c, q in by_book.get(book, []):
            key = _identity(c)
            if not integrity.market_actionable(c) or key in seen_prices:
                continue
            seen_prices.add(key)
            full_book_pairs.append((c, q))
        if not full_book_pairs:
            per_book[book] = {
                "label": BOOK_LABELS[book], "quoted_selections": 0,
                "distinct_events": 0, "baseline_ready": False,
                "reason": "No qualifying fresh prices at this bookmaker",
            }
            continue
        fifo = sorted(priced_rows, key=lambda r: (
            r.get("source_first_seen_at") or "", r["event_id"], r["source_lane"],
        ))
        for i, r in enumerate(fifo, 1):
            r["baseline_rank"] = i
        for r in priced_rows:
            # The exact same scoring function is used by the challenger.
            c = {"min_odds": r["min_odds"]}
            quote = {"price": r["quote_price"], "captured_at": r["quote_captured_at"]}
            coverage = len(by_identity.get(_identity(r), {}))
            detail = _price_score(c, quote, now, coverage)
            r["quality_score"] = detail["score"] if detail else None

        ranked = sorted(priced_rows, key=lambda r: (
            -(r["quality_score"] if r["quality_score"] is not None else -1),
            r.get("source_first_seen_at") or "", r["event_id"], r["source_lane"],
        ))
        for i, r in enumerate(ranked, 1):
            r["quality_rank"] = i

        # Bookmaker readiness MUST use the full candidate universe, not
        # the bounded rows shown on screen. The existing engine evaluates
        # every candidate; the display limit must not create false blockers.
        picks = portfolio._pick_unique(full_book_pairs)
        stamps = [hr._parse(q.get("captured_at")) for _, q in picks]
        coherent = (len(picks) >= 2 and all(stamps) and
                    (max(stamps)-min(stamps)) <= timedelta(minutes=QUOTE_SPREAD_MINUTES))
        events = {str(c.get("event_id") or "") for c, _ in full_book_pairs}
        per_book[book] = {
            "label": BOOK_LABELS[book], "quoted_selections": len(full_book_pairs),
            "distinct_events": len(events), "baseline_ready": bool(coherent),
            "baseline_prefix_legs": len(picks),
            "reason": (
                "Current FIFO prefix has enough same-book, time-coherent legs"
                if coherent else
                "Need at least two distinct fixtures at this bookmaker"
                if len(events) < 2 else
                "FIFO prefix quotes are more than five minutes apart"
            ),
        }

    return rows, {
        "distinct_candidates": len(full_universe),
        "raw_source_rows": len(raw_universe),
        "merged_source_rows": merged_rows,
        "missing_line_candidates": sum(
            str(c.get("source_lane") or "") in eligible_lanes
            and not integrity.market_actionable(c) for c in full_universe
        ),
        "visible_candidates": len(universe),
        "priced_visible_candidates": priced,
        "waiting_visible_candidates": waiting,
        "excluded_visible_candidates": excluded,
        "bookmakers": per_book,
    }


def capture(db, now, candidates, eligible_lanes, by_book):
    """Called by the baseline from the already computed, same-book quote map."""
    _schema(db)
    current = db.fetchone("SELECT snapshot_at FROM hr_portfolio_queue_state WHERE id=1")
    if current:
        last = hr._parse(current.get("snapshot_at"))
        if last and (now-last).total_seconds() < MIN_REFRESH_SECONDS:
            return {"updated": False, "reason": "recent_snapshot"}
    snapshot_at = now.isoformat()
    rows, metrics = _build_rows(candidates, eligible_lanes, by_book, now)
    fields = [
        "snapshot_at", "source_lane", "source_status", "event_id",
        "sport_key", "market_key", "selection", "point",
        "home_team", "away_team", "commence_time", "source_first_seen_at",
        "source_odds", "min_odds", "bookmaker_key", "quote_price",
        "quote_captured_at", "quote_age_minutes", "source_eligible",
        "queue_status", "baseline_rank", "quality_rank", "quality_score",
    ]
    statement = "INSERT INTO hr_portfolio_queue_rows(" + ",".join(fields) + ") VALUES(" + ",".join("?" for _ in fields) + ")"
    for row in rows:
        db.execute(statement, tuple(row.get(k) for k in fields))
    total = int(metrics["distinct_candidates"])
    quoted_count = len({_identity(c) for book_items in by_book.values() for c, _ in book_items
                        if str(c.get("source_lane") or "") in eligible_lanes
                        and integrity.market_actionable(c)})
    # Preserve duplicate raw candidate counts for diagnostics without inflating
    # the unique selection queue; dashboard counts describe economic options.
    seen_canonical = {}
    for c in candidates:
        key = _identity(c)
        seen_canonical.setdefault(key, set()).add(str(c.get("source_lane") or ""))
    excluded_count = sum(not (lanes & set(eligible_lanes)) for lanes in seen_canonical.values())
    eligible_count = total-excluded_count
    book_summary = dict(metrics["bookmakers"])
    book_summary["_diagnostics"] = {
        "raw_source_rows": metrics["raw_source_rows"],
        "merged_source_rows": metrics["merged_source_rows"],
        "missing_line_candidates": metrics["missing_line_candidates"],
    }
    db.execute("""INSERT INTO hr_portfolio_queue_state(
        id,snapshot_at,version,source_candidates,eligible_candidates,
        quoted_candidates,waiting_price_candidates,excluded_candidates,
        visible_candidates,truncated_candidates,bookmaker_summary_json
    ) VALUES(1,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(id) DO UPDATE SET
        snapshot_at=excluded.snapshot_at,version=excluded.version,
        source_candidates=excluded.source_candidates,
        eligible_candidates=excluded.eligible_candidates,
        quoted_candidates=excluded.quoted_candidates,
        waiting_price_candidates=excluded.waiting_price_candidates,
        excluded_candidates=excluded.excluded_candidates,
        visible_candidates=excluded.visible_candidates,
        truncated_candidates=excluded.truncated_candidates,
        bookmaker_summary_json=excluded.bookmaker_summary_json""", (
        snapshot_at, VERSION, total, eligible_count, quoted_count,
        max(0, eligible_count-quoted_count), excluded_count,
        metrics["visible_candidates"],
        max(0, total-metrics["visible_candidates"]),
        json.dumps(book_summary, sort_keys=True),
    ))
    db.execute("DELETE FROM hr_portfolio_queue_rows WHERE snapshot_at<>?", (snapshot_at,))
    return {
        "updated": True, "source_candidates": total, "visible": len(rows),
        "quoted": quoted_count, "waiting": max(0, eligible_count-quoted_count),
        "no_extra_quote_polls": True,
    }


def read(db, *, view="portfolio", limit=DISPLAY_LIMIT):
    _schema(db)
    st = db.fetchone("SELECT * FROM hr_portfolio_queue_state WHERE id=1")
    empty = {
        "snapshot_at": None, "version": VERSION,
        "status": "AWAITING_FIRST_SNAPSHOT", "source_candidates": 0,
        "eligible_candidates": 0, "quoted_candidates": 0,
        "waiting_price_candidates": 0, "excluded_candidates": 0,
        "visible_candidates": 0, "truncated_candidates": 0,
        "bookmakers": {}, "rows": [], "waiting_rows": [], "view": view,
        "method": "shared_cached_snapshot_no_extra_API_calls",
    }
    if not st:
        return empty
    st = dict(st)
    order = (
        "bookmaker_key, quality_rank, source_first_seen_at, event_id"
        if view == "quality" else
        "source_first_seen_at, event_id, bookmaker_key"
    )
    # ORDER BY is a fixed internal literal; the client cannot supply SQL.
    q = ("SELECT * FROM hr_portfolio_queue_rows "
         "WHERE snapshot_at=? AND queue_status='FRESH_BOOK_QUOTE' "
         "ORDER BY " + order + " LIMIT ?")
    rows = [dict(x) for x in db.fetchall(q, (st["snapshot_at"], max(1, min(int(limit), 150))))]
    waiting = [dict(x) for x in db.fetchall(
        """SELECT * FROM hr_portfolio_queue_rows
            WHERE snapshot_at=? AND queue_status!='FRESH_BOOK_QUOTE'
            ORDER BY queue_status,source_first_seen_at,event_id LIMIT ?""",
        (st["snapshot_at"], max(1, min(int(limit)//2, 75))),
    )]
    book_summary = json.loads(st.get("bookmaker_summary_json") or "{}")
    diagnostics = book_summary.pop("_diagnostics", {})
    return {
        "snapshot_at": st["snapshot_at"], "version": VERSION,
        "duplicate_raw_rows_collapsed": int(diagnostics.get("merged_source_rows") or 0),
        "missing_line_candidates": int(diagnostics.get("missing_line_candidates") or 0),
        "status": "SNAPSHOT",
        "source_candidates": int(st["source_candidates"]),
        "eligible_candidates": int(st["eligible_candidates"]),
        "quoted_candidates": int(st["quoted_candidates"]),
        "waiting_price_candidates": int(st["waiting_price_candidates"]),
        "excluded_candidates": int(st["excluded_candidates"]),
        "visible_candidates": int(st["visible_candidates"]),
        "truncated_candidates": int(st["truncated_candidates"]),
        "bookmakers": book_summary,
        "rows": rows, "waiting_rows": waiting, "view": view,
        "method": "shared_cached_snapshot_no_extra_API_calls",
        "note": (
            "Challenger queue is a live rank preview; no separate holds or cards "
            "are formed until baseline creates a new group. Quotes can move."
        ),
    }


def install(app, db):
    """Only add read-only routes/exports; baseline calls capture opportunistically."""
    _schema(db)

    @app.get("/api/hr-portfolio-shadow/queue")
    def portfolio_queue():
        return read(db, view="portfolio")

    @app.get("/api/hr-quality-challenger/queue")
    def quality_queue():
        return read(db, view="quality")

    tables = ("hr_portfolio_queue_state", "hr_portfolio_queue_rows")
    try:
        import exporter
        exporter.EXPORT_TABLES = tuple(dict.fromkeys(tuple(exporter.EXPORT_TABLES) + tables))
    except Exception as exc:
        print("HR_QUEUE_EXPORT_ERROR " + sanitize_sensitive_text(exc), flush=True)
    try:
        import split_exporter
        split_exporter.WEEKLY_TABLES = tuple(dict.fromkeys(tuple(split_exporter.WEEKLY_TABLES) + tables))
        split_exporter.MANUAL_TABLES = tuple(dict.fromkeys(tuple(split_exporter.MANUAL_TABLES) + tables))
    except Exception as exc:
        print("HR_QUEUE_SPLIT_EXPORT_ERROR " + sanitize_sensitive_text(exc), flush=True)
    return {
        "installed": True, "version": VERSION,
        "maximum_cached_candidates": VISIBLE_CANDIDATES,
        "update_interval_seconds": MIN_REFRESH_SECONDS,
        "no_additional_api_calls": True, "betting_authority": False,
        "tables": tables,
    }
