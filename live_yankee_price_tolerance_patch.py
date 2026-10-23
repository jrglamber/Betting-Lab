from __future__ import annotations

"""Forward-only counterfactual price-tolerance research for Live Yankee.

The production/manual Live Yankee feed remains unchanged. This layer reuses the
quotes already stored in live_yankee_quotes and makes zero provider calls.
For each tolerance it records:
- selections that would have been executable at that price compromise;
- selections captured by the relaxed rule that strict P100 missed;
- genuinely same-book four-leg Yankee cards that could have been formed;
- settled selection hit rates and Yankee P/L.

P100 is the strict control. P98/P96/P94/P90/P85 relax only the frozen minimum
acceptable price. The NCAAF current-price 3.00-4.99 guard remains intact so the
experiment changes one thing at a time.
"""

from datetime import datetime, timedelta, timezone
from html import escape
from itertools import combinations
from math import prod
import hashlib
import json
from typing import Any, Dict, Mapping, Optional, Sequence

from fastapi.responses import HTMLResponse

import live_yankee_feed as ly
import hit_rate_multiples_lab as hr
from db import utc_now_iso
from manual_systems_shadow import DEFAULT_PLACEABLE_BOOKS

VERSION = "LIVE_YANKEE_PRICE_TOLERANCE_FORWARD_V1"
VARIANTS = {
    "P100": 1.00,
    "P98": 0.98,
    "P96": 0.96,
    "P94": 0.94,
    "P90": 0.90,
    "P85": 0.85,
}
REFERENCE_STAKE_GBP = 10.0
MAX_CARDS_PER_VARIANT_PER_CYCLE = 3
_INSTALLED = False
_ORIGINAL_MAINTENANCE = None
_ORIGINAL_PAGE = None


def _parse(value: Any) -> Optional[datetime]:
    return ly._parse_iso(value)


def _ensure_schema(db) -> None:
    iid = "BIGSERIAL PRIMARY KEY" if db.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db.execute("""CREATE TABLE IF NOT EXISTS live_yankee_tolerance_meta(
        experiment_key TEXT PRIMARY KEY,
        installed_at TEXT NOT NULL,
        version TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS live_yankee_tolerance_audit(
        candidate_id INTEGER PRIMARY KEY,
        first_observed_at TEXT NOT NULL,
        source_lane TEXT NOT NULL,
        source_table TEXT NOT NULL,
        source_id INTEGER NOT NULL,
        event_id TEXT NOT NULL,
        frozen_min_odds REAL NOT NULL,
        source_odds REAL,
        fresh_quote_seen INTEGER NOT NULL DEFAULT 0,
        best_fresh_price REAL,
        best_price_ratio REAL,
        best_bookmaker_key TEXT,
        result TEXT,
        settled_at TEXT
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS live_yankee_tolerance_eligibility(
        id {iid},
        candidate_id INTEGER NOT NULL,
        variant TEXT NOT NULL,
        tolerance REAL NOT NULL,
        first_eligible_at TEXT NOT NULL,
        bookmaker_key TEXT NOT NULL,
        bookmaker_title TEXT,
        entry_odds REAL NOT NULL,
        price_to_min_ratio REAL NOT NULL,
        result TEXT,
        settled_at TEXT,
        UNIQUE(candidate_id,variant)
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS live_yankee_tolerance_cards(
        id {iid},
        card_key TEXT NOT NULL UNIQUE,
        variant TEXT NOT NULL,
        tolerance REAL NOT NULL,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'OPEN',
        bookmaker_key TEXT NOT NULL,
        bookmaker_title TEXT,
        oldest_quote_age_minutes REAL,
        reference_stake_gbp REAL NOT NULL DEFAULT 10.0,
        result TEXT,
        winning_legs INTEGER,
        return_units REAL,
        pnl_units REAL,
        roi_pct REAL,
        reference_return_gbp REAL,
        reference_pnl_gbp REAL,
        settled_at TEXT
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS live_yankee_tolerance_legs(
        id {iid},
        card_id INTEGER NOT NULL,
        variant TEXT NOT NULL,
        leg_order INTEGER NOT NULL,
        candidate_id INTEGER NOT NULL,
        source_lane TEXT NOT NULL,
        source_table TEXT NOT NULL,
        source_id INTEGER NOT NULL,
        event_id TEXT NOT NULL,
        sport_key TEXT,
        league TEXT,
        market_key TEXT NOT NULL,
        selection TEXT NOT NULL,
        point REAL,
        frozen_min_odds REAL NOT NULL,
        entry_odds REAL NOT NULL,
        price_to_min_ratio REAL NOT NULL,
        bookmaker_key TEXT NOT NULL,
        bookmaker_title TEXT,
        quote_captured_at TEXT NOT NULL,
        quote_age_minutes REAL,
        commence_time TEXT,
        home_team TEXT,
        away_team TEXT,
        result TEXT,
        settled_at TEXT,
        UNIQUE(card_id,leg_order)
    )""")
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_lyta_lane ON live_yankee_tolerance_audit(source_lane)",
        "CREATE INDEX IF NOT EXISTS idx_lyte_variant ON live_yankee_tolerance_eligibility(variant,candidate_id)",
        "CREATE INDEX IF NOT EXISTS idx_lytc_variant ON live_yankee_tolerance_cards(variant,created_at)",
        "CREATE INDEX IF NOT EXISTS idx_lytl_variant ON live_yankee_tolerance_legs(variant,candidate_id)",
    ):
        try:
            db.execute(sql)
        except Exception:
            pass


