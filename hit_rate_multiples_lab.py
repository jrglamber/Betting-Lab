from __future__ import annotations

"""Forward-only Hit-Rate Multiples Lab.

Independent shadow study. HR1 freezes the strict high-hit pockets selected on
12 Oct 2026; HR2 is the broader challenger. Weekly promotions/relegations use a
new POLICY_VERSION so old candidates/cards/results remain immutable.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
from itertools import combinations
from math import ceil, prod
import hashlib, inspect, json, threading
from typing import Any, Dict, Mapping, Optional, Sequence

from fastapi.responses import HTMLResponse

import live_yankee_feed as ly
from db import sanitize_sensitive_text, utc_now_iso
from manual_systems_shadow import DEFAULT_PLACEABLE_BOOKS
from quota import provider_actual_cost

VERSION = "HIT_RATE_MULTIPLES_V1"
POLICY_VERSION = "HRM_2026-10-12_V1"
REFERENCE_STAKE_GBP = 10.0
QUOTE_REFRESH_MINUTES = 5.0
QUOTE_FRESHNESS_MINUTES = 15.0
FORMATION_HORIZON_HOURS = 30.0
MAX_REFRESH_EVENTS_PER_CYCLE = 8
DAILY_REFRESH_CREDIT_BUDGET = 500
REACTION_SECONDS = 60

MIN_LEG_PRICE_RETENTION = 0.975
MIN_REQUIRED_ODDS_RETENTION = 0.985
MIN_CARD_PRICE_RETENTION = 0.95
SYSTEMS = {
    "YANKEE": {"legs": 4, "sizes": (2, 3, 4)},
    "HEINZ": {"legs": 6, "sizes": (2, 3, 4, 5, 6)},
}
DESIGN_BACKTEST = {
    "window": "2026-09-12 to 2026-10-12",
    "hr1_pool": {"selections": 143, "hit_rate_pct": 57.3, "singles_roi_pct": 14.4},
    "hr1_yankee": {"cards": 18, "roi_pct": 28.8, "profitable": 11, "four_of_four": 0},
    "hr1_heinz": {"cards": 7, "roi_pct": 2.5, "profitable": 2, "six_of_six": 0},
}
POLICY = {
    "HR1": {
        "label": "Strict high-hit pool",
        "pockets": [
            "Football BTTS 1.80-1.99", "Football totals 1.80-1.99",
            "Tennis H2H 1.80-1.99", "EuroLeague H2H 1.50-2.99",
            "NFL H2H 1.50-2.99", "MLB H2H 1.50-1.99",
        ],
    },
    "HR2": {
        "label": "Expanded hit+value challenger",
        "pockets": [
            "HR1 pockets", "Football totals 2.00-2.99",
            "Football H2H 2.50-2.99", "Tennis H2H 2.00-2.99",
        ],
    },
}

_INSTALLED = False
_LOCK = threading.Lock()
_STOP = threading.Event()
_THREAD = None


def _parse(value: Any) -> Optional[datetime]:
    return ly._parse_iso(value)


def _key(policy: str, exp: str, system: str, row: Mapping[str, Any]) -> str:
    point = row.get("point")
    try:
        point_key = "" if point is None else f"{float(point):.8f}".rstrip("0").rstrip(".")
    except Exception:
        point_key = str(point or "")
    raw = "|".join([
        policy, exp, system, str(row.get("event_id") or "").lower(),
        str(row.get("market_key") or "").lower(),
        str(row.get("selection") or "").lower(), point_key,
    ])
    return hashlib.sha256(raw.encode()).hexdigest()


def _ensure_schema(db) -> None:
    ly._ensure_schema(db)
    id_col = "BIGSERIAL PRIMARY KEY" if db.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db.execute("""CREATE TABLE IF NOT EXISTS hit_rate_policy_versions (
        policy_version TEXT PRIMARY KEY, activated_at TEXT NOT NULL,
        deactivated_at TEXT, is_active INTEGER NOT NULL DEFAULT 1,
        policy_json TEXT NOT NULL, design_backtest_json TEXT NOT NULL
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS hit_rate_candidates (
        id {id_col}, candidate_key TEXT NOT NULL UNIQUE,
        policy_version TEXT NOT NULL, experiment_key TEXT NOT NULL, system_type TEXT NOT NULL,
        first_seen_at TEXT NOT NULL, source_created_at TEXT NOT NULL,
        source_lane TEXT NOT NULL, source_table TEXT NOT NULL, source_id INTEGER NOT NULL,
        event_id TEXT NOT NULL, sport_key TEXT, league TEXT, market_key TEXT NOT NULL,
        selection TEXT NOT NULL, point REAL, min_odds REAL NOT NULL, source_odds REAL NOT NULL,
        band_min REAL NOT NULL, band_max REAL NOT NULL, commence_time TEXT NOT NULL,
        home_team TEXT, away_team TEXT, status TEXT NOT NULL DEFAULT 'QUEUED',
        consumed_card_id INTEGER, expired_at TEXT
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS hit_rate_cards (
        id {id_col}, card_key TEXT NOT NULL UNIQUE,
        policy_version TEXT NOT NULL, experiment_key TEXT NOT NULL, system_type TEXT NOT NULL,
        created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN',
        assembly_mode TEXT NOT NULL, source_mix TEXT, bookmaker_mix TEXT,
        oldest_quote_age_minutes REAL, result TEXT, winning_legs INTEGER,
        return_units REAL, pnl_units REAL, roi_pct REAL,
        reference_stake_gbp REAL NOT NULL DEFAULT 10.0,
        reference_return_gbp REAL, reference_pnl_gbp REAL, settled_at TEXT
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS hit_rate_legs (
        id {id_col}, card_id INTEGER NOT NULL, leg_order INTEGER NOT NULL,
        candidate_id INTEGER NOT NULL, source_lane TEXT NOT NULL,
        source_table TEXT NOT NULL, source_id INTEGER NOT NULL,
        event_id TEXT NOT NULL, sport_key TEXT, league TEXT, market_key TEXT NOT NULL,
        selection TEXT NOT NULL, point REAL, entry_odds REAL NOT NULL,
        bookmaker_key TEXT NOT NULL, bookmaker_title TEXT,
        quote_captured_at TEXT NOT NULL, quote_age_minutes REAL,
        min_odds REAL, band_min REAL, band_max REAL,
        commence_time TEXT, home_team TEXT, away_team TEXT,
        result TEXT, settled_at TEXT, UNIQUE(card_id,leg_order)
    )""")
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_hr_queue ON hit_rate_candidates(policy_version,experiment_key,system_type,status,first_seen_at)",
        "CREATE INDEX IF NOT EXISTS idx_hr_cards ON hit_rate_cards(policy_version,experiment_key,system_type,created_at)",
    ):
        try: db.execute(sql)
        except Exception: pass


def _activate_policy(db) -> Dict[str, Any]:
    _ensure_schema(db)
    row = db.fetchone("SELECT * FROM hit_rate_policy_versions WHERE policy_version=?", (POLICY_VERSION,))
    if not row:
        stamp = utc_now_iso()
        db.execute("UPDATE hit_rate_policy_versions SET is_active=0,deactivated_at=? WHERE is_active=1", (stamp,))
        db.execute("""INSERT INTO hit_rate_policy_versions(
            policy_version,activated_at,is_active,policy_json,design_backtest_json
        ) VALUES(?,?,?,?,?)""", (
            POLICY_VERSION, stamp, 1, json.dumps(POLICY, sort_keys=True),
            json.dumps(DESIGN_BACKTEST, sort_keys=True),
        ))
    else:
        db.execute("UPDATE hit_rate_policy_versions SET is_active=CASE WHEN policy_version=? THEN 1 ELSE 0 END", (POLICY_VERSION,))
    return dict(db.fetchone("SELECT * FROM hit_rate_policy_versions WHERE policy_version=?", (POLICY_VERSION,)) or {})


def _actionable(value: Any, now: datetime) -> bool:
    dt = _parse(value)
    return bool(dt and now < dt <= now + timedelta(hours=FORMATION_HORIZON_HOURS))


def _qualify(kind: str, row: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    p = float(row.get("source_odds") or 0)
    market = str(row.get("market_key") or "").lower()
    sport = str(row.get("sport_key") or "").lower()
    out: Dict[str, Dict[str, Any]] = {}
    if kind == "FOOTBALL":
        if market == "btts" and 1.8 <= p < 2:
            m = {"lane": "FOOTBALL_BTTS_1_80_1_99", "lo": 1.8, "hi": 2.0}
            out["HR1"] = dict(m); out["HR2"] = dict(m)
        if market == "totals":
            if 1.8 <= p < 2:
                out["HR1"] = {"lane": "FOOTBALL_TOTALS_1_80_1_99", "lo": 1.8, "hi": 2.0}
            if 1.8 <= p < 3:
                out["HR2"] = {"lane": "FOOTBALL_TOTALS_1_80_2_99", "lo": 1.8, "hi": 3.0}
        if market == "h2h" and 2.5 <= p < 3:
            out["HR2"] = {"lane": "FOOTBALL_H2H_2_50_2_99", "lo": 2.5, "hi": 3.0}
    elif kind == "TENNIS":
        if 1.8 <= p < 2:
            out["HR1"] = {"lane": "TENNIS_H2H_1_80_1_99", "lo": 1.8, "hi": 2.0}
        if 1.8 <= p < 3:
            out["HR2"] = {"lane": "TENNIS_H2H_1_80_2_99", "lo": 1.8, "hi": 3.0}
    elif kind == "MULTISPORT":
        m = None
        if sport == "basketball_euroleague" and 1.5 <= p < 3:
            m = {"lane": "EUROLEAGUE_H2H_1_50_2_99", "lo": 1.5, "hi": 3.0}
        elif "nfl" in sport and 1.5 <= p < 3:
            m = {"lane": "NFL_H2H_1_50_2_99", "lo": 1.5, "hi": 3.0}
        elif sport == "baseball_mlb" and 1.5 <= p < 2:
            m = {"lane": "MLB_H2H_1_50_1_99", "lo": 1.5, "hi": 2.0}
        if m:
            out["HR1"] = dict(m); out["HR2"] = dict(m)
    return out


def _source_rows(db, now: datetime) -> list[dict[str, Any]]:
    specs = (
        ("FOOTBALL", "execution_shadow_bets", """SELECT b.id AS source_id,b.created_at AS source_created_at,b.event_id,
            e.sport_key,e.league,e.commence_time,e.home_team,e.away_team,
            b.market_key,b.selection,b.point,b.min_odds,b.offered_odds AS source_odds
            FROM execution_shadow_bets b JOIN events e ON e.event_id=b.event_id
            WHERE b.status='OPEN' AND e.status='UPCOMING' ORDER BY b.created_at,b.id"""),
        ("TENNIS", "tennis_execution_bets", """SELECT b.id AS source_id,b.created_at AS source_created_at,b.event_id,
            e.sport_key,e.tournament_title AS league,e.commence_time,
            e.player_one AS home_team,e.player_two AS away_team,
            'h2h' AS market_key,b.selection,NULL AS point,b.min_odds,b.offered_odds AS source_odds
            FROM tennis_execution_bets b JOIN tennis_events e ON e.event_id=b.event_id
            WHERE b.status='OPEN' AND e.status='UPCOMING' ORDER BY b.created_at,b.id"""),
        ("MULTISPORT", "multisport_execution_bets", """SELECT b.id AS source_id,b.created_at AS source_created_at,b.event_id,
            e.sport_key,e.league_title AS league,e.commence_time,e.home_team,e.away_team,
            'h2h' AS market_key,b.selection,NULL AS point,b.min_odds,b.offered_odds AS source_odds
            FROM multisport_execution_bets b JOIN multisport_events e ON e.event_id=b.event_id
            WHERE b.status='OPEN' AND e.status='UPCOMING' ORDER BY b.created_at,b.id"""),
    )
    out = []
    for kind, table, sql in specs:
        try: rows = db.fetchall(sql)
        except Exception: rows = []
        for raw in rows:
            row = dict(raw)
            if not _actionable(row.get("commence_time"), now): continue
            if float(row.get("source_odds") or 0) <= 1 or float(row.get("min_odds") or 0) <= 1: continue
            for exp, meta in _qualify(kind, row).items():
                x = dict(row)
                x.update(experiment_key=exp, source_lane=meta["lane"], source_table=table,
                         band_min=float(meta["lo"]), band_max=float(meta["hi"]))
                out.append(x)
    return sorted(out, key=lambda r: (
        str(r.get("source_created_at") or ""), str(r.get("experiment_key") or ""),
        str(r.get("source_lane") or ""), int(r.get("source_id") or 0),
    ))


def _ingest(db, now: datetime) -> Dict[str, int]:
    seen = inserted = 0
    for row in _source_rows(db, now):
        for system in SYSTEMS:
            seen += 1
            stamp = utc_now_iso()
            key = _key(POLICY_VERSION, row["experiment_key"], system, row)
            try:
                inserted += int(db.execute("""INSERT INTO hit_rate_candidates(
                    candidate_key,policy_version,experiment_key,system_type,first_seen_at,source_created_at,
                    source_lane,source_table,source_id,event_id,sport_key,league,market_key,selection,point,
                    min_odds,source_odds,band_min,band_max,commence_time,home_team,away_team,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(candidate_key) DO NOTHING""", (
                    key, POLICY_VERSION, row["experiment_key"], system, stamp,
                    str(row.get("source_created_at") or stamp), row["source_lane"], row["source_table"],
                    int(row.get("source_id") or 0), str(row.get("event_id") or ""),
                    str(row.get("sport_key") or ""), str(row.get("league") or ""),
                    str(row.get("market_key") or "h2h"), str(row.get("selection") or ""),
                    row.get("point"), float(row.get("min_odds") or 0),
                    float(row.get("source_odds") or 0), float(row["band_min"]),
                    float(row["band_max"]), str(row.get("commence_time") or ""),
                    str(row.get("home_team") or ""), str(row.get("away_team") or ""), "QUEUED",
                )) > 0)
            except Exception:
                pass
    return {"source_stream_rows": seen, "inserted": inserted}


def _expire(db, now: datetime) -> int:
    n = 0
    for row in db.fetchall("SELECT id,commence_time FROM hit_rate_candidates WHERE status='QUEUED'"):
        dt = _parse(row.get("commence_time"))
        if dt is None or dt <= now:
            n += db.execute("UPDATE hit_rate_candidates SET status='EXPIRED',expired_at=? WHERE id=? AND status='QUEUED'",
                            (utc_now_iso(), int(row["id"])))
    return n


def _credits_today(db, now: datetime) -> int:
    row = db.fetchone("""SELECT COALESCE(SUM(actual_cost),0) AS n FROM collector_runs
        WHERE run_type='HIT_RATE_MULTIPLES_QUOTES' AND started_at>=?""",
        (now.date().isoformat() + "T00:00:00+00:00",)) or {}
    return int(row.get("n") or 0)


def _targets(db, now: datetime) -> list[dict[str, Any]]:
    rows = [dict(x) for x in db.fetchall("SELECT * FROM hit_rate_candidates WHERE status='QUEUED' ORDER BY first_seen_at,id")]
    streams: Dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for x in rows:
        if _actionable(x.get("commence_time"), now):
            streams[(x["policy_version"], x["experiment_key"], x["system_type"])].add(str(x["event_id"]))
    if not any(len(v) >= 2 for v in streams.values()): return []
    events: Dict[str, Dict[str, Any]] = {}
    for x in rows:
        if not _actionable(x.get("commence_time"), now): continue
        eid = str(x["event_id"])
        e = events.setdefault(eid, {
            "event_id": eid, "sport_key": str(x.get("sport_key") or ""),
            "commence_time": str(x.get("commence_time") or ""),
            "first_seen_at": str(x.get("first_seen_at") or ""), "markets": set(),
        })
        e["markets"].add(str(x.get("market_key") or "h2h"))
    due = []
    for e in events.values():
        last = db.fetchone("""SELECT MAX(started_at) AS at FROM collector_runs
            WHERE run_type='HIT_RATE_MULTIPLES_QUOTES' AND event_id=? AND ok=1""", (e["event_id"],)) or {}
        last_dt = _parse(last.get("at"))
        if last_dt and (now - last_dt).total_seconds() / 60 < QUOTE_REFRESH_MINUTES: continue
        e["markets"] = tuple(sorted(e["markets"])); due.append(e)
    return sorted(due, key=lambda x: (x["first_seen_at"], x["commence_time"], x["event_id"]))


def _refresh(db, collector, settings, now: datetime) -> Dict[str, Any]:
    books = tuple(getattr(settings, "manual_systems_placeable_bookmaker_keys", DEFAULT_PLACEABLE_BOOKS) or DEFAULT_PLACEABLE_BOOKS)
    books = tuple(x for x in books if x in DEFAULT_PLACEABLE_BOOKS) or DEFAULT_PLACEABLE_BOOKS
    spent = _credits_today(db, now)
    if spent >= DAILY_REFRESH_CREDIT_BUDGET:
        return {"polled": 0, "reason": "daily_budget", "spent": spent}
    polled = inserted = 0
    for target in _targets(db, now)[:MAX_REFRESH_EVENTS_PER_CYCLE]:
        markets = tuple(target.get("markets") or ())
        estimated = max(1, ceil(len(books) / 10)) * max(1, len(markets))
        if spent + estimated > DAILY_REFRESH_CREDIT_BUDGET: break
        decision = collector.quota.decide(estimated)
        if not decision.allowed:
            return {"polled": polled, "inserted": inserted, "reason": decision.reason, "spent": spent}
        actual = 0
        try:
            result = collector.api.event_odds(
                target["sport_key"], target["event_id"], collector.region,
                markets, bookmaker_keys=books,
            )
            actual = provider_actual_cost(result.last_cost, estimated)
            collector.quota.update(remaining=result.remaining, used=result.used, last_cost=result.last_cost)
            added = ly._store_quotes(db, result.data, target["event_id"], utc_now_iso(), markets)
            db.record_collector_run("HIT_RATE_MULTIPLES_QUOTES", True,
                event_id=target["event_id"], sport_key=target["sport_key"],
                requested_markets=",".join(markets), estimated_cost=estimated,
                actual_cost=actual, detail=f"books={','.join(books)}; quote_rows={added}")
            polled += 1; inserted += added; spent += actual
        except Exception as exc:
            db.record_collector_run("HIT_RATE_MULTIPLES_QUOTES", False,
                event_id=target["event_id"], sport_key=target["sport_key"],
                requested_markets=",".join(markets), estimated_cost=estimated,
                actual_cost=actual, detail=f"error={sanitize_sensitive_text(exc)}")
    return {"polled": polled, "inserted": inserted, "reason": "ok", "spent": spent}


def _quotes(db, c: Mapping[str, Any], now: datetime, allow_min_miss: bool = False) -> Dict[str, Dict[str, Any]]:
    rows = db.fetchall("""SELECT * FROM live_yankee_quotes
        WHERE event_id=? AND market_key=? AND selection=?
        ORDER BY captured_at DESC,id DESC LIMIT 500""",
        (c["event_id"], c["market_key"], c["selection"]))
    cutoff = now - timedelta(minutes=QUOTE_FRESHNESS_MINUTES)
    first_seen = _parse(c.get("first_seen_at"))
    out: Dict[str, Dict[str, Any]] = {}
    for raw in rows:
        q = dict(raw); book = str(q.get("bookmaker_key") or "")
        if book in out or book not in DEFAULT_PLACEABLE_BOOKS: continue
        if not ly._same_point(q.get("point"), c.get("point")): continue
        ts = _parse(q.get("captured_at"))
        if ts is None or ts < cutoff or ts > now or (first_seen and ts < first_seen): continue
        price = float(q.get("price") or 0)
        if not (float(c["band_min"]) <= price < float(c["band_max"])): continue
        if not allow_min_miss and price + 1e-12 < float(c.get("min_odds") or 0): continue
        q["quote_age_minutes"] = max(0.0, (now - ts).total_seconds() / 60.0)
        out[book] = q
    return out


def _choose(db, candidates: Sequence[Mapping[str, Any]], now: datetime):
    maps = [_quotes(db, c, now, False) for c in candidates]
    if any(not x for x in maps): return None
    common = set(maps[0])
    for x in maps[1:]: common.intersection_update(x)
    if common:
        book = max(common, key=lambda b: (prod(float(x[b]["price"]) for x in maps), b))
        return [dict(x[book]) for x in maps], "SAME_BOOK_VERIFIED"
    best = [dict(max(x.values(), key=lambda q: (float(q["price"]), str(q.get("bookmaker_key") or "")))) for x in maps]
    best_prod = prod(float(x["price"]) for x in best)
    anchors: Dict[str, int] = defaultdict(int)
    for q in best: anchors[str(q.get("bookmaker_key") or "")] += 1
    min_anchor = 2 if len(candidates) == 4 else 3
    options = []
    for book, n in anchors.items():
        if n < min_anchor: continue
        proposed = []; retentions = []; ok = True
        for c, bq in zip(candidates, best):
            alt = _quotes(db, c, now, True).get(book)
            if alt is None: ok = False; break
            ap = float(alt["price"]); bp = float(bq["price"]); min_odds = float(c.get("min_odds") or 0)
            if ap / bp < MIN_LEG_PRICE_RETENTION - 1e-12: ok = False; break
            if min_odds > 1 and ap / min_odds < MIN_REQUIRED_ODDS_RETENTION - 1e-12: ok = False; break
            proposed.append(dict(alt)); retentions.append(ap / bp)
        if not ok: continue
        combined = prod(float(x["price"]) for x in proposed) / best_prod
        if combined >= MIN_CARD_PRICE_RETENTION - 1e-12:
            options.append((n, combined, min(retentions), book, proposed))
    if options:
        options.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
        return options[0][4], "SAME_BOOK_CONSOLIDATED"
    return best, "CROSS_BOOK_SYNTHETIC"


def _ready(db, policy: str, exp: str, system: str, now: datetime):
    rows = db.fetchall("""SELECT * FROM hit_rate_candidates
        WHERE policy_version=? AND experiment_key=? AND system_type=? AND status='QUEUED'
        ORDER BY first_seen_at,id""", (policy, exp, system))
    out = []; used_events = set()
    for raw in rows:
        c = dict(raw); eid = str(c["event_id"])
        if eid in used_events or not _actionable(c.get("commence_time"), now): continue
        if not _quotes(db, c, now, False): continue
        out.append(c); used_events.add(eid)
    return out


def _make_card(db, policy: str, exp: str, system: str,
               candidates: Sequence[Mapping[str, Any]],
               quotes: Sequence[Mapping[str, Any]], mode: str) -> Optional[int]:
    ids = [int(x["id"]) for x in candidates]
    card_key = hashlib.sha256("|".join([VERSION, policy, exp, system] + [str(x) for x in ids]).encode()).hexdigest()
    db.execute("""INSERT INTO hit_rate_cards(
        card_key,policy_version,experiment_key,system_type,created_at,status,
        assembly_mode,source_mix,bookmaker_mix,oldest_quote_age_minutes,reference_stake_gbp
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(card_key) DO NOTHING""", (
        card_key, policy, exp, system, utc_now_iso(), "OPEN", mode,
        ",".join(sorted({str(x.get("source_lane") or "") for x in candidates})),
        ",".join(sorted({str(x.get("bookmaker_key") or "") for x in quotes})),
        max(float(x.get("quote_age_minutes") or 0) for x in quotes), REFERENCE_STAKE_GBP,
    ))
    card = db.fetchone("SELECT * FROM hit_rate_cards WHERE card_key=?", (card_key,))
    if not card: return None
    cid = int(card["id"])
    if int((db.fetchone("SELECT COUNT(*) AS n FROM hit_rate_legs WHERE card_id=?", (cid,)) or {}).get("n") or 0):
        return cid
    for order, (c, q) in enumerate(zip(candidates, quotes), 1):
        db.execute("""INSERT INTO hit_rate_legs(
            card_id,leg_order,candidate_id,source_lane,source_table,source_id,
            event_id,sport_key,league,market_key,selection,point,entry_odds,
            bookmaker_key,bookmaker_title,quote_captured_at,quote_age_minutes,
            min_odds,band_min,band_max,commence_time,home_team,away_team
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            cid, order, int(c["id"]), c["source_lane"], c["source_table"], int(c["source_id"]),
            c["event_id"], c.get("sport_key"), c.get("league"), c["market_key"], c["selection"], c.get("point"),
            float(q["price"]), q["bookmaker_key"], q.get("bookmaker_title"), q["captured_at"],
            float(q.get("quote_age_minutes") or 0), float(c.get("min_odds") or 0),
            float(c["band_min"]), float(c["band_max"]), c.get("commence_time"),
            c.get("home_team"), c.get("away_team"),
        ))
        db.execute("UPDATE hit_rate_candidates SET status='CONSUMED',consumed_card_id=? WHERE id=? AND status='QUEUED'",
                   (cid, int(c["id"])))
    return cid


def _form(db, now: datetime) -> Dict[str, int]:
    formed = verified = consolidated = cross = 0
    streams = db.fetchall("""SELECT DISTINCT policy_version,experiment_key,system_type
        FROM hit_rate_candidates WHERE status='QUEUED'
        ORDER BY policy_version,experiment_key,system_type""")
    for s in streams:
        policy = str(s["policy_version"]); exp = str(s["experiment_key"]); system = str(s["system_type"])
        need = int(SYSTEMS[system]["legs"])
        for _ in range(3):
            ready = _ready(db, policy, exp, system, now)
            if len(ready) < need: break
            candidates = ready[:need]; picked = _choose(db, candidates, now)
            if not picked: break
            quotes, mode = picked
            if not _make_card(db, policy, exp, system, candidates, quotes, mode): break
            formed += 1
            verified += int(mode == "SAME_BOOK_VERIFIED")
            consolidated += int(mode == "SAME_BOOK_CONSOLIDATED")
            cross += int(mode == "CROSS_BOOK_SYNTHETIC")
    return {"formed": formed, "same_book_verified": verified,
            "same_book_consolidated": consolidated, "cross_book": cross}


def _result(value: Any) -> Optional[str]:
    t = str(value or "").strip().upper()
    if t in {"WIN", "WON", "WINNER"}: return "WIN"
    if t in {"LOSS", "LOSE", "LOST", "LOSER"}: return "LOSS"
    if t in {"PUSH", "VOID", "REFUND", "CANCELLED", "CANCELED"}: return "PUSH" if t == "PUSH" else "VOID"
    return None


def _source_result(db, leg: Mapping[str, Any]) -> Optional[str]:
    table = str(leg.get("source_table") or "")
    if table not in {"execution_shadow_bets", "tennis_execution_bets", "multisport_execution_bets"}: return None
    try: row = db.fetchone(f"SELECT result,status,pnl_units FROM {table} WHERE id=?", (int(leg["source_id"]),)) or {}
    except Exception: return None
    r = _result(row.get("result"))
    if r: return r
    if str(row.get("status") or "").upper() in {"VOID", "CANCELLED", "CANCELED"}: return "VOID"
    if str(row.get("status") or "").upper() == "SETTLED" and row.get("pnl_units") is not None:
        pnl = float(row.get("pnl_units") or 0)
        return "WIN" if pnl > 1e-12 else "LOSS" if pnl < -1e-12 else None
    return None


def _combos(system: str, orders: Sequence[int]):
    spec = SYSTEMS[system]
    out = []
    for size in spec["sizes"]: out.extend(combinations(tuple(orders), size))
    return out


def settle_cards(db) -> Dict[str, int]:
    touched = settled = 0
    for card in db.fetchall("SELECT * FROM hit_rate_cards WHERE status!='SETTLED' ORDER BY id"):
        cid = int(card["id"])
        legs = [dict(x) for x in db.fetchall("SELECT * FROM hit_rate_legs WHERE card_id=? ORDER BY leg_order", (cid,))]
        changed = False
        for leg in legs:
            r = _result(leg.get("result")) or _source_result(db, leg)
            if r and r != _result(leg.get("result")):
                db.execute("UPDATE hit_rate_legs SET result=?,settled_at=? WHERE id=?",
                           (r, utc_now_iso(), int(leg["id"])))
                leg["result"] = r; changed = True
        touched += int(changed)
        if not legs or any(_result(x.get("result")) is None for x in legs): continue
        combos = _combos(str(card["system_type"]), [int(x["leg_order"]) for x in legs])
        by_order = {int(x["leg_order"]): x for x in legs}; line_stake = 1.0 / len(combos); ret = 0.0
        for combo in combos:
            odds = 1.0; lost = False
            for order in combo:
                leg = by_order[order]; r = _result(leg.get("result"))
                if r == "LOSS": lost = True; break
                if r == "WIN": odds *= float(leg["entry_odds"])
            if not lost: ret += line_stake * odds
        pnl = ret - 1.0
        wins = sum(_result(x.get("result")) == "WIN" for x in legs)
        label = "PROFIT" if pnl > 1e-12 else "LOSS" if pnl < -1e-12 else "BREAKEVEN"
        db.execute("""UPDATE hit_rate_cards SET status='SETTLED',result=?,winning_legs=?,
            return_units=?,pnl_units=?,roi_pct=?,reference_return_gbp=?,
            reference_pnl_gbp=?,settled_at=? WHERE id=?""", (
            label, wins, ret, pnl, pnl * 100.0, ret * REFERENCE_STAKE_GBP,
            pnl * REFERENCE_STAKE_GBP, utc_now_iso(), cid,
        ))
        settled += 1
    return {"touched": touched, "settled": settled}


def _metrics(db, policy: str, exp: str, system: str) -> Dict[str, Any]:
    c = db.fetchone("""SELECT COUNT(*) AS total,
        SUM(CASE WHEN status='QUEUED' THEN 1 ELSE 0 END) AS queued,
        SUM(CASE WHEN status='CONSUMED' THEN 1 ELSE 0 END) AS consumed,
        SUM(CASE WHEN status='EXPIRED' THEN 1 ELSE 0 END) AS expired,
        COUNT(DISTINCT CASE WHEN status='QUEUED' THEN event_id END) AS queued_events
        FROM hit_rate_candidates WHERE policy_version=? AND experiment_key=? AND system_type=?""",
        (policy, exp, system)) or {}
    cards = [dict(x) for x in db.fetchall("""SELECT * FROM hit_rate_cards
        WHERE policy_version=? AND experiment_key=? AND system_type=? ORDER BY id""",
        (policy, exp, system))]
    done = [x for x in cards if x.get("pnl_units") is not None]
    pnl = sum(float(x.get("pnl_units") or 0) for x in done)
    return {
        "candidates": int(c.get("total") or 0), "queued": int(c.get("queued") or 0),
        "queued_events": int(c.get("queued_events") or 0), "consumed": int(c.get("consumed") or 0),
        "expired": int(c.get("expired") or 0), "cards": len(cards), "settled": len(done),
        "profitable": sum(float(x.get("pnl_units") or 0) > 1e-12 for x in done),
        "pnl_units": pnl, "roi_pct": (pnl / len(done) * 100.0) if done else None,
        "at_least_2": sum(int(x.get("winning_legs") or 0) >= 2 for x in done),
        "at_least_3": sum(int(x.get("winning_legs") or 0) >= 3 for x in done),
        "full_house": sum(int(x.get("winning_legs") or 0) == int(SYSTEMS[system]["legs"]) for x in done),
        "same_book_verified": sum(x.get("assembly_mode") == "SAME_BOOK_VERIFIED" for x in cards),
        "same_book_consolidated": sum(x.get("assembly_mode") == "SAME_BOOK_CONSOLIDATED" for x in cards),
        "cross_book": sum(x.get("assembly_mode") == "CROSS_BOOK_SYNTHETIC" for x in cards),
    }


def scoreboard(db) -> Dict[str, Any]:
    _ensure_schema(db)
    active = db.fetchone("SELECT * FROM hit_rate_policy_versions WHERE is_active=1 ORDER BY activated_at DESC LIMIT 1") or {}
    policy = str(active.get("policy_version") or POLICY_VERSION)
    return {
        "version": VERSION, "active_policy_version": policy, "activated_at": active.get("activated_at"),
        "policy": POLICY, "design_backtest": DESIGN_BACKTEST,
        "streams": {exp: {system: _metrics(db, policy, exp, system) for system in SYSTEMS} for exp in POLICY},
        "quote_refresh_minutes": QUOTE_REFRESH_MINUTES, "quote_freshness_minutes": QUOTE_FRESHNESS_MINUTES,
    }


def run_maintenance(db, collector, settings, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    _activate_policy(db)
    try:
        out = {"ok": True, "version": VERSION, "policy_version": POLICY_VERSION,
               "expired": _expire(db, now), "ingested": _ingest(db, now)}
        out["quotes"] = _refresh(db, collector, settings, now)
        out["formed"] = _form(db, now)
        out["settlement"] = settle_cards(db)
        out["scoreboard"] = scoreboard(db)
        db.record_collector_run("HIT_RATE_MULTIPLES_MAINT", True,
                                detail=json.dumps(out, sort_keys=True, default=str))
        print("HIT_RATE_MULTIPLES_MAINT " + json.dumps(out, sort_keys=True, default=str), flush=True)
        return out
    except Exception as exc:
        err = sanitize_sensitive_text(exc)
        db.record_collector_run("HIT_RATE_MULTIPLES_MAINT", False, detail=err)
        print(f"HIT_RATE_MULTIPLES_MAINT_ERROR {type(exc).__name__}: {err}", flush=True)
        return {"ok": False, "error": err}


def _fmt(value: Any) -> str:
    dt = _parse(value)
    return dt.strftime("%d %b %H:%M UTC") if dt else "—"


def _label(row: Mapping[str, Any]) -> str:
    market = str(row.get("market_key") or "").lower()
    selection = str(row.get("selection") or "—")
    point = row.get("point")
    if market == "h2h": return selection
    if market == "btts": return f"BTTS {selection}"
    if market == "totals": return f"{selection} {point}" if point is not None else selection
    return f"{market.replace('_',' ').title()} {selection}".strip()


def _card_html(db, card: Mapping[str, Any]) -> str:
    legs = db.fetchall("SELECT * FROM hit_rate_legs WHERE card_id=? ORDER BY leg_order", (int(card["id"]),))
    rows = []
    for x in legs:
        r = str(x.get("result") or "PENDING").upper()
        cls = "ok" if r == "WIN" else "bad" if r == "LOSS" else ""
        fixture = f"{x.get('home_team') or ''} v {x.get('away_team') or ''}".strip()
        rows.append(f"<tr><td>{x['leg_order']}</td><td>{escape(str(x.get('source_lane') or ''))}</td>"
                    f"<td>{escape(fixture)}</td><td>{escape(_label(x))}</td>"
                    f"<td>{float(x.get('entry_odds') or 0):.2f}</td>"
                    f"<td>{escape(str(x.get('bookmaker_title') or x.get('bookmaker_key') or ''))}</td>"
                    f"<td class='{cls}'><strong>{r}</strong></td></tr>")
    pnl = card.get("reference_pnl_gbp")
    summary = "PENDING" if pnl is None else (
        f"{int(card.get('winning_legs') or 0)}/{len(legs)} winners · "
        f"return £{float(card.get('reference_return_gbp') or 0):.2f} · "
        f"P/L £{float(pnl):+.2f} · ROI {float(card.get('roi_pct') or 0):+.1f}%"
    )
    note = "<div class='muted'>Cross-book synthetic: research result, not one verified one-book system.</div>" if card.get("assembly_mode") == "CROSS_BOOK_SYNTHETIC" else ""
    return (f"<div class='card' style='margin:12px 0'><strong>{card['experiment_key']} "
            f"{str(card['system_type']).title()} #{card['id']}</strong> · {card['assembly_mode']} · {summary}"
            f"<div class='table-wrap' style='margin-top:8px'><table><thead><tr><th>#</th><th>Pocket</th>"
            f"<th>Event</th><th>Pick</th><th>Odds</th><th>Book</th><th>Result</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>{note}</div>")


def _page(web_module) -> str:
    db = web_module.db
    try: settle_cards(db)
    except Exception: pass
    s = scoreboard(db); policy = str(s["active_policy_version"])
    stream = []
    for exp in ("HR1", "HR2"):
        for system in ("YANKEE", "HEINZ"):
            m = s["streams"][exp][system]
            roi = "—" if m["roi_pct"] is None else f"{float(m['roi_pct']):+.1f}%"
            stream.append(f"<tr><td>{exp}</td><td>{system.title()}</td><td>{m['queued']}</td>"
                          f"<td>{m['queued_events']}</td><td>{m['cards']}</td><td>{m['settled']}</td>"
                          f"<td>{m['profitable']}</td><td>{m['at_least_3']}</td><td>{m['full_house']}</td><td>{roi}</td></tr>")
    now = datetime.now(timezone.utc); queue = []
    for c in db.fetchall("""SELECT * FROM hit_rate_candidates WHERE policy_version=?
        AND system_type='YANKEE' AND status='QUEUED' ORDER BY first_seen_at,id LIMIT 80""", (policy,)):
        qs = _quotes(db, c, now, False); best = max([float(q["price"]) for q in qs.values()], default=None)
        queue.append(f"<tr><td>{c['experiment_key']}</td><td>{escape(str(c['source_lane']))}</td>"
                     f"<td>{escape(str(c.get('home_team') or ''))} v {escape(str(c.get('away_team') or ''))}</td>"
                     f"<td>{escape(_label(c))}</td><td>{float(c['source_odds']):.2f}</td>"
                     f"<td>{'WAIT' if best is None else f'{best:.2f}'}</td><td>{len(qs)}</td><td>{_fmt(c.get('commence_time'))}</td></tr>")
    cards = "".join(_card_html(db, x) for x in db.fetchall("SELECT * FROM hit_rate_cards ORDER BY id DESC LIMIT 30"))
    b = DESIGN_BACKTEST
    hr1 = "".join(f"<li>{escape(x)}</li>" for x in POLICY["HR1"]["pockets"])
    hr2 = "".join(f"<li>{escape(x)}</li>" for x in POLICY["HR2"]["pockets"])
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Hit-Rate Multiples Lab</title><style>{web_module.BASE_STYLE}</style></head><body><a href='/'>← Betting Lab</a>
<h1>Hit-Rate Multiples Lab <span class='pill'>SHADOW · FORWARD ONLY · NO EXECUTION</span></h1>
<div class='sub'>High-hit-rate/value pockets, assembled chronologically. Yankees are primary; Heinz runs independently as a benchmark.</div>
<div class='grid'><div class='card'><div class='label'>POLICY</div><div class='value' style='font-size:17px'>{policy}</div></div>
<div class='card'><div class='label'>STARTED</div><div class='value' style='font-size:17px'>{_fmt(s.get('activated_at'))}</div></div>
<div class='card'><div class='label'>QUOTE REFRESH</div><div class='value'>{QUOTE_REFRESH_MINUTES:.0f}m</div></div>
<div class='card'><div class='label'>STAKE CONTROL</div><div class='value'>£{REFERENCE_STAKE_GBP:.0f}</div></div></div>
<div class='section-grid'><div class='panel'><h2>HR1 — strict</h2><ul>{hr1}</ul></div>
<div class='panel'><h2>HR2 — challenger</h2><ul>{hr2}</ul></div></div>
<div class='panel'><h2>Frozen design backtest</h2><div class='summary'>HR1: <strong>{b['hr1_pool']['selections']} selections · {b['hr1_pool']['hit_rate_pct']:.1f}% hit · {b['hr1_pool']['singles_roi_pct']:+.1f}% singles ROI</strong>. Yankees: <strong>{b['hr1_yankee']['cards']} · {b['hr1_yankee']['roi_pct']:+.1f}% ROI</strong>, no 4/4. Heinz: <strong>{b['hr1_heinz']['cards']} · {b['hr1_heinz']['roi_pct']:+.1f}% ROI</strong>. Design-only; forward evidence below.</div></div>
<div class='panel'><h2>Forward streams</h2><div class='table-wrap'><table><thead><tr><th>Pool</th><th>System</th><th>Queue</th><th>Events</th><th>Cards</th><th>Settled</th><th>Profitable</th><th>3+ wins</th><th>All legs</th><th>ROI</th></tr></thead><tbody>{''.join(stream)}</tbody></table></div></div>
<div class='panel'><h2>Assembly</h2><div class='summary'>Fresh prices ≤15m · one leg/event · exact dedupe · <strong>same-book → near-price consolidation → cross-book synthetic</strong>. Consolidation retains ≥97.5% per leg, ≥98.5% of source minimum and ≥95% combined. We do not wait indefinitely for all legs at one bookmaker.</div><div class='muted'>Yankee and Heinz are independent forward experiments. Weekly promotions/relegations create a new policy version; old cohorts never change.</div></div>
<div class='panel'><h2>Current Yankee queues</h2><div class='table-wrap'><table><thead><tr><th>Pool</th><th>Pocket</th><th>Event</th><th>Pick</th><th>Source odds</th><th>Best manual</th><th>Books</th><th>Kickoff</th></tr></thead><tbody>{''.join(queue) or "<tr><td colspan='8'>Waiting for qualifying future selections.</td></tr>"}</tbody></table></div></div>
<div class='panel'><h2>Forward cards & results</h2>{cards or "<div class='muted'>No cards yet.</div>"}</div></body></html>"""


def _install_dashboard(web_module) -> None:
    app = web_module.app
    if not any(getattr(r, "path", None) == "/hit-rate-multiples" for r in app.router.routes):
        @app.get("/hit-rate-multiples", response_class=HTMLResponse)
        def hit_rate_page(): return HTMLResponse(_page(web_module))
        @app.get("/api/hit-rate-multiples/status")
        def hit_rate_status(): return scoreboard(web_module.db)
    root = next((r for r in list(app.router.routes)
                 if getattr(r, "path", None) == "/" and "GET" in (getattr(r, "methods", set()) or set())), None)
    if root is None or getattr(root.endpoint, "_hit_rate_wrapped", False): return
    original = root.endpoint; name = root.name; include = getattr(root, "include_in_schema", True)
    app.router.routes.remove(root)

    async def wrapped_root():
        response = original()
        if inspect.isawaitable(response): response = await response
        if not isinstance(response, HTMLResponse): return response
        html = response.body.decode("utf-8")
        if "href='/hit-rate-multiples'" not in html:
            link = "<a href='/hit-rate-multiples'>Hit-Rate Multiples</a>"
            marker = "<a href='/live-yankee'>Live Yankee Feed</a>"
            html = html.replace(marker, marker + link, 1) if marker in html else html.replace("<div class='nav'>", "<div class='nav'>" + link, 1)
        try:
            m = scoreboard(web_module.db)["streams"]["HR1"]["YANKEE"]
            roi = "—" if m["roi_pct"] is None else f"{float(m['roi_pct']):+.1f}%"
            panel = (f"<div class='panel'><h2>Hit-Rate Multiples Lab</h2><div class='summary'>"
                     f"HR1 Yankee: queue <strong>{m['queued']}</strong> · cards <strong>{m['cards']}</strong> · "
                     f"settled <strong>{m['settled']}</strong> · ROI <strong>{roi}</strong>. "
                     f"<a href='/hit-rate-multiples'>Open lab →</a></div></div>")
            html = html.replace("</body>", panel + "</body>", 1)
        except Exception: pass
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return HTMLResponse(html, status_code=response.status_code, headers=headers)

    wrapped_root._hit_rate_wrapped = True
    app.add_api_route("/", wrapped_root, methods=["GET"], response_class=HTMLResponse,
                      name=name, include_in_schema=include)


def _install_exports() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        import exporter
        extra = ("hit_rate_policy_versions", "hit_rate_candidates", "hit_rate_cards", "hit_rate_legs")
        exporter.EXPORT_TABLES = tuple(dict.fromkeys(tuple(exporter.EXPORT_TABLES) + extra))
        out["full"] = list(extra)
    except Exception as exc:
        out["full_error"] = f"{type(exc).__name__}: {exc}"
    try:
        import split_exporter
        extra = ("hit_rate_policy_versions", "hit_rate_candidates", "hit_rate_cards", "hit_rate_legs")
        split_exporter.WEEKLY_TABLES = tuple(dict.fromkeys(tuple(split_exporter.WEEKLY_TABLES) + extra))
        split_exporter.MANUAL_TABLES = tuple(dict.fromkeys(tuple(split_exporter.MANUAL_TABLES) + extra))
        out["weekly"] = list(extra); out["manual"] = list(extra)
    except Exception as exc:
        out["split_error"] = f"{type(exc).__name__}: {exc}"
    return out


def install() -> Dict[str, Any]:
    global _INSTALLED, _THREAD
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}
    import web as web_module
    _activate_policy(web_module.db)
    _install_dashboard(web_module)
    exports = _install_exports()
    worker = web_module.worker
    original_start = worker.start; original_stop = worker.stop

    def run_once():
        if not _LOCK.acquire(blocking=False): return
        try: run_maintenance(worker.db, worker.collector, web_module.settings)
        finally: _LOCK.release()

    def loop():
        print(f"HIT_RATE_MULTIPLES_HEARTBEAT started reaction_seconds={REACTION_SECONDS}", flush=True)
        if _STOP.wait(8): return
        while not _STOP.is_set():
            try: run_once()
            except Exception as exc:
                print(f"HIT_RATE_MULTIPLES_LOOP_ERROR {type(exc).__name__}: {sanitize_sensitive_text(exc)}", flush=True)
            _STOP.wait(REACTION_SECONDS)

    def start():
        global _THREAD
        original_start()
        if _THREAD is None or not _THREAD.is_alive():
            _STOP.clear()
            _THREAD = threading.Thread(target=loop, daemon=True, name="hit-rate-multiples")
            _THREAD.start()

    def stop():
        _STOP.set()
        original_stop()

    worker.start = start; worker.stop = stop
    worker._hit_rate_multiples_installed = True
    web_module.VERSION = "0.19.50"
    _INSTALLED = True
    return {
        "installed": True, "version": VERSION, "policy_version": POLICY_VERSION,
        "systems": list(SYSTEMS), "experiments": list(POLICY),
        "reaction_seconds": REACTION_SECONDS, "exports": exports,
        "dashboard_version": web_module.VERSION,
    }
