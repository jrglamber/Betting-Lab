from __future__ import annotations

"""Forward-only same-book price-tolerance challengers for HR1/HR2.

This research layer reuses quotes already collected by the Hit-Rate Multiples
lane. It makes zero provider/API calls and never places or alerts a bookmaker
bet. Same-book remains mandatory. Only the minimum acceptable price is relaxed
for the challenger variants.

Variants:
- P98: quoted price >= 98% of the candidate's frozen minimum acceptable odds
- P96: quoted price >= 96%
- P94: quoted price >= 94%

The source candidate/pocket is unchanged. Quotes must be fresh and captured
after this experiment was installed, so the evidence is genuinely forward.
"""

from datetime import datetime, timedelta, timezone
from html import escape
from itertools import combinations
from math import prod
import json
import threading
from typing import Any, Dict, Mapping, Optional, Sequence

from fastapi.responses import HTMLResponse

import hit_rate_multiples_lab as hr
from db import sanitize_sensitive_text, utc_now_iso
from manual_systems_shadow import DEFAULT_PLACEABLE_BOOKS

VERSION = "HR_PRICE_TOLERANCE_FORWARD_V1"
VARIANTS = {"P98": 0.98, "P96": 0.96, "P94": 0.94}
REACTION_SECONDS = 120
MAX_CARDS_PER_STREAM_PER_CYCLE = 3

_INSTALLED = False
_STOP = threading.Event()
_THREAD = None
_LOCK = threading.Lock()


def _parse(value: Any) -> Optional[datetime]:
    return hr._parse(value)