def _forward_start(db) -> datetime:
    _ensure_schema(db)
    row = db.fetchone(
        "SELECT installed_at FROM live_yankee_tolerance_meta WHERE experiment_key=?",
        (VERSION,),
    )
    if not row:
        stamp = utc_now_iso()
        db.execute(
            "INSERT INTO live_yankee_tolerance_meta(experiment_key,installed_at,version) VALUES(?,?,?) ON CONFLICT(experiment_key) DO NOTHING",
            (VERSION, stamp, VERSION),
        )
        row = db.fetchone(
            "SELECT installed_at FROM live_yankee_tolerance_meta WHERE experiment_key=?",
            (VERSION,),
        ) or {"installed_at": stamp}
    return _parse(row.get("installed_at")) or datetime.now(timezone.utc)


def _allowed_books() -> set[str]:
    values = getattr(ly.web_module.settings, "manual_systems_placeable_bookmaker_keys", None) or DEFAULT_PLACEABLE_BOOKS
    return {str(x) for x in values if str(x)}


def _fresh_quotes(db, candidate: Mapping[str, Any], now: datetime, forward_start: datetime) -> Dict[str, dict]:
    cutoff = max(
        forward_start,
        now - timedelta(minutes=float(ly.QUOTE_FRESHNESS_MINUTES)),
        _parse(candidate.get("first_seen_at")) or forward_start,
        _parse(candidate.get("source_created_at")) or forward_start,
    )
    allowed = _allowed_books()
    rows = db.fetchall(
        """SELECT * FROM live_yankee_quotes
           WHERE event_id=? AND market_key=? AND selection=?
           ORDER BY captured_at DESC,id DESC""",
        (candidate.get("event_id"), candidate.get("market_key"), candidate.get("selection")),
    )
    best: Dict[str, dict] = {}
    for raw in rows:
        row = dict(raw)
        if not ly._same_point(row.get("point"), candidate.get("point")):
            continue
        ts = _parse(row.get("captured_at"))
        if ts is None or ts < cutoff or ts > now:
            continue
        book = str(row.get("bookmaker_key") or "")
        if not book or book not in allowed or book in best:
            continue
        try:
            price = float(row.get("price") or 0)
        except Exception:
            continue
        if price <= 1:
            continue
        # Preserve the live NCAAF execution band; only min-price retention changes.
        if str(candidate.get("source_lane") or "") == "NCAAF_3_TO_4_99" and not (3.0 <= price < 5.0):
            continue
        row["quote_age_minutes"] = max(0.0, (now - ts).total_seconds() / 60.0)
        row["price_to_min_ratio"] = price / float(candidate.get("min_odds") or price)
        best[book] = row
    return best


