from __future__ import annotations

"""Read-only queue -> price -> same-book funnel diagnostics for multiples labs.

This patch adds observability only. It does not change candidate generation,
quote polling, price acceptance, same-book construction, staking, alerts, or
betting authority.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
import inspect
import threading
from typing import Any, Dict, Mapping, Optional, Sequence

from fastapi.responses import HTMLResponse

import credit_policy_unlimited_patch as credit_policy
import dashboard_layout_v2
import hit_rate_multiples_lab as hr
import hit_rate_same_book_only_patch as hr_same
import live_yankee_feed as ly
import live_yankee_same_book_only_patch as ly_same

VERSION = "MULTIPLES_FUNNEL_DIAGNOSTIC_V1"
CACHE_SECONDS = 60
_INSTALLED = False
_ORIGINAL_PAGE = None
_CACHE: Dict[str, Any] = {"at": None, "data": None}
_LOCK = threading.Lock()

_STAGE_RANK = {
    "UNTIMED": 0,
    "TOO_LATE": 0,
    "OUTSIDE_WINDOW": 0,
    "NEVER_QUOTED": 1,
    "QUOTE_STALE": 2,
    "PRICE_MISS": 3,
    "FRESH_STRICT": 4,
}


def _dt(value: Any) -> Optional[datetime]:
    return ly._parse_iso(value)


def _manual_books() -> set[str]:
    return {str(x) for x in getattr(hr, "DEFAULT_PLACEABLE_BOOKS", ()) if str(x)}


def _quote_rows(db, candidates: Sequence[Mapping[str, Any]]) -> Dict[tuple[str, str, str], list[dict]]:
    if not candidates:
        return {}
    starts = []
    for c in candidates:
        parsed = _dt(c.get("first_seen_at") or c.get("source_created_at"))
        if parsed:
            starts.append(parsed)
    # Current queues live inside a 30h formation horizon. A small buffer keeps
    # the history query bounded while retaining the full life of current rows.
    since = min(starts) - timedelta(hours=2) if starts else datetime.now(timezone.utc) - timedelta(hours=36)
    try:
        rows = db.fetchall(
            """SELECT * FROM live_yankee_quotes
               WHERE captured_at>=?
               ORDER BY captured_at DESC,id DESC""",
            (since.isoformat(),),
        )
    except Exception:
        rows = []
    out: Dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    wanted_events = {str(c.get("event_id") or "") for c in candidates}
    for raw in rows:
        row = dict(raw)
        eid = str(row.get("event_id") or "")
        if eid not in wanted_events:
            continue
        key = (eid, str(row.get("market_key") or ""), str(row.get("selection") or ""))
        out[key].append(row)
    return out


def _candidate_quote_state(
    candidate: Mapping[str, Any],
    quote_index: Mapping[tuple[str, str, str], Sequence[Mapping[str, Any]]],
    now: datetime,
    *,
    kind: str,
) -> tuple[str, set[str]]:
    kickoff = _dt(candidate.get("commence_time"))
    if kickoff is None or not str(candidate.get("event_id") or ""):
        return "UNTIMED", set()

    if kind == "YANKEE":
        if kickoff <= now + timedelta(minutes=ly.MIN_LEAD_MINUTES):
            return "TOO_LATE", set()
        if kickoff > now + timedelta(hours=ly.FORMATION_HORIZON_HOURS):
            return "OUTSIDE_WINDOW", set()
        first_seen = _dt(candidate.get("source_created_at"))
        freshness = float(ly.QUOTE_FRESHNESS_MINUTES)
    else:
        if kickoff <= now:
            return "TOO_LATE", set()
        if kickoff > now + timedelta(hours=hr.FORMATION_HORIZON_HOURS):
            return "OUTSIDE_WINDOW", set()
        first_seen = _dt(candidate.get("first_seen_at"))
        freshness = float(hr.QUOTE_FRESHNESS_MINUTES)

    key = (
        str(candidate.get("event_id") or ""),
        str(candidate.get("market_key") or ""),
        str(candidate.get("selection") or ""),
    )
    rows = quote_index.get(key, ())
    cutoff = now - timedelta(minutes=freshness)
    books = _manual_books()
    ever = False
    loose_books: set[str] = set()
    strict_books: set[str] = set()

    for raw in rows:
        row = dict(raw)
        if not ly._same_point(row.get("point"), candidate.get("point")):
            continue
        ts = _dt(row.get("captured_at"))
        if ts is None or ts > now or (first_seen and ts < first_seen):
            continue
        ever = True
        if ts < cutoff:
            continue
        book = str(row.get("bookmaker_key") or "")
        if not book or book not in books:
            continue
        try:
            price = float(row.get("price") or 0)
        except Exception:
            continue
        if price <= 1:
            continue

        if kind == "HR":
            try:
                lo = float(candidate.get("band_min") or 0)
                hi = float(candidate.get("band_max") or 0)
            except Exception:
                continue
            if not (lo <= price < hi):
                continue
        elif str(candidate.get("source_lane") or "") == "NCAAF_3_TO_4_99":
            if not (3.0 <= price < 5.0):
                continue

        loose_books.add(book)
        try:
            min_odds = float(candidate.get("min_odds") or 0)
        except Exception:
            min_odds = 0.0
        if price + 1e-12 >= min_odds:
            strict_books.add(book)

    if strict_books:
        return "FRESH_STRICT", strict_books
    if loose_books:
        return "PRICE_MISS", set()
    if ever:
        return "QUOTE_STALE", set()
    return "NEVER_QUOTED", set()


def _event_funnel(
    candidates: Sequence[Mapping[str, Any]],
    quote_index: Mapping[tuple[str, str, str], Sequence[Mapping[str, Any]]],
    now: datetime,
    *,
    kind: str,
) -> Dict[str, Any]:
    by_event: Dict[str, Dict[str, Any]] = {}
    for raw in candidates:
        candidate = dict(raw)
        event_id = str(candidate.get("event_id") or "") or f"__candidate_{candidate.get('id')}"
        stage, strict_books = _candidate_quote_state(candidate, quote_index, now, kind=kind)
        current = by_event.get(event_id)
        rank = _STAGE_RANK.get(stage, -1)
        if current is None or rank > int(current["rank"]):
            by_event[event_id] = {
                "stage": stage,
                "rank": rank,
                "strict_books": set(strict_books),
            }
        elif rank == int(current["rank"]) and stage == "FRESH_STRICT":
            current["strict_books"].update(strict_books)

    reasons: Dict[str, int] = defaultdict(int)
    book_events: Dict[str, set[str]] = defaultdict(set)
    for event_id, state in by_event.items():
        stage = str(state["stage"])
        reasons[stage] += 1
        if stage == "FRESH_STRICT":
            for book in state["strict_books"]:
                book_events[str(book)].add(event_id)

    max_same_book = max((len(v) for v in book_events.values()), default=0)
    best_books = sorted(book for book, events in book_events.items() if len(events) == max_same_book and max_same_book)
    return {
        "queued_candidates": len(candidates),
        "queued_events": len(by_event),
        "reasons": dict(reasons),
        "actionable_events": sum(reasons.get(x, 0) for x in ("NEVER_QUOTED", "QUOTE_STALE", "PRICE_MISS", "FRESH_STRICT")),
        "ever_priced_events": reasons.get("QUOTE_STALE", 0) + reasons.get("PRICE_MISS", 0) + reasons.get("FRESH_STRICT", 0),
        "fresh_loose_events": reasons.get("PRICE_MISS", 0) + reasons.get("FRESH_STRICT", 0),
        "fresh_strict_events": reasons.get("FRESH_STRICT", 0),
        "max_same_book_events": max_same_book,
        "best_common_books": best_books,
    }


def _recent_expiry_rows(db, table: str, *, policy: Optional[str] = None, exp: Optional[str] = None, system: Optional[str] = None) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    clauses = ["status='EXPIRED'", "expired_at>=?"]
    params: list[Any] = [since]
    if policy is not None:
        clauses.append("policy_version=?")
        params.append(policy)
    if exp is not None:
        clauses.append("experiment_key=?")
        params.append(exp)
    if system is not None:
        clauses.append("system_type=?")
        params.append(system)
    try:
        return [dict(x) for x in db.fetchall(
            f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} ORDER BY expired_at DESC",
            tuple(params),
        )]
    except Exception:
        return []


def _historical_price_state(
    candidate: Mapping[str, Any],
    quote_index: Mapping[tuple[str, str, str], Sequence[Mapping[str, Any]]],
    *,
    kind: str,
) -> tuple[bool, bool]:
    key = (
        str(candidate.get("event_id") or ""),
        str(candidate.get("market_key") or ""),
        str(candidate.get("selection") or ""),
    )
    first_seen = _dt(candidate.get("source_created_at") if kind == "YANKEE" else candidate.get("first_seen_at"))
    end = _dt(candidate.get("expired_at")) or datetime.now(timezone.utc)
    ever = strict = False
    books = _manual_books()
    for raw in quote_index.get(key, ()):
        row = dict(raw)
        if not ly._same_point(row.get("point"), candidate.get("point")):
            continue
        ts = _dt(row.get("captured_at"))
        if ts is None or ts > end or (first_seen and ts < first_seen):
            continue
        ever = True
        book = str(row.get("bookmaker_key") or "")
        if not book or book not in books:
            continue
        try:
            price = float(row.get("price") or 0)
            min_odds = float(candidate.get("min_odds") or 0)
        except Exception:
            continue
        if kind == "HR":
            try:
                lo = float(candidate.get("band_min") or 0)
                hi = float(candidate.get("band_max") or 0)
            except Exception:
                continue
            if not (lo <= price < hi):
                continue
        elif str(candidate.get("source_lane") or "") == "NCAAF_3_TO_4_99" and not (3.0 <= price < 5.0):
            continue
        if price + 1e-12 >= min_odds:
            strict = True
    return ever, strict


def _expired_summary(
    rows: Sequence[Mapping[str, Any]],
    quote_index: Mapping[tuple[str, str, str], Sequence[Mapping[str, Any]]],
    *,
    kind: str,
) -> Dict[str, int]:
    by_event: Dict[str, Dict[str, bool]] = {}
    for row in rows:
        event_id = str(row.get("event_id") or f"id:{row.get('id')}")
        ever, strict = _historical_price_state(row, quote_index, kind=kind)
        state = by_event.setdefault(event_id, {"ever": False, "strict": False})
        state["ever"] = bool(state["ever"] or ever)
        state["strict"] = bool(state["strict"] or strict)
    return {
        "expired_candidates_24h": len(rows),
        "expired_events_24h": len(by_event),
        "expired_ever_priced_events_24h": sum(int(x["ever"]) for x in by_event.values()),
        "expired_ever_strict_events_24h": sum(int(x["strict"]) for x in by_event.values()),
    }


def _poll_stats(db, run_type: str) -> Dict[str, Any]:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        rows = db.fetchall(
            """SELECT ok,actual_cost,detail
               FROM collector_runs
               WHERE run_type=? AND started_at>=?""",
            (run_type, since),
        )
    except Exception:
        rows = []
    zero = failed = credits = 0
    for row in rows:
        failed += int(not bool(row.get("ok")))
        credits += int(row.get("actual_cost") or 0)
        detail = str(row.get("detail") or "").lower()
        if "rows=0" in detail or "quote_rows=0" in detail:
            zero += 1
    return {
        "polls_24h": len(rows),
        "failed_polls_24h": failed,
        "zero_row_polls_24h": zero,
        "credits_24h": credits,
    }


def _backoff_suppressed(db, now: datetime, *, kind: str) -> int:
    try:
        if kind == "YANKEE":
            original = credit_policy._ORIGINAL_LY_TARGETS
            current = ly._targets
        else:
            original = credit_policy._ORIGINAL_HRM_TARGETS
            current = hr._targets
        if original is None:
            return 0
        before = {str(x.get("event_id") or "") for x in original(db, now)}
        after = {str(x.get("event_id") or "") for x in current(db, now)}
        return len(before - after)
    except Exception:
        return 0


def _live_yankee(
    db,
    now: datetime,
    rows: Sequence[Mapping[str, Any]],
    qidx: Mapping[tuple[str, str, str], Sequence[Mapping[str, Any]]],
    expired_rows: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    funnel = _event_funnel(rows, qidx, now, kind="YANKEE")
    try:
        picked = ly_same._pick_four(db, now)
    except Exception:
        picked = None
    funnel.update(
        {
            "name": "Live Yankee",
            "experiment": "YANKEE",
            "system_type": "YANKEE",
            "required_legs": 4,
            "policy_version": ly.ALGORITHM_VERSION,
            "placeable_now": bool(picked),
            "placeable_mode": picked[2] if picked else None,
            "placeable_book": (
                str(picked[1][0].get("bookmaker_key") or "")
                if picked and picked[1] else None
            ),
        }
    )
    funnel.update(_expired_summary(expired_rows, qidx, kind="YANKEE"))
    return funnel


def _hr_stream(
    db,
    now: datetime,
    policy: str,
    exp: str,
    system: str,
    rows: Sequence[Mapping[str, Any]],
    qidx: Mapping[tuple[str, str, str], Sequence[Mapping[str, Any]]],
    expired_rows: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    funnel = _event_funnel(rows, qidx, now, kind="HR")
    need = int(hr.SYSTEMS[system]["legs"])
    try:
        ready = hr._ready(db, policy, exp, system, now)
        picked = hr_same._pick_from_ready(db, ready, need, now) if len(ready) >= need else None
    except Exception:
        picked = None
    funnel.update(
        {
            "name": f"{exp} {system.title()}",
            "experiment": exp,
            "system_type": system,
            "required_legs": need,
            "policy_version": policy,
            "placeable_now": bool(picked),
            "placeable_mode": picked[2] if picked else None,
            "placeable_book": (
                str(picked[1][0].get("bookmaker_key") or "")
                if picked and picked[1] else None
            ),
        }
    )
    funnel.update(_expired_summary(expired_rows, qidx, kind="HR"))
    return funnel


def _diagnostic_uncached(db) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    policy = str(getattr(hr, "POLICY_VERSION", "") or "")

    live_rows = [dict(x) for x in db.fetchall(
        "SELECT * FROM live_yankee_candidates WHERE status='QUEUED' ORDER BY first_seen_at,id"
    )]
    live_expired = _recent_expiry_rows(db, "live_yankee_candidates")
    hr_rows_all = [dict(x) for x in db.fetchall(
        """SELECT * FROM hit_rate_candidates
           WHERE policy_version=? AND status='QUEUED'
           ORDER BY first_seen_at,id""",
        (policy,),
    )]
    hr_expired_all = _recent_expiry_rows(db, "hit_rate_candidates", policy=policy)

    # One bounded quote-history read feeds every stream and the 24h expiry audit.
    qidx = _quote_rows(db, list(live_rows) + list(live_expired) + list(hr_rows_all) + list(hr_expired_all))

    streams = [_live_yankee(db, now, live_rows, qidx, live_expired)]
    for exp in ("HR1", "HR2"):
        for system in ("YANKEE", "HEINZ"):
            current = [
                x for x in hr_rows_all
                if str(x.get("experiment_key") or "") == exp
                and str(x.get("system_type") or "") == system
            ]
            expired = [
                x for x in hr_expired_all
                if str(x.get("experiment_key") or "") == exp
                and str(x.get("system_type") or "") == system
            ]
            streams.append(_hr_stream(db, now, policy, exp, system, current, qidx, expired))

    live_spent = int(ly._credits_today(db, now))
    hr_spent = int(hr._credits_today(db, now))
    controls = {
        "live_yankee": {
            "spent_today": live_spent,
            "daily_cap": int(ly.DAILY_REFRESH_CREDIT_BUDGET),
            "cap_reached": live_spent >= int(ly.DAILY_REFRESH_CREDIT_BUDGET),
            "backoff_suppressed_due_events": _backoff_suppressed(db, now, kind="YANKEE"),
            **_poll_stats(db, "LIVE_YANKEE_QUOTES"),
        },
        "hit_rate": {
            "spent_today": hr_spent,
            "daily_cap": int(hr.DAILY_REFRESH_CREDIT_BUDGET),
            "cap_reached": hr_spent >= int(hr.DAILY_REFRESH_CREDIT_BUDGET),
            "backoff_suppressed_due_events": _backoff_suppressed(db, now, kind="HR"),
            **_poll_stats(db, "HIT_RATE_MULTIPLES_QUOTES"),
        },
    }

    for stream in streams:
        reasons = dict(stream.get("reasons") or {})
        strict = int(stream.get("fresh_strict_events") or 0)
        need = int(stream.get("required_legs") or 0)
        max_book = int(stream.get("max_same_book_events") or 0)
        blockers = []
        if reasons.get("NEVER_QUOTED"):
            blockers.append("NEVER_QUOTED")
        if reasons.get("QUOTE_STALE"):
            blockers.append("QUOTE_STALE")
        if reasons.get("PRICE_MISS"):
            blockers.append("PRICE_MISS")
        if strict < need:
            blockers.append("NOT_ENOUGH_FRESH_LEGS")
        elif not stream.get("placeable_now") and max_book < need:
            blockers.append("NO_COMMON_BOOK")
        elif not stream.get("placeable_now"):
            blockers.append("CONSOLIDATION_RULES")
        if not blockers and stream.get("placeable_now"):
            blockers.append("READY")
        stream["current_blockers"] = blockers

    return {
        "version": VERSION,
        "generated_at": now.isoformat(),
        "policy_version": policy,
        "streams": streams,
        "quote_controls": controls,
        "definitions": {
            "ever_priced": "matching manual-book quote observed after the candidate was frozen",
            "fresh_loose": "matching quote is fresh and inside the frozen odds band, before minimum-price test",
            "fresh_strict": "fresh matching quote passes the candidate minimum-price test",
            "max_same_book": "largest number of distinct fresh-strict events simultaneously available at one supported bookmaker",
            "expiry_window_hours": 24,
        },
        "selection_rules_changed": False,
        "pricing_rules_changed": False,
        "betting_authority_changed": False,
        "provider_calls_added": 0,
    }


def diagnostic(db, *, force: bool = False) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    with _LOCK:
        at = _CACHE.get("at")
        if (
            not force
            and at is not None
            and _CACHE.get("data") is not None
            and (now - at).total_seconds() < CACHE_SECONDS
        ):
            return dict(_CACHE["data"])
        data = _diagnostic_uncached(db)
        _CACHE["at"] = now
        _CACHE["data"] = data
        return dict(data)


def _metric(label: str, value: str, note: str = "", css: str = "") -> str:
    return (
        "<div class='card metric-card'>"
        f"<div class='label'>{escape(label)}</div>"
        f"<div class='value {escape(css)}'>{escape(value)}</div>"
        f"<div class='muted'>{escape(note)}</div></div>"
    )


def _stream_rows(data: Mapping[str, Any]) -> str:
    body = []
    for stream in data.get("streams", []):
        ready = bool(stream.get("placeable_now"))
        status = "READY" if ready else "WAITING"
        css = "good" if ready else ""
        blockers = ", ".join(stream.get("current_blockers") or ()) or "—"
        expired = int(stream.get("expired_events_24h") or 0)
        expired_strict = int(stream.get("expired_ever_strict_events_24h") or 0)
        body.append(
            "<tr>"
            f"<td><strong>{escape(str(stream.get('name') or ''))}</strong>"
            f"<div class='muted'>needs {int(stream.get('required_legs') or 0)} legs</div></td>"
            f"<td>{int(stream.get('queued_events') or 0)}</td>"
            f"<td>{int(stream.get('actionable_events') or 0)}</td>"
            f"<td>{int(stream.get('ever_priced_events') or 0)}</td>"
            f"<td>{int(stream.get('fresh_loose_events') or 0)}</td>"
            f"<td>{int(stream.get('fresh_strict_events') or 0)}</td>"
            f"<td>{int(stream.get('max_same_book_events') or 0)}</td>"
            f"<td class='{css}'>{status}</td>"
            f"<td>{expired}<div class='muted'>{expired_strict} ever hit strict price</div></td>"
            f"<td class='muted'>{escape(blockers)}</td>"
            "</tr>"
        )
    return "".join(body)


def _reason_rows(data: Mapping[str, Any]) -> str:
    rows = []
    for stream in data.get("streams", []):
        reasons = dict(stream.get("reasons") or {})
        rows.append(
            "<tr>"
            f"<td><strong>{escape(str(stream.get('name') or ''))}</strong></td>"
            f"<td>{int(reasons.get('NEVER_QUOTED') or 0)}</td>"
            f"<td>{int(reasons.get('QUOTE_STALE') or 0)}</td>"
            f"<td>{int(reasons.get('PRICE_MISS') or 0)}</td>"
            f"<td>{int(reasons.get('FRESH_STRICT') or 0)}</td>"
            f"<td>{int(reasons.get('OUTSIDE_WINDOW') or 0)}</td>"
            f"<td>{int(reasons.get('TOO_LATE') or 0)}</td>"
            f"<td>{int(reasons.get('UNTIMED') or 0)}</td>"
            "</tr>"
        )
    return "".join(rows)


def _panel(db) -> str:
    data = diagnostic(db)
    ly_ctl = dict(data.get("quote_controls", {}).get("live_yankee") or {})
    hr_ctl = dict(data.get("quote_controls", {}).get("hit_rate") or {})
    ready_count = sum(int(bool(x.get("placeable_now"))) for x in data.get("streams", []))
    stream_table = (
        "<div class='table-wrap'><table><thead><tr>"
        "<th>Stream</th><th>Queue events</th><th>Actionable</th><th>Ever priced</th>"
        "<th>Fresh in band</th><th>Fresh strict</th><th>Max same book</th>"
        "<th>Now</th><th>Expired 24h</th><th>Current blocker</th>"
        "</tr></thead><tbody>"
        + _stream_rows(data)
        + "</tbody></table></div>"
    )
    reason_table = (
        "<details style='margin-top:12px'><summary style='cursor:pointer;font-weight:700'>"
        "Failure reasons by stream <span class='muted'>Tap to expand</span></summary>"
        "<div class='table-wrap' style='margin-top:10px'><table><thead><tr>"
        "<th>Stream</th><th>Never quoted</th><th>Quote stale</th><th>Price miss</th>"
        "<th>Fresh strict</th><th>Outside 30h</th><th>Too late</th><th>Untimed</th>"
        "</tr></thead><tbody>"
        + _reason_rows(data)
        + "</tbody></table></div></details>"
    )
    cards = "".join(
        (
            _metric("Placeable now", str(ready_count), "Live Yankee + HR1/HR2 stream variants"),
            _metric(
                "Yankee quote spend",
                f"{int(ly_ctl.get('spent_today') or 0):,}/{int(ly_ctl.get('daily_cap') or 0):,}",
                f"backoff currently suppresses {int(ly_ctl.get('backoff_suppressed_due_events') or 0)} due events",
                "badx" if ly_ctl.get("cap_reached") else "",
            ),
            _metric(
                "HR quote spend",
                f"{int(hr_ctl.get('spent_today') or 0):,}/{int(hr_ctl.get('daily_cap') or 0):,}",
                f"backoff currently suppresses {int(hr_ctl.get('backoff_suppressed_due_events') or 0)} due events",
                "badx" if hr_ctl.get("cap_reached") else "",
            ),
            _metric(
                "Yankee polls 24h",
                f"{int(ly_ctl.get('polls_24h') or 0):,}",
                f"{int(ly_ctl.get('zero_row_polls_24h') or 0)} returned zero price rows",
            ),
            _metric(
                "HR polls 24h",
                f"{int(hr_ctl.get('polls_24h') or 0):,}",
                f"{int(hr_ctl.get('zero_row_polls_24h') or 0)} returned zero price rows",
            ),
        )
    )
    return (
        "<section id='multiples-funnel'>"
        "<div class='section-title'><div><h2>Multiples Queue → Price Funnel</h2>"
        "<div class='muted'>Read-only diagnostics for Live Yankee, HR1 and HR2. "
        "This shows where queued selections are being lost before a genuinely placeable same-book card is formed.</div>"
        "</div></div>"
        f"<div class='grid compact'>{cards}</div>"
        f"{stream_table}{reason_table}"
        "<div class='muted' style='margin-top:10px'>"
        "Ever priced = a matching quote was observed after the candidate froze. "
        "Fresh in band ignores only the candidate minimum-price test; Fresh strict passes it. "
        "Max same book is the largest number of distinct fresh-strict events simultaneously available at one supported bookmaker. "
        "No provider calls are made by this panel.</div>"
        "</section>"
    )


def install(app, db) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_PAGE
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    _ORIGINAL_PAGE = dashboard_layout_v2._page

    def page_with_funnel(db_obj, base_style: str) -> str:
        html = _ORIGINAL_PAGE(db_obj, base_style)
        if "id='multiples-funnel'" not in html:
            marker = "<section id='leaderboard'>"
            panel = _panel(db_obj)
            if marker in html:
                html = html.replace(marker, panel + "\n\n    " + marker, 1)
            else:
                html = html.replace("</body>", panel + "</body>", 1)
        if "href='#multiples-funnel'" not in html:
            nav_marker = "<a href='#credits'>Credits</a>"
            link = "<a href='#multiples-funnel'>Multiples funnel</a>"
            if nav_marker in html:
                html = html.replace(nav_marker, nav_marker + link, 1)
        return html

    dashboard_layout_v2._page = page_with_funnel

    if not any(getattr(r, "path", None) == "/api/multiples-funnel" for r in app.router.routes):
        @app.get("/api/multiples-funnel")
        def multiples_funnel_api():
            return diagnostic(db)

    # Emit one startup snapshot so Railway logs preserve the initial diagnosis
    # even if nobody opens the dashboard immediately after deployment.
    try:
        snapshot = diagnostic(db, force=True)
        print(
            "MULTIPLES_FUNNEL_DIAGNOSTIC "
            + str(
                {
                    "version": VERSION,
                    "streams": [
                        {
                            "name": x.get("name"),
                            "queue": x.get("queued_events"),
                            "actionable": x.get("actionable_events"),
                            "ever_priced": x.get("ever_priced_events"),
                            "fresh_strict": x.get("fresh_strict_events"),
                            "max_same_book": x.get("max_same_book_events"),
                            "placeable_now": x.get("placeable_now"),
                            "blockers": x.get("current_blockers"),
                        }
                        for x in snapshot.get("streams", [])
                    ],
                    "quote_controls": snapshot.get("quote_controls"),
                }
            ),
            flush=True,
        )
    except Exception as exc:
        print(f"MULTIPLES_FUNNEL_DIAGNOSTIC_ERROR {type(exc).__name__}: {exc}", flush=True)

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "presentation_only": True,
        "api_route": "/api/multiples-funnel",
        "provider_calls_added": 0,
        "selection_rules_changed": False,
        "pricing_rules_changed": False,
        "betting_authority_changed": False,
    }