def _ensure_schema(db) -> None:
    id_col = "BIGSERIAL PRIMARY KEY" if db.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db.execute("""CREATE TABLE IF NOT EXISTS hit_rate_price_tolerance_meta (
        experiment_key TEXT PRIMARY KEY,
        installed_at TEXT NOT NULL,
        version TEXT NOT NULL
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS hit_rate_price_tolerance_cards (
        id {id_col},
        card_key TEXT NOT NULL UNIQUE,
        variant TEXT NOT NULL,
        tolerance REAL NOT NULL,
        policy_version TEXT NOT NULL,
        experiment_key TEXT NOT NULL,
        system_type TEXT NOT NULL,
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
    db.execute(f"""CREATE TABLE IF NOT EXISTS hit_rate_price_tolerance_legs (
        id {id_col},
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
        "CREATE INDEX IF NOT EXISTS idx_hrpt_cards ON hit_rate_price_tolerance_cards(variant,policy_version,experiment_key,system_type,created_at)",
        "CREATE INDEX IF NOT EXISTS idx_hrpt_legs ON hit_rate_price_tolerance_legs(variant,candidate_id)",
    ):
        try:
            db.execute(sql)
        except Exception:
            pass


def _forward_start(db) -> datetime:
    _ensure_schema(db)
    row = db.fetchone(
        "SELECT installed_at FROM hit_rate_price_tolerance_meta WHERE experiment_key=?",
        (VERSION,),
    )
    if not row:
        stamp = utc_now_iso()
        db.execute(
            """INSERT INTO hit_rate_price_tolerance_meta(experiment_key,installed_at,version)
               VALUES(?,?,?) ON CONFLICT(experiment_key) DO NOTHING""",
            (VERSION, stamp, VERSION),
        )
        row = db.fetchone(
            "SELECT installed_at FROM hit_rate_price_tolerance_meta WHERE experiment_key=?",
            (VERSION,),
        ) or {"installed_at": stamp}
    dt = _parse(row.get("installed_at"))
    return dt or datetime.now(timezone.utc)


def _active_policy(db) -> str:
    row = db.fetchone(
        "SELECT policy_version FROM hit_rate_policy_versions WHERE is_active=1 ORDER BY activated_at DESC LIMIT 1"
    ) or {}
    return str(row.get("policy_version") or hr.POLICY_VERSION)


def _candidate_rows(db, policy: str, exp: str, system: str, variant: str, now: datetime) -> list[dict]:
    rows = db.fetchall(
        """SELECT * FROM hit_rate_candidates
           WHERE policy_version=? AND experiment_key=? AND system_type=?
           ORDER BY first_seen_at,id""",
        (policy, exp, system),
    )
    used = {
        int(x["candidate_id"])
        for x in db.fetchall(
            """SELECT l.candidate_id
               FROM hit_rate_price_tolerance_legs l
               JOIN hit_rate_price_tolerance_cards c ON c.id=l.card_id
               WHERE c.variant=? AND c.policy_version=? AND c.experiment_key=? AND c.system_type=?""",
            (variant, policy, exp, system),
        )
    }
    horizon = now + timedelta(hours=float(hr.FORMATION_HORIZON_HOURS))
    out = []
    seen_events = set()
    for raw in rows:
        c = dict(raw)
        cid = int(c.get("id") or 0)
        if not cid or cid in used:
            continue
        kickoff = _parse(c.get("commence_time"))
        if kickoff is None or kickoff <= now or kickoff > horizon:
            continue
        eid = str(c.get("event_id") or "")
        if not eid or eid in seen_events:
            continue
        out.append(c)
        seen_events.add(eid)
    return out


def _quotes(db, candidate: Mapping[str, Any], tolerance: float, now: datetime, forward_start: datetime) -> Dict[str, dict]:
    rows = db.fetchall(
        """SELECT * FROM live_yankee_quotes
           WHERE event_id=? AND market_key=? AND selection=?
           ORDER BY captured_at DESC,id DESC LIMIT 500""",
        (
            candidate.get("event_id"),
            candidate.get("market_key"),
            candidate.get("selection"),
        ),
    )
    cutoff = max(
        forward_start,
        now - timedelta(minutes=float(hr.QUOTE_FRESHNESS_MINUTES)),
        _parse(candidate.get("first_seen_at")) or forward_start,
    )
    allowed = {str(x) for x in DEFAULT_PLACEABLE_BOOKS if str(x)}
    required = float(candidate.get("min_odds") or 0) * float(tolerance)
    out: Dict[str, dict] = {}
    for raw in rows:
        q = dict(raw)
        book = str(q.get("bookmaker_key") or "")
        if not book or book not in allowed or book in out:
            continue
        if not hr.ly._same_point(q.get("point"), candidate.get("point")):
            continue
        ts = _parse(q.get("captured_at"))
        if ts is None or ts < cutoff or ts > now:
            continue
        try:
            price = float(q.get("price") or 0)
        except Exception:
            continue
        if price <= 1 or price + 1e-12 < required:
            continue
        q["quote_age_minutes"] = max(0.0, (now - ts).total_seconds() / 60.0)
        q["price_to_min_ratio"] = price / float(candidate.get("min_odds") or price)
        out[book] = q
    return out


def _pick_same_book(db, candidates: Sequence[Mapping[str, Any]], need: int, tolerance: float, now: datetime, forward_start: datetime):
    states = []
    for c in candidates:
        q = _quotes(db, c, tolerance, now, forward_start)
        if q:
            states.append({"candidate": dict(c), "quotes": q})
    if len(states) < need:
        return None

    books = sorted({book for state in states for book in state["quotes"]})
    options = []
    for book in books:
        eligible = [state for state in states if book in state["quotes"]]
        if len(eligible) < need:
            continue
        chosen = sorted(
            eligible,
            key=lambda state: (
                str(state["candidate"].get("first_seen_at") or ""),
                int(state["candidate"].get("id") or 0),
            ),
        )[:need]
        candidates_out = [state["candidate"] for state in chosen]
        quotes_out = [dict(state["quotes"][book]) for state in chosen]
        latest_seen = max(str(c.get("first_seen_at") or "") for c in candidates_out)
        card_price = prod(float(q.get("price") or 0) for q in quotes_out)
        options.append((latest_seen, -card_price, book, candidates_out, quotes_out))

    if not options:
        return None
    options.sort(key=lambda x: (x[0], x[1], x[2]))
    _, _, book, chosen_candidates, chosen_quotes = options[0]
    return book, chosen_candidates, chosen_quotes


def _make_card(db, variant: str, tolerance: float, policy: str, exp: str, system: str,
               candidates: Sequence[Mapping[str, Any]], quotes: Sequence[Mapping[str, Any]]) -> Optional[int]:
    ids = [int(c["id"]) for c in candidates]
    book = str(quotes[0].get("bookmaker_key") or "")
    raw_key = "|".join([VERSION, variant, policy, exp, system, book] + [str(x) for x in ids])
    import hashlib
    card_key = hashlib.sha256(raw_key.encode()).hexdigest()
    stamp = utc_now_iso()
    db.execute(
        """INSERT INTO hit_rate_price_tolerance_cards(
             card_key,variant,tolerance,policy_version,experiment_key,system_type,created_at,status,
             bookmaker_key,bookmaker_title,oldest_quote_age_minutes,reference_stake_gbp
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(card_key) DO NOTHING""",
        (
            card_key, variant, tolerance, policy, exp, system, stamp, "OPEN", book,
            str(quotes[0].get("bookmaker_title") or ""),
            max(float(q.get("quote_age_minutes") or 0) for q in quotes),
            float(hr.REFERENCE_STAKE_GBP),
        ),
    )
    card = db.fetchone("SELECT * FROM hit_rate_price_tolerance_cards WHERE card_key=?", (card_key,))
    if not card:
        return None
    cid = int(card["id"])
    existing = db.fetchone("SELECT COUNT(*) AS n FROM hit_rate_price_tolerance_legs WHERE card_id=?", (cid,)) or {}
    if int(existing.get("n") or 0):
        return cid
    for order, (c, q) in enumerate(zip(candidates, quotes), 1):
        entry = float(q.get("price") or 0)
        frozen_min = float(c.get("min_odds") or 0)
        db.execute(
            """INSERT INTO hit_rate_price_tolerance_legs(
                 card_id,variant,leg_order,candidate_id,source_lane,source_table,source_id,event_id,
                 sport_key,league,market_key,selection,point,frozen_min_odds,entry_odds,
                 price_to_min_ratio,bookmaker_key,bookmaker_title,quote_captured_at,quote_age_minutes,
                 commence_time,home_team,away_team
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cid, variant, order, int(c["id"]), str(c.get("source_lane") or ""),
                str(c.get("source_table") or ""), int(c.get("source_id") or 0),
                str(c.get("event_id") or ""), str(c.get("sport_key") or ""),
                str(c.get("league") or ""), str(c.get("market_key") or ""),
                str(c.get("selection") or ""), c.get("point"), frozen_min, entry,
                entry / frozen_min if frozen_min > 1 else 1.0, book,
                str(q.get("bookmaker_title") or ""), str(q.get("captured_at") or stamp),
                float(q.get("quote_age_minutes") or 0), str(c.get("commence_time") or ""),
                str(c.get("home_team") or ""), str(c.get("away_team") or ""),
            ),
        )
    return cid


def _form(db, now: datetime) -> Dict[str, int]:
    forward_start = _forward_start(db)
    policy = _active_policy(db)
    made: Dict[str, int] = {v: 0 for v in VARIANTS}
    for variant, tolerance in VARIANTS.items():
        for exp in ("HR1", "HR2"):
            for system, spec in hr.SYSTEMS.items():
                need = int(spec["legs"])
                for _ in range(MAX_CARDS_PER_STREAM_PER_CYCLE):
                    candidates = _candidate_rows(db, policy, exp, system, variant, now)
                    if len(candidates) < need:
                        break
                    picked = _pick_same_book(db, candidates, need, tolerance, now, forward_start)
                    if not picked:
                        break
                    _, chosen, quotes = picked
                    cid = _make_card(db, variant, tolerance, policy, exp, system, chosen, quotes)
                    if not cid:
                        break
                    made[variant] += 1
    return made


def _result(value: Any) -> Optional[str]:
    return hr._result(value)


def _settle(db) -> Dict[str, int]:
    touched = settled = 0
    cards = db.fetchall("SELECT * FROM hit_rate_price_tolerance_cards WHERE status!='SETTLED' ORDER BY id")
    for card in cards:
        cid = int(card["id"])
        legs = [dict(x) for x in db.fetchall(
            "SELECT * FROM hit_rate_price_tolerance_legs WHERE card_id=? ORDER BY leg_order", (cid,)
        )]
        changed = False
        for leg in legs:
            r = _result(leg.get("result")) or hr._source_result(db, leg)
            if r and r != _result(leg.get("result")):
                db.execute(
                    "UPDATE hit_rate_price_tolerance_legs SET result=?,settled_at=? WHERE id=?",
                    (r, utc_now_iso(), int(leg["id"])),
                )
                leg["result"] = r
                changed = True
        touched += int(changed)
        if not legs or any(_result(x.get("result")) is None for x in legs):
            continue

        system = str(card.get("system_type") or "")
        combos = []
        orders = tuple(int(x["leg_order"]) for x in legs)
        for size in hr.SYSTEMS[system]["sizes"]:
            combos.extend(combinations(orders, size))
        by_order = {int(x["leg_order"]): x for x in legs}
        line_stake = 1.0 / len(combos)
        ret = 0.0
        for combo in combos:
            odds = 1.0
            lost = False
            for order in combo:
                leg = by_order[order]
                result = _result(leg.get("result"))
                if result == "LOSS":
                    lost = True
                    break
                if result == "WIN":
                    odds *= float(leg["entry_odds"])
            if not lost:
                ret += line_stake * odds

        pnl = ret - 1.0
        wins = sum(_result(x.get("result")) == "WIN" for x in legs)
        label = "PROFIT" if pnl > 1e-12 else "LOSS" if pnl < -1e-12 else "BREAKEVEN"
        stake = float(card.get("reference_stake_gbp") or hr.REFERENCE_STAKE_GBP)
        db.execute(
            """UPDATE hit_rate_price_tolerance_cards
               SET status='SETTLED',result=?,winning_legs=?,return_units=?,pnl_units=?,roi_pct=?,
                   reference_return_gbp=?,reference_pnl_gbp=?,settled_at=?
               WHERE id=?""",
            (label, wins, ret, pnl, pnl * 100.0, ret * stake, pnl * stake, utc_now_iso(), cid),
        )
        settled += 1
    return {"touched": touched, "settled": settled}


def _metrics(db, variant: str, exp: str, system: str) -> Dict[str, Any]:
    rows = [dict(x) for x in db.fetchall(
        """SELECT * FROM hit_rate_price_tolerance_cards
           WHERE variant=? AND experiment_key=? AND system_type=? ORDER BY id""",
        (variant, exp, system),
    )]
    done = [x for x in rows if x.get("pnl_units") is not None]
    pnl = sum(float(x.get("pnl_units") or 0) for x in done)
    return {
        "cards": len(rows),
        "settled": len(done),
        "profitable": sum(float(x.get("pnl_units") or 0) > 1e-12 for x in done),
        "pnl_units": pnl,
        "roi_pct": (pnl / len(done) * 100.0) if done else None,
        "full_house": sum(
            int(x.get("winning_legs") or 0) == int(hr.SYSTEMS[system]["legs"]) for x in done
        ),
    }


def scoreboard(db) -> Dict[str, Any]:
    start = _forward_start(db)
    return {
        "version": VERSION,
        "forward_start_at": start.isoformat(),
        "provider_calls_added": 0,
        "same_book_required": True,
        "quote_freshness_minutes": float(hr.QUOTE_FRESHNESS_MINUTES),
        "variants": {
            variant: {
                "tolerance": tolerance,
                "streams": {
                    exp: {system: _metrics(db, variant, exp, system) for system in hr.SYSTEMS}
                    for exp in ("HR1", "HR2")
                },
            }
            for variant, tolerance in VARIANTS.items()
        },
    }


def run_maintenance(db) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    _ensure_schema(db)
    formed = _form(db, now)
    settlement = _settle(db)
    out = {
        "ok": True,
        "version": VERSION,
        "formed": formed,
        "settlement": settlement,
        "provider_calls_added": 0,
        "same_book_required": True,
    }
    try:
        db.record_collector_run(
            "HR_PRICE_TOLERANCE_MAINT", True, detail=json.dumps(out, sort_keys=True, default=str)
        )
    except Exception:
        pass
    print("HR_PRICE_TOLERANCE_MAINT " + json.dumps(out, sort_keys=True, default=str), flush=True)
    return out


def _page(web_module) -> str:
    score = scoreboard(web_module.db)
    rows = []
    for variant, payload in score["variants"].items():
        tol = float(payload["tolerance"]) * 100.0
        for exp in ("HR1", "HR2"):
            for system in ("YANKEE", "HEINZ"):
                m = payload["streams"][exp][system]
                roi = "—" if m["roi_pct"] is None else f"{float(m['roi_pct']):+.1f}%"
                rows.append(
                    f"<tr><td><strong>{escape(variant)}</strong><div class='muted'>≥{tol:.0f}% of frozen min price</div></td>"
                    f"<td>{escape(exp)}</td><td>{escape(system.title())}</td><td>{m['cards']}</td>"
                    f"<td>{m['settled']}</td><td>{m['profitable']}</td><td>{m['full_house']}</td>"
                    f"<td>{float(m['pnl_units']):+.2f}u</td><td>{roi}</td></tr>"
                )
    body = "".join(rows) or "<tr><td colspan='9'>No forward challenger cards yet.</td></tr>"
    return f"""<!doctype html><html><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>HR price tolerance</title><style>{web_module.BASE_STYLE}</style></head><body>
    <a href='/'>← Betting Lab</a> · <a href='/hit-rate-multiples'>Hit-Rate Multiples</a>
    <h1>HR Price Tolerance <span class='pill'>FORWARD SHADOW</span></h1>
    <div class='sub'>Same-book remains mandatory. P98/P96/P94 reuse the existing HR quote cache and make
    <strong>zero additional provider calls</strong>. Only placement price tolerance changes; source pockets and
    selections stay frozen. Forward start: {escape(str(score['forward_start_at']))}.</div>
    <div class='panel'><table><thead><tr><th>Variant</th><th>Pool</th><th>System</th><th>Cards</th>
    <th>Settled</th><th>Profitable</th><th>All legs won</th><th>P&amp;L</th><th>ROI</th></tr></thead>
    <tbody>{body}</tbody></table></div></body></html>"""


def _install_routes(web_module) -> None:
    app = web_module.app
    if not any(getattr(r, "path", None) == "/hit-rate-price-tolerance" for r in app.router.routes):
        @app.get("/hit-rate-price-tolerance", response_class=HTMLResponse)
        def hr_price_tolerance_page():
            return HTMLResponse(_page(web_module))

        @app.get("/api/hit-rate-price-tolerance/status")
        def hr_price_tolerance_status():
            return scoreboard(web_module.db)


def _install_exports() -> None:
    tables = (
        "hit_rate_price_tolerance_meta",
        "hit_rate_price_tolerance_cards",
        "hit_rate_price_tolerance_legs",
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
    global _INSTALLED, _THREAD
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    import web as web_module
    db = db or web_module.db
    _ensure_schema(db)
    forward_start = _forward_start(db)
    _install_exports()
    _install_routes(web_module)

    def loop():
        print(
            f"HR_PRICE_TOLERANCE_HEARTBEAT started reaction_seconds={REACTION_SECONDS} provider_calls_added=0",
            flush=True,
        )
        if _STOP.wait(12):
            return
        while not _STOP.is_set():
            if _LOCK.acquire(blocking=False):
                try:
                    run_maintenance(db)
                except Exception as exc:
                    err = sanitize_sensitive_text(exc)
                    try:
                        db.record_collector_run("HR_PRICE_TOLERANCE_MAINT", False, detail=err)
                    except Exception:
                        pass
                    print(f"HR_PRICE_TOLERANCE_ERROR {type(exc).__name__}: {err}", flush=True)
                finally:
                    _LOCK.release()
            _STOP.wait(REACTION_SECONDS)

    _STOP.clear()
    _THREAD = threading.Thread(target=loop, daemon=True, name="hr-price-tolerance")
    _THREAD.start()
    _INSTALLED = True
    return {
        "installed": True,
        "version": VERSION,
        "variants": VARIANTS,
        "same_book_required": True,
        "provider_calls_added": 0,
        "reaction_seconds": REACTION_SECONDS,
        "forward_start_at": forward_start.isoformat(),
        "dashboard": "/hit-rate-price-tolerance",
    }