def _observe_candidates(db, now: datetime, forward_start: datetime) -> Dict[str, int]:
    observed = fresh = eligibility = 0
    rows = db.fetchall("SELECT * FROM live_yankee_candidates ORDER BY id")
    for raw in rows:
        c = dict(raw)
        kickoff = _parse(c.get("commence_time"))
        if kickoff is None or kickoff <= now:
            continue
        cid = int(c.get("id") or 0)
        if not cid:
            continue
        db.execute(
            """INSERT INTO live_yankee_tolerance_audit(
                 candidate_id,first_observed_at,source_lane,source_table,source_id,event_id,
                 frozen_min_odds,source_odds
               ) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(candidate_id) DO NOTHING""",
            (
                cid, utc_now_iso(), str(c.get("source_lane") or ""), str(c.get("source_table") or ""),
                int(c.get("source_id") or 0), str(c.get("event_id") or ""),
                float(c.get("min_odds") or 0), float(c.get("source_odds") or 0),
            ),
        )
        observed += 1
        quotes = _fresh_quotes(db, c, now, forward_start)
        if not quotes:
            continue
        fresh += 1
        best = max(quotes.values(), key=lambda q: (float(q.get("price") or 0), str(q.get("bookmaker_key") or "")))
        price = float(best.get("price") or 0)
        ratio = price / float(c.get("min_odds") or price)
        prior = db.fetchone("SELECT best_price_ratio FROM live_yankee_tolerance_audit WHERE candidate_id=?", (cid,)) or {}
        old_ratio = prior.get("best_price_ratio")
        if old_ratio is None or ratio > float(old_ratio):
            db.execute(
                """UPDATE live_yankee_tolerance_audit
                   SET fresh_quote_seen=1,best_fresh_price=?,best_price_ratio=?,best_bookmaker_key=?
                   WHERE candidate_id=?""",
                (price, ratio, str(best.get("bookmaker_key") or ""), cid),
            )
        else:
            db.execute("UPDATE live_yankee_tolerance_audit SET fresh_quote_seen=1 WHERE candidate_id=?", (cid,))

        for variant, tolerance in VARIANTS.items():
            eligible_quotes = [
                q for q in quotes.values()
                if float(q.get("price") or 0) + 1e-12 >= float(c.get("min_odds") or 0) * tolerance
            ]
            if not eligible_quotes:
                continue
            chosen = max(eligible_quotes, key=lambda q: (float(q.get("price") or 0), str(q.get("bookmaker_key") or "")))
            entry = float(chosen.get("price") or 0)
            changed = db.execute(
                """INSERT INTO live_yankee_tolerance_eligibility(
                     candidate_id,variant,tolerance,first_eligible_at,bookmaker_key,bookmaker_title,
                     entry_odds,price_to_min_ratio
                   ) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(candidate_id,variant) DO NOTHING""",
                (
                    cid, variant, tolerance, utc_now_iso(), str(chosen.get("bookmaker_key") or ""),
                    str(chosen.get("bookmaker_title") or ""), entry,
                    entry / float(c.get("min_odds") or entry),
                ),
            )
            eligibility += int(changed > 0)
    return {"observed": observed, "fresh": fresh, "eligibility_added": eligibility}


def _variant_states(db, variant: str, tolerance: float, now: datetime, forward_start: datetime):
    used = {
        int(r["candidate_id"])
        for r in db.fetchall(
            """SELECT l.candidate_id FROM live_yankee_tolerance_legs l
               JOIN live_yankee_tolerance_cards c ON c.id=l.card_id
               WHERE c.variant=?""",
            (variant,),
        )
    }
    rows = db.fetchall("SELECT * FROM live_yankee_candidates ORDER BY first_seen_at,id")
    states = []
    seen_events = set()
    for raw in rows:
        c = dict(raw)
        cid = int(c.get("id") or 0)
        eid = str(c.get("event_id") or "")
        kickoff = _parse(c.get("commence_time"))
        if not cid or cid in used or not eid or eid in seen_events or kickoff is None or kickoff <= now:
            continue
        quotes = _fresh_quotes(db, c, now, forward_start)
        eligible = {
            book: q for book, q in quotes.items()
            if float(q.get("price") or 0) + 1e-12 >= float(c.get("min_odds") or 0) * tolerance
        }
        if not eligible:
            continue
        states.append({"candidate": c, "quotes": eligible})
        seen_events.add(eid)
    return states


def _pick_same_book(states: Sequence[Mapping[str, Any]]):
    by_book: Dict[str, list[Mapping[str, Any]]] = {}
    for state in states:
        for book in state["quotes"]:
            by_book.setdefault(str(book), []).append(state)
    options = []
    for book, eligible in by_book.items():
        if len(eligible) < 4:
            continue
        chosen = sorted(
            eligible,
            key=lambda s: (
                str(s["candidate"].get("first_seen_at") or ""),
                int(s["candidate"].get("id") or 0),
            ),
        )[:4]
        cs = [dict(s["candidate"]) for s in chosen]
        qs = [dict(s["quotes"][book]) for s in chosen]
        latest_seen = max(str(c.get("first_seen_at") or "") for c in cs)
        card_price = prod(float(q.get("price") or 0) for q in qs)
        options.append((latest_seen, -card_price, book, cs, qs))
    if not options:
        return None
    options.sort(key=lambda x: (x[0], x[1], x[2]))
    _, _, book, cs, qs = options[0]
    return book, cs, qs


def _make_card(db, variant: str, tolerance: float, candidates: Sequence[Mapping[str, Any]], quotes: Sequence[Mapping[str, Any]]) -> Optional[int]:
    ids = [int(c["id"]) for c in candidates]
    book = str(quotes[0].get("bookmaker_key") or "")
    raw = "|".join([VERSION, variant, book] + [str(x) for x in ids])
    key = hashlib.sha256(raw.encode()).hexdigest()
    db.execute(
        """INSERT INTO live_yankee_tolerance_cards(
             card_key,variant,tolerance,created_at,status,bookmaker_key,bookmaker_title,
             oldest_quote_age_minutes,reference_stake_gbp
           ) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(card_key) DO NOTHING""",
        (
            key, variant, tolerance, utc_now_iso(), "OPEN", book,
            str(quotes[0].get("bookmaker_title") or ""),
            max(float(q.get("quote_age_minutes") or 0) for q in quotes), REFERENCE_STAKE_GBP,
        ),
    )
    card = db.fetchone("SELECT * FROM live_yankee_tolerance_cards WHERE card_key=?", (key,))
    if not card:
        return None
    cid = int(card["id"])
    existing = db.fetchone("SELECT COUNT(*) AS n FROM live_yankee_tolerance_legs WHERE card_id=?", (cid,)) or {}
    if int(existing.get("n") or 0):
        return cid
    for order, (c, q) in enumerate(zip(candidates, quotes), 1):
        entry = float(q.get("price") or 0)
        minimum = float(c.get("min_odds") or 0)
        db.execute(
            """INSERT INTO live_yankee_tolerance_legs(
                 card_id,variant,leg_order,candidate_id,source_lane,source_table,source_id,event_id,
                 sport_key,league,market_key,selection,point,frozen_min_odds,entry_odds,
                 price_to_min_ratio,bookmaker_key,bookmaker_title,quote_captured_at,quote_age_minutes,
                 commence_time,home_team,away_team
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cid, variant, order, int(c["id"]), str(c.get("source_lane") or ""),
                str(c.get("source_table") or ""), int(c.get("source_id") or 0), str(c.get("event_id") or ""),
                str(c.get("sport_key") or ""), str(c.get("league") or ""), str(c.get("market_key") or ""),
                str(c.get("selection") or ""), c.get("point"), minimum, entry,
                entry / minimum if minimum > 1 else 1.0, book, str(q.get("bookmaker_title") or ""),
                str(q.get("captured_at") or utc_now_iso()), float(q.get("quote_age_minutes") or 0),
                str(c.get("commence_time") or ""), str(c.get("home_team") or ""), str(c.get("away_team") or ""),
            ),
        )
    return cid


def _form_cards(db, now: datetime, forward_start: datetime) -> Dict[str, int]:
    made = {v: 0 for v in VARIANTS}
    for variant, tolerance in VARIANTS.items():
        for _ in range(MAX_CARDS_PER_VARIANT_PER_CYCLE):
            states = _variant_states(db, variant, tolerance, now, forward_start)
            picked = _pick_same_book(states)
            if not picked:
                break
            _, cs, qs = picked
            if not _make_card(db, variant, tolerance, cs, qs):
                break
            made[variant] += 1
    return made


def _source_result(db, row: Mapping[str, Any]) -> Optional[str]:
    table = str(row.get("source_table") or "")
    allowed = {"football_predictive4_bets", "football_predictive4_market_bets", "multisport_execution_bets"}
    if table not in allowed:
        return None
    try:
        src = db.fetchone(f"SELECT result,status,pnl_units FROM {table} WHERE id=?", (int(row.get("source_id") or 0),)) or {}
    except Exception:
        return None
    result = hr._result(src.get("result"))
    if result:
        return result
    status = str(src.get("status") or "").upper()
    if status in {"VOID", "CANCELLED", "CANCELED"}:
        return "VOID"
    if status == "SETTLED" and src.get("pnl_units") is not None:
        pnl = float(src.get("pnl_units") or 0)
        return "WIN" if pnl > 1e-12 else "LOSS" if pnl < -1e-12 else None
    return None


def _settle(db) -> Dict[str, int]:
    candidate_updates = card_updates = 0
    for audit in db.fetchall("SELECT * FROM live_yankee_tolerance_audit WHERE result IS NULL ORDER BY candidate_id"):
        r = _source_result(db, audit)
        if not r:
            continue
        stamp = utc_now_iso()
        db.execute("UPDATE live_yankee_tolerance_audit SET result=?,settled_at=? WHERE candidate_id=?", (r, stamp, int(audit["candidate_id"])))
        db.execute("UPDATE live_yankee_tolerance_eligibility SET result=?,settled_at=? WHERE candidate_id=? AND result IS NULL", (r, stamp, int(audit["candidate_id"])))
        candidate_updates += 1

    for card in db.fetchall("SELECT * FROM live_yankee_tolerance_cards WHERE status!='SETTLED' ORDER BY id"):
        cid = int(card["id"])
        legs = [dict(x) for x in db.fetchall("SELECT * FROM live_yankee_tolerance_legs WHERE card_id=? ORDER BY leg_order", (cid,))]
        for leg in legs:
            r = hr._result(leg.get("result")) or _source_result(db, leg)
            if r and r != hr._result(leg.get("result")):
                db.execute("UPDATE live_yankee_tolerance_legs SET result=?,settled_at=? WHERE id=?", (r, utc_now_iso(), int(leg["id"])))
                leg["result"] = r
        if len(legs) != 4 or any(hr._result(x.get("result")) is None for x in legs):
            continue
        orders = tuple(int(x["leg_order"]) for x in legs)
        combos = list(combinations(orders, 2)) + list(combinations(orders, 3)) + [orders]
        by_order = {int(x["leg_order"]): x for x in legs}
        line_stake = 1.0 / len(combos)
        ret = 0.0
        for combo in combos:
            odds = 1.0
            lost = False
            for order in combo:
                leg = by_order[order]
                r = hr._result(leg.get("result"))
                if r == "LOSS":
                    lost = True
                    break
                if r == "WIN":
                    odds *= float(leg["entry_odds"])
            if not lost:
                ret += line_stake * odds
        pnl = ret - 1.0
        wins = sum(hr._result(x.get("result")) == "WIN" for x in legs)
        label = "PROFIT" if pnl > 1e-12 else "LOSS" if pnl < -1e-12 else "BREAKEVEN"
        stake = float(card.get("reference_stake_gbp") or REFERENCE_STAKE_GBP)
        db.execute(
            """UPDATE live_yankee_tolerance_cards SET status='SETTLED',result=?,winning_legs=?,
               return_units=?,pnl_units=?,roi_pct=?,reference_return_gbp=?,reference_pnl_gbp=?,settled_at=?
               WHERE id=?""",
            (label, wins, ret, pnl, pnl * 100.0, ret * stake, pnl * stake, utc_now_iso(), cid),
        )
        card_updates += 1
    return {"candidate_results": candidate_updates, "cards_settled": card_updates}


def _variant_metrics(db, variant: str) -> Dict[str, Any]:
    eligible = [dict(x) for x in db.fetchall("SELECT * FROM live_yankee_tolerance_eligibility WHERE variant=? ORDER BY id", (variant,))]
    settled_eligible = [x for x in eligible if hr._result(x.get("result")) in {"WIN", "LOSS"}]
    strict_ids = {int(x["candidate_id"]) for x in db.fetchall("SELECT candidate_id FROM live_yankee_tolerance_eligibility WHERE variant='P100'")}
    incremental = [x for x in eligible if int(x["candidate_id"]) not in strict_ids] if variant != "P100" else []
    settled_incremental = [x for x in incremental if hr._result(x.get("result")) in {"WIN", "LOSS"}]
    cards = [dict(x) for x in db.fetchall("SELECT * FROM live_yankee_tolerance_cards WHERE variant=? ORDER BY id", (variant,))]
    done = [x for x in cards if x.get("pnl_units") is not None]
    pnl = sum(float(x.get("pnl_units") or 0) for x in done)
    return {
        "eligible_candidates": len(eligible),
        "settled_candidates": len(settled_eligible),
        "candidate_wins": sum(hr._result(x.get("result")) == "WIN" for x in settled_eligible),
        "candidate_hit_rate_pct": (
            sum(hr._result(x.get("result")) == "WIN" for x in settled_eligible) / len(settled_eligible) * 100.0
            if settled_eligible else None
        ),
        "incremental_vs_p100": len(incremental),
        "incremental_settled": len(settled_incremental),
        "incremental_wins": sum(hr._result(x.get("result")) == "WIN" for x in settled_incremental),
        "incremental_hit_rate_pct": (
            sum(hr._result(x.get("result")) == "WIN" for x in settled_incremental) / len(settled_incremental) * 100.0
            if settled_incremental else None
        ),
        "cards": len(cards),
        "settled_cards": len(done),
        "profitable_cards": sum(float(x.get("pnl_units") or 0) > 1e-12 for x in done),
        "full_house": sum(int(x.get("winning_legs") or 0) == 4 for x in done),
        "pnl_units": pnl,
        "roi_pct": pnl / len(done) * 100.0 if done else None,
    }


def _source_matrix(db) -> list[dict[str, Any]]:
    lanes = [str(x["source_lane"]) for x in db.fetchall("SELECT DISTINCT source_lane FROM live_yankee_tolerance_audit ORDER BY source_lane")]
    rows = []
    for lane in lanes:
        total = int((db.fetchone("SELECT COUNT(*) AS n FROM live_yankee_tolerance_audit WHERE source_lane=?", (lane,)) or {}).get("n") or 0)
        item: Dict[str, Any] = {"source_lane": lane, "observed": total}
        for variant in VARIANTS:
            item[variant] = int((db.fetchone(
                """SELECT COUNT(*) AS n FROM live_yankee_tolerance_eligibility e
                   JOIN live_yankee_tolerance_audit a ON a.candidate_id=e.candidate_id
                   WHERE e.variant=? AND a.source_lane=?""",
                (variant, lane),
            ) or {}).get("n") or 0)
        rows.append(item)
    return rows


def scoreboard(db) -> Dict[str, Any]:
    start = _forward_start(db)
    audit = db.fetchone("""SELECT COUNT(*) AS observed,
        COALESCE(SUM(CASE WHEN fresh_quote_seen=1 THEN 1 ELSE 0 END),0) AS fresh
        FROM live_yankee_tolerance_audit""") or {}
    return {
        "version": VERSION,
        "forward_start_at": start.isoformat(),
        "provider_calls_added": 0,
        "same_book_required": True,
        "variants": {v: {"tolerance": t, **_variant_metrics(db, v)} for v, t in VARIANTS.items()},
        "observed_candidates": int(audit.get("observed") or 0),
        "fresh_quoted_candidates": int(audit.get("fresh") or 0),
        "source_matrix": _source_matrix(db),
        "selection_rules_changed": False,
        "live_yankee_rules_changed": False,
        "betting_authority_changed": False,
    }


def run_research_maintenance(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    forward_start = _forward_start(db)
    observed = _observe_candidates(db, now, forward_start)
    formed = _form_cards(db, now, forward_start)
    settled = _settle(db)
    out = {
        "ok": True,
        "version": VERSION,
        "observed": observed,
        "formed": formed,
        "settled": settled,
        "provider_calls_added": 0,
        "same_book_required": True,
    }
    try:
        db.record_collector_run("LIVE_YANKEE_PRICE_TOLERANCE_MAINT", True, detail=json.dumps(out, sort_keys=True, default=str))
    except Exception:
        pass
    return out


def _page() -> str:
    s = scoreboard(ly.web_module.db)
    rows = []
    for variant, m in s["variants"].items():
        roi = "—" if m["roi_pct"] is None else f"{float(m['roi_pct']):+.1f}%"
        inc_hr = "—" if m["incremental_hit_rate_pct"] is None else f"{float(m['incremental_hit_rate_pct']):.1f}%"
        rows.append(
            f"<tr><td><strong>{escape(variant)}</strong></td><td>≥{float(m['tolerance'])*100:.0f}%</td>"
            f"<td>{m['eligible_candidates']}</td><td>{m['incremental_vs_p100']}</td>"
            f"<td>{m['incremental_wins']}/{m['incremental_settled']} ({inc_hr})</td>"
            f"<td>{m['cards']}</td><td>{m['settled_cards']}</td><td>{float(m['pnl_units']):+.2f}u</td><td>{roi}</td></tr>"
        )
    source_rows = []
    for x in s["source_matrix"]:
        source_rows.append(
            "<tr><td><strong>" + escape(str(x["source_lane"])) + "</strong></td><td>" + str(x["observed"]) + "</td>" +
            "".join(f"<td>{x[v]}</td>" for v in VARIANTS) + "</tr>"
        )
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>Live Yankee Price Tolerance</title><style>{ly.web_module.BASE_STYLE}</style></head><body>
    <a href='/'>← Betting Lab</a> · <a href='/live-yankee'>Live Yankee Feed</a>
    <h1>Live Yankee Price Tolerance <span class='pill'>FORWARD SHADOW</span></h1>
    <div class='sub'>P100 is the strict-price control. P98/P96/P94/P90/P85 reuse existing Live Yankee quotes and add zero provider calls. Same-book four-leg construction remains mandatory. The production/manual Yankee feed is unchanged. Forward start: {escape(str(s['forward_start_at']))}.</div>
    <div class='grid'><div class='card'><div class='label'>Observed selections</div><div class='value'>{s['observed_candidates']}</div></div>
    <div class='card'><div class='label'>Fresh quoted</div><div class='value'>{s['fresh_quoted_candidates']}</div></div></div>
    <div class='panel'><h2>Tolerance comparison</h2><table><thead><tr><th>Variant</th><th>Min retention</th><th>Eligible legs</th><th>Extra vs P100</th><th>Extra winners</th><th>Same-book Yankees</th><th>Settled</th><th>P&amp;L</th><th>ROI</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
    <div class='panel'><h2>Source-lane eligibility</h2><table><thead><tr><th>Source</th><th>Observed</th>{''.join(f'<th>{v}</th>' for v in VARIANTS)}</tr></thead><tbody>{''.join(source_rows)}</tbody></table></div>
    </body></html>"""


def _install_exports() -> None:
    tables = (
        "live_yankee_tolerance_meta",
        "live_yankee_tolerance_audit",
        "live_yankee_tolerance_eligibility",
        "live_yankee_tolerance_cards",
        "live_yankee_tolerance_legs",
    )
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


def install(db=None) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_MAINTENANCE, _ORIGINAL_PAGE
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}
    db = db or ly.web_module.db
    _ensure_schema(db)
    start = _forward_start(db)
    _install_exports()

    _ORIGINAL_MAINTENANCE = ly.run_maintenance
    def maintenance_with_tolerance(db_obj, collector, settings, now=None):
        state = dict(_ORIGINAL_MAINTENANCE(db_obj, collector, settings, now))
        try:
            state["price_tolerance_research"] = run_research_maintenance(db_obj, now)
        except Exception as exc:
            state["price_tolerance_research"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return state
    ly.run_maintenance = maintenance_with_tolerance

    _ORIGINAL_PAGE = ly._page_html
    def page_with_tolerance_link():
        html = _ORIGINAL_PAGE()
        link = "<a href='/live-yankee-price-tolerance'>Price tolerance research →</a>"
        if "href='/live-yankee-price-tolerance'" not in html:
            html = html.replace("<h1>Live Yankee Feed", "<div>" + link + "</div><h1>Live Yankee Feed", 1)
        return html
    ly._page_html = page_with_tolerance_link

    app = ly.web_module.app
    if not any(getattr(r, "path", None) == "/live-yankee-price-tolerance" for r in app.router.routes):
        @app.get("/live-yankee-price-tolerance", response_class=HTMLResponse)
        def live_yankee_price_tolerance_page():
            return HTMLResponse(_page())

        @app.get("/api/live-yankee-price-tolerance/status")
        def live_yankee_price_tolerance_status():
            return scoreboard(db)

    startup = run_research_maintenance(db)
    _INSTALLED = True
    return {
        "installed": True,
        "version": VERSION,
        "forward_start_at": start.isoformat(),
        "variants": VARIANTS,
        "provider_calls_added": 0,
        "same_book_required": True,
        "live_yankee_rules_changed": False,
        "betting_authority_changed": False,
        "startup": startup,
    }
