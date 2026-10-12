from __future__ import annotations

"""Forward-only Hit-Rate Multiples Lab.

Independent research companion to Live Yankee Feed. HR1 freezes the strict
high-hit pockets found in the 12 Oct 2026 review; HR2 is a broader challenger.
Weekly promotions/relegations must use a new POLICY_VERSION so old forward
queues/cards/results are never rewritten. No auto execution and no Telegram.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
from itertools import combinations
from math import ceil, prod
import hashlib
import inspect
import json
import threading
from typing import Any, Dict, Mapping, Optional, Sequence

from fastapi.responses import HTMLResponse

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
SYSTEM_SPECS = {
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


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if out.tzinfo is None:
            out = out.replace(tzinfo=timezone.utc)
        return out.astimezone(timezone.utc)
    except Exception:
        return None


def _same_point(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) < 1e-9
    except Exception:
        return str(a) == str(b)


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _candidate_key(policy: str, experiment: str, system_type: str, event_id: Any,
                   market: Any, selection: Any, point: Any) -> str:
    if point is None:
        point_key = ""
    else:
        try:
            point_key = f"{float(point):.8f}".rstrip("0").rstrip(".")
        except Exception:
            point_key = _norm(point)
    raw = "|".join((policy, experiment, system_type, _norm(event_id),
                    _norm(market), _norm(selection), point_key))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ensure_schema(db) -> None:
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
        selection TEXT NOT NULL, outcome_description TEXT, point REAL,
        min_odds REAL NOT NULL, source_odds REAL NOT NULL,
        band_min REAL NOT NULL, band_max REAL NOT NULL,
        commence_time TEXT NOT NULL, home_team TEXT, away_team TEXT,
        status TEXT NOT NULL DEFAULT 'QUEUED', consumed_card_id INTEGER, expired_at TEXT
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS hit_rate_quotes (
        id {id_col}, event_id TEXT NOT NULL, captured_at TEXT NOT NULL,
        bookmaker_key TEXT NOT NULL, bookmaker_title TEXT,
        market_key TEXT NOT NULL, selection TEXT NOT NULL,
        outcome_description TEXT, point REAL, price REAL NOT NULL
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
        selection TEXT NOT NULL, outcome_description TEXT, point REAL,
        entry_odds REAL NOT NULL, bookmaker_key TEXT NOT NULL, bookmaker_title TEXT,
        quote_captured_at TEXT NOT NULL, quote_age_minutes REAL,
        min_odds REAL, band_min REAL, band_max REAL, commence_time TEXT,
        home_team TEXT, away_team TEXT, result TEXT, settled_at TEXT,
        UNIQUE(card_id,leg_order)
    )""")
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_hit_rate_queue ON hit_rate_candidates(policy_version,experiment_key,system_type,status,first_seen_at)",
        "CREATE INDEX IF NOT EXISTS idx_hit_rate_quote ON hit_rate_quotes(event_id,market_key,selection,captured_at)",
        "CREATE INDEX IF NOT EXISTS idx_hit_rate_cards ON hit_rate_cards(policy_version,experiment_key,system_type,created_at)",
    ):
        try:
            db.execute(sql)
        except Exception:
            pass


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
        # A normal redeploy must not reset the clean forward start.
        db.execute("UPDATE hit_rate_policy_versions SET is_active=CASE WHEN policy_version=? THEN 1 ELSE 0 END", (POLICY_VERSION,))
    return dict(db.fetchone("SELECT * FROM hit_rate_policy_versions WHERE policy_version=?", (POLICY_VERSION,)) or {})


def _actionable(value: Any, now: datetime) -> bool:
    kickoff = _parse_iso(value)
    return bool(kickoff and now < kickoff <= now + timedelta(hours=FORMATION_HORIZON_HOURS))


def _qualifications(kind: str, row: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    price = float(row.get("source_odds") or 0)
    market = str(row.get("market_key") or "").lower()
    sport = str(row.get("sport_key") or "").lower()
    out: Dict[str, Dict[str, Any]] = {}
    if kind == "FOOTBALL":
        if market == "btts" and 1.80 <= price < 2.00:
            meta = {"lane": "FOOTBALL_BTTS_1_80_1_99", "band_min": 1.80, "band_max": 2.00}
            out["HR1"] = dict(meta); out["HR2"] = dict(meta)
        if market == "totals":
            if 1.80 <= price < 2.00:
                out["HR1"] = {"lane": "FOOTBALL_TOTALS_1_80_1_99", "band_min": 1.80, "band_max": 2.00}
            if 1.80 <= price < 3.00:
                out["HR2"] = {"lane": "FOOTBALL_TOTALS_1_80_2_99", "band_min": 1.80, "band_max": 3.00}
        if market == "h2h" and 2.50 <= price < 3.00:
            out["HR2"] = {"lane": "FOOTBALL_H2H_2_50_2_99", "band_min": 2.50, "band_max": 3.00}
        return out
    if kind == "TENNIS":
        if 1.80 <= price < 2.00:
            out["HR1"] = {"lane": "TENNIS_H2H_1_80_1_99", "band_min": 1.80, "band_max": 2.00}
        if 1.80 <= price < 3.00:
            out["HR2"] = {"lane": "TENNIS_H2H_1_80_2_99", "band_min": 1.80, "band_max": 3.00}
        return out
    if kind == "MULTISPORT":
        meta = None
        if sport == "basketball_euroleague" and 1.50 <= price < 3.00:
            meta = {"lane": "EUROLEAGUE_H2H_1_50_2_99", "band_min": 1.50, "band_max": 3.00}
        elif "nfl" in sport and 1.50 <= price < 3.00:
            meta = {"lane": "NFL_H2H_1_50_2_99", "band_min": 1.50, "band_max": 3.00}
        elif sport == "baseball_mlb" and 1.50 <= price < 2.00:
            meta = {"lane": "MLB_H2H_1_50_1_99", "band_min": 1.50, "band_max": 2.00}
        if meta:
            out["HR1"] = dict(meta); out["HR2"] = dict(meta)
    return out


def _source_rows(db, now: datetime) -> list[dict[str, Any]]:
    """Seed any still-open future pick at activation; only post-activation fresh
    manual quotes can form a card, so this does not introduce hindsight."""
    specs = (
        ("FOOTBALL", "execution_shadow_bets", """SELECT b.id AS source_id,b.created_at AS source_created_at,b.event_id,
            e.sport_key,e.league,e.commence_time,e.home_team,e.away_team,
            b.market_key,b.selection,b.outcome_description,b.point,b.min_odds,b.offered_odds AS source_odds
            FROM execution_shadow_bets b JOIN events e ON e.event_id=b.event_id
            WHERE b.status='OPEN' AND e.status='UPCOMING' ORDER BY b.created_at,b.id"""),
        ("TENNIS", "tennis_execution_bets", """SELECT b.id AS source_id,b.created_at AS source_created_at,b.event_id,
            e.sport_key,e.tournament_title AS league,e.commence_time,e.player_one AS home_team,e.player_two AS away_team,
            'h2h' AS market_key,b.selection,NULL AS outcome_description,NULL AS point,b.min_odds,b.offered_odds AS source_odds
            FROM tennis_execution_bets b JOIN tennis_events e ON e.event_id=b.event_id
            WHERE b.status='OPEN' AND e.status='UPCOMING' ORDER BY b.created_at,b.id"""),
        ("MULTISPORT", "multisport_execution_bets", """SELECT b.id AS source_id,b.created_at AS source_created_at,b.event_id,
            e.sport_key,e.league_title AS league,e.commence_time,e.home_team,e.away_team,
            'h2h' AS market_key,b.selection,NULL AS outcome_description,NULL AS point,b.min_odds,b.offered_odds AS source_odds
            FROM multisport_execution_bets b JOIN multisport_events e ON e.event_id=b.event_id
            WHERE b.status='OPEN' AND e.status='UPCOMING' ORDER BY b.created_at,b.id"""),
    )
    out = []
    for kind, table, sql in specs:
        try:
            rows = db.fetchall(sql)
        except Exception:
            rows = []
        for raw in rows:
            row = dict(raw)
            if not _actionable(row.get("commence_time"), now):
                continue
            if float(row.get("source_odds") or 0) <= 1 or float(row.get("min_odds") or 0) <= 1:
                continue
            for experiment, meta in _qualifications(kind, row).items():
                item = dict(row)
                item.update(experiment_key=experiment, source_lane=meta["lane"],
                            source_table=table, band_min=float(meta["band_min"]),
                            band_max=float(meta["band_max"]))
                out.append(item)
    return sorted(out, key=lambda r: (str(r.get("source_created_at") or ""),
                                      str(r.get("experiment_key") or ""),
                                      str(r.get("source_lane") or ""), int(r.get("source_id") or 0)))


def _ingest(db, now: datetime) -> Dict[str, int]:
    seen = inserted = 0
    for row in _source_rows(db, now):
        for system_type in SYSTEM_SPECS:
            seen += 1
            key = _candidate_key(POLICY_VERSION, row["experiment_key"], system_type,
                                 row.get("event_id"), row.get("market_key"),
                                 row.get("selection"), row.get("point"))
            try:
                inserted += int(db.execute("""INSERT INTO hit_rate_candidates(
                    candidate_key,policy_version,experiment_key,system_type,first_seen_at,source_created_at,
                    source_lane,source_table,source_id,event_id,sport_key,league,market_key,selection,
                    outcome_description,point,min_odds,source_odds,band_min,band_max,commence_time,
                    home_team,away_team,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(candidate_key) DO NOTHING""", (
                    key, POLICY_VERSION, row["experiment_key"], system_type, utc_now_iso(),
                    str(row.get("source_created_at") or utc_now_iso()), row["source_lane"], row["source_table"],
                    int(row.get("source_id") or 0), str(row.get("event_id") or ""), str(row.get("sport_key") or ""),
                    str(row.get("league") or ""), str(row.get("market_key") or "h2h"), str(row.get("selection") or ""),
                    str(row.get("outcome_description") or ""), row.get("point"), float(row.get("min_odds") or 0),
                    float(row.get("source_odds") or 0), float(row["band_min"]), float(row["band_max"]),
                    str(row.get("commence_time") or ""), str(row.get("home_team") or ""),
                    str(row.get("away_team") or ""), "QUEUED",
                )) > 0)
            except Exception:
                pass
    return {"source_stream_rows": seen, "inserted": inserted}


def _expire(db, now: datetime) -> int:
    total = 0
    for row in db.fetchall("SELECT id,commence_time FROM hit_rate_candidates WHERE status='QUEUED'"):
        kickoff = _parse_iso(row.get("commence_time"))
        if kickoff is None or kickoff <= now:
            total += db.execute("UPDATE hit_rate_candidates SET status='EXPIRED',expired_at=? WHERE id=? AND status='QUEUED'",
                                (utc_now_iso(), int(row["id"])))
    return total


def _credits_today(db, now: datetime) -> int:
    row = db.fetchone("""SELECT COALESCE(SUM(actual_cost),0) AS n FROM collector_runs
        WHERE run_type='HIT_RATE_MULTIPLES_QUOTES' AND started_at>=?""",
        (now.date().isoformat() + "T00:00:00+00:00",)) or {}
    return int(row.get("n") or 0)


def _refresh_targets(db, now: datetime) -> list[dict[str, Any]]:
    queued = [dict(x) for x in db.fetchall("SELECT * FROM hit_rate_candidates WHERE status='QUEUED' ORDER BY first_seen_at,id")]
    stream_events: Dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in queued:
        if _actionable(row.get("commence_time"), now):
            stream_events[(row["policy_version"], row["experiment_key"], row["system_type"])].add(str(row["event_id"]))
    # Start warming prices with two selections; don't wait until a full Yankee exists.
    if not any(len(v) >= 2 for v in stream_events.values()):
        return []
    by_event: Dict[str, Dict[str, Any]] = {}
    for row in queued:
        if not _actionable(row.get("commence_time"), now):
            continue
        eid = str(row.get("event_id") or "")
        item = by_event.setdefault(eid, {"event_id": eid, "sport_key": str(row.get("sport_key") or ""),
            "commence_time": str(row.get("commence_time") or ""), "first_seen_at": str(row.get("first_seen_at") or ""), "markets": set()})
        item["markets"].add(str(row.get("market_key") or "h2h"))
    due = []
    for item in by_event.values():
        last = db.fetchone("SELECT MAX(started_at) AS at FROM collector_runs WHERE run_type='HIT_RATE_MULTIPLES_QUOTES' AND event_id=? AND ok=1",
                           (item["event_id"],)) or {}
        last_dt = _parse_iso(last.get("at"))
        if last_dt and (now - last_dt).total_seconds() / 60.0 < QUOTE_REFRESH_MINUTES:
            continue
        item["markets"] = tuple(sorted(item["markets"])); due.append(item)
    return sorted(due, key=lambda x: (x["first_seen_at"], x["commence_time"], x["event_id"]))


def _store_quotes(db, payload: Any, event_id: str, captured_at: str, markets: Sequence[str]) -> int:
    events = [payload] if isinstance(payload, Mapping) else [x for x in payload if isinstance(x, Mapping)] if isinstance(payload, list) else []
    wanted = set(markets); rows = []
    for event in events:
        pid = str(event.get("id") or event_id)
        if pid != event_id:
            continue
        for book in event.get("bookmakers") or []:
            bkey = str(book.get("key") or "")
            if bkey not in DEFAULT_PLACEABLE_BOOKS:
                continue
            btitle = str(book.get("title") or bkey)
            for market in book.get("markets") or []:
                mkey = str(market.get("key") or "")
                if mkey not in wanted:
                    continue
                stamp = str(market.get("last_update") or book.get("last_update") or captured_at)
                for outcome in market.get("outcomes") or []:
                    try:
                        price = float(outcome.get("price"))
                    except Exception:
                        continue
                    selection = str(outcome.get("name") or "")
                    if price <= 1 or not selection:
                        continue
                    rows.append((pid, stamp, bkey, btitle, mkey, selection,
                                 str(outcome.get("description") or ""), outcome.get("point"), price))
    if rows:
        db.executemany("""INSERT INTO hit_rate_quotes(event_id,captured_at,bookmaker_key,bookmaker_title,
            market_key,selection,outcome_description,point,price) VALUES(?,?,?,?,?,?,?,?,?)""", rows)
    return len(rows)


def _refresh(db, collector, settings, now: datetime) -> Dict[str, Any]:
    books = tuple(getattr(settings, "manual_systems_placeable_bookmaker_keys", DEFAULT_PLACEABLE_BOOKS) or DEFAULT_PLACEABLE_BOOKS)
    books = tuple(x for x in books if x in DEFAULT_PLACEABLE_BOOKS) or DEFAULT_PLACEABLE_BOOKS
    spent = _credits_today(db, now)
    if spent >= DAILY_REFRESH_CREDIT_BUDGET:
        return {"polled": 0, "inserted": 0, "reason": "daily_budget", "spent": spent}
    targets = _refresh_targets(db, now); polled = inserted = 0
    for target in targets[:MAX_REFRESH_EVENTS_PER_CYCLE]:
        markets = tuple(target.get("markets") or ())
        if not markets:
            continue
        estimated = max(1, ceil(len(books) / 10)) * max(1, len(markets))
        if spent + estimated > DAILY_REFRESH_CREDIT_BUDGET:
            break
        decision = collector.quota.decide(estimated)
        if not decision.allowed:
            return {"polled": polled, "inserted": inserted, "reason": decision.reason, "spent": spent}
        actual = 0
        try:
            result = collector.api.event_odds(target["sport_key"], target["event_id"], collector.region,
                                              markets, bookmaker_keys=books)
            actual = provider_actual_cost(result.last_cost, estimated)
            collector.quota.update(remaining=result.remaining, used=result.used, last_cost=result.last_cost)
            added = _store_quotes(db, result.data, target["event_id"], utc_now_iso(), markets)
            db.record_collector_run("HIT_RATE_MULTIPLES_QUOTES", True, event_id=target["event_id"],
                sport_key=target["sport_key"], requested_markets=",".join(markets),
                estimated_cost=estimated, actual_cost=actual, detail=f"books={','.join(books)}; quote_rows={added}")
            polled += 1; inserted += added; spent += actual
        except Exception as exc:
            db.record_collector_run("HIT_RATE_MULTIPLES_QUOTES", False, event_id=target["event_id"],
                sport_key=target["sport_key"], requested_markets=",".join(markets),
                estimated_cost=estimated, actual_cost=actual, detail=f"error={sanitize_sensitive_text(exc)}")
    return {"polled": polled, "inserted": inserted, "reason": "ok", "spent": spent}


def _latest_quotes(db, candidate: Mapping[str, Any], now: datetime, allow_min_miss: bool = False) -> Dict[str, Dict[str, Any]]:
    rows = db.fetchall("""SELECT * FROM hit_rate_quotes WHERE event_id=? AND market_key=? AND selection=?
        ORDER BY captured_at DESC,id DESC LIMIT 500""",
        (candidate["event_id"], candidate["market_key"], candidate["selection"]))
    cutoff = now - timedelta(minutes=QUOTE_FRESHNESS_MINUTES)
    out: Dict[str, Dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw); book = str(row.get("bookmaker_key") or "")
        if book in out or book not in DEFAULT_PLACEABLE_BOOKS:
            continue
        if not _same_point(row.get("point"), candidate.get("point")):
            continue
        want_desc = str(candidate.get("outcome_description") or "")
        got_desc = str(row.get("outcome_description") or "")
        if want_desc and got_desc and want_desc != got_desc:
            continue
        ts = _parse_iso(row.get("captured_at"))
        if ts is None or ts < cutoff or ts > now:
            continue
        price = float(row.get("price") or 0)
        if not (float(candidate["band_min"]) <= price < float(candidate["band_max"])):
            continue
        min_odds = float(candidate.get("min_odds") or 0)
        if not allow_min_miss and price + 1e-12 < min_odds:
            continue
        row["quote_age_minutes"] = max(0.0, (now - ts).total_seconds() / 60.0)
        out[book] = row
    return out


def _choose_quotes(db, candidates: Sequence[Mapping[str, Any]], now: datetime):
    qualified = [_latest_quotes(db, c, now, False) for c in candidates]
    if any(not q for q in qualified):
        return None
    common = set(qualified[0])
    for q in qualified[1:]:
        common.intersection_update(q)
    if common:
        book = max(common, key=lambda b: (prod(float(q[b]["price"]) for q in qualified), b))
        return [dict(q[book]) for q in qualified], "SAME_BOOK_VERIFIED"
    synthetic = [dict(max(q.values(), key=lambda x: (float(x["price"]), str(x.get("bookmaker_key") or "")))) for q in qualified]
    synthetic_product = prod(float(q["price"]) for q in synthetic)
    anchors: Dict[str, int] = defaultdict(int)
    for q in synthetic:
        anchors[str(q.get("bookmaker_key") or "")] += 1
    min_anchor = 2 if len(candidates) == 4 else 3
    options = []
    for book, anchor_count in anchors.items():
        if anchor_count < min_anchor:
            continue
        proposed = []; retentions = []; valid = True
        for candidate, best in zip(candidates, synthetic):
            alt = _latest_quotes(db, candidate, now, True).get(book)
            if alt is None:
                valid = False; break
            alt_price = float(alt.get("price") or 0); best_price = float(best.get("price") or 0)
            if alt_price / best_price + 1e-12 < MIN_LEG_PRICE_RETENTION:
                valid = False; break
            min_odds = float(candidate.get("min_odds") or 0)
            if min_odds > 1 and alt_price / min_odds + 1e-12 < MIN_REQUIRED_ODDS_RETENTION:
                valid = False; break
            proposed.append(dict(alt)); retentions.append(alt_price / best_price)
        if not valid or len(proposed) != len(candidates):
            continue
        combined = prod(float(q["price"]) for q in proposed) / synthetic_product
        if combined + 1e-12 >= MIN_CARD_PRICE_RETENTION:
            options.append((anchor_count, combined, min(retentions), book, proposed))
    if options:
        options.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
        return options[0][4], "SAME_BOOK_CONSOLIDATED"
    return synthetic, "CROSS_BOOK_SYNTHETIC"


def _ready(db, policy: str, experiment: str, system_type: str, now: datetime):
    rows = db.fetchall("""SELECT * FROM hit_rate_candidates WHERE policy_version=? AND experiment_key=?
        AND system_type=? AND status='QUEUED' ORDER BY first_seen_at,id""", (policy, experiment, system_type))
    out = []; seen_events = set()
    for raw in rows:
        c = dict(raw); eid = str(c.get("event_id") or "")
        if not eid or eid in seen_events or not _actionable(c.get("commence_time"), now):
            continue
        if not _latest_quotes(db, c, now, False):
            continue
        out.append(c); seen_events.add(eid)
    return out


def _create_card(db, policy: str, experiment: str, system_type: str,
                 candidates: Sequence[Mapping[str, Any]], quotes: Sequence[Mapping[str, Any]], mode: str) -> Optional[int]:
    ids = [int(x["id"]) for x in candidates]
    card_key = hashlib.sha256("|".join([VERSION, policy, experiment, system_type] + [str(x) for x in ids]).encode()).hexdigest()
    db.execute("""INSERT INTO hit_rate_cards(card_key,policy_version,experiment_key,system_type,created_at,status,
        assembly_mode,source_mix,bookmaker_mix,oldest_quote_age_minutes,reference_stake_gbp)
        VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(card_key) DO NOTHING""", (
        card_key, policy, experiment, system_type, utc_now_iso(), "OPEN", mode,
        ",".join(sorted({str(x.get("source_lane") or "") for x in candidates})),
        ",".join(sorted({str(x.get("bookmaker_key") or "") for x in quotes})),
        max(float(x.get("quote_age_minutes") or 0) for x in quotes), REFERENCE_STAKE_GBP,
    ))
    card = db.fetchone("SELECT * FROM hit_rate_cards WHERE card_key=?", (card_key,))
    if not card:
        return None
    cid = int(card["id"])
    if int((db.fetchone("SELECT COUNT(*) AS n FROM hit_rate_legs WHERE card_id=?", (cid,)) or {}).get("n") or 0):
        return cid
    for order, (c, q) in enumerate(zip(candidates, quotes), 1):
        db.execute("""INSERT INTO hit_rate_legs(card_id,leg_order,candidate_id,source_lane,source_table,source_id,
            event_id,sport_key,league,market_key,selection,outcome_description,point,entry_odds,bookmaker_key,
            bookmaker_title,quote_captured_at,quote_age_minutes,min_odds,band_min,band_max,commence_time,home_team,away_team)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            cid, order, int(c["id"]), c["source_lane"], c["source_table"], int(c["source_id"]), c["event_id"],
            c.get("sport_key"), c.get("league"), c["market_key"], c["selection"], c.get("outcome_description"), c.get("point"),
            float(q["price"]), q["bookmaker_key"], q.get("bookmaker_title"), q["captured_at"],
            float(q.get("quote_age_minutes") or 0), float(c.get("min_odds") or 0), float(c["band_min"]),
            float(c["band_max"]), c.get("commence_time"), c.get("home_team"), c.get("away_team"),
        ))
        db.execute("UPDATE hit_rate_candidates SET status='CONSUMED',consumed_card_id=? WHERE id=? AND status='QUEUED'",
                   (cid, int(c["id"])))
    return cid


def _form(db, now: datetime) -> Dict[str, int]:
    formed = verified = consolidated = cross = 0
    streams = db.fetchall("""SELECT DISTINCT policy_version,experiment_key,system_type FROM hit_rate_candidates
        WHERE status='QUEUED' ORDER BY policy_version,experiment_key,system_type""")
    for stream in streams:
        policy = str(stream["policy_version"]); exp = str(stream["experiment_key"]); system = str(stream["system_type"])
        need = int(SYSTEM_SPECS.get(system, {}).get("legs") or 0)
        for _ in range(3):
            ready = _ready(db, policy, exp, system, now)
            if len(ready) < need:
                break
            candidates = ready[:need]; picked = _choose_quotes(db, candidates, now)
            if not picked:
                break
            quotes, mode = picked
            if not _create_card(db, policy, exp, system, candidates, quotes, mode):
                break
            formed += 1; verified += int(mode == "SAME_BOOK_VERIFIED")
            consolidated += int(mode == "SAME_BOOK_CONSOLIDATED"); cross += int(mode == "CROSS_BOOK_SYNTHETIC")
    return {"formed": formed, "same_book_verified": verified, "same_book_consolidated": consolidated, "cross_book": cross}


def _normalize_result(value: Any) -> Optional[str]:
    text = str(value or "").strip().upper()
    if text in {"WIN", "WON", "WINNER"}: return "WIN"
    if text in {"LOSS", "LOSE", "LOST", "LOSER"}: return "LOSS"
    if text in {"PUSH", "VOID", "REFUND", "CANCELLED", "CANCELED"}: return "PUSH" if text == "PUSH" else "VOID"
    return None


def _source_result(db, leg: Mapping[str, Any]) -> Optional[str]:
    table = str(leg.get("source_table") or "")
    if table not in {"execution_shadow_bets", "tennis_execution_bets", "multisport_execution_bets"}:
        return None
    try:
        row = db.fetchone(f"SELECT result,status,pnl_units FROM {table} WHERE id=?", (int(leg["source_id"]),)) or {}
    except Exception:
        return None
    result = _normalize_result(row.get("result"))
    if result: return result
    if str(row.get("status") or "").upper() in {"VOID", "CANCELLED", "CANCELED"}: return "VOID"
    if str(row.get("status") or "").upper() == "SETTLED" and row.get("pnl_units") is not None:
        pnl = float(row.get("pnl_units") or 0)
        return "WIN" if pnl > 1e-12 else "LOSS" if pnl < -1e-12 else None
    return None


def _line_combos(system: str, orders: Sequence[int]):
    spec = SYSTEM_SPECS.get(system)
    if not spec or len(orders) != int(spec["legs"]): return []
    out = []
    for size in spec["sizes"]: out.extend(combinations(tuple(orders), size))
    return out


def settle_cards(db) -> Dict[str, int]:
    touched = settled = 0
    for card in db.fetchall("SELECT * FROM hit_rate_cards WHERE status!='SETTLED' ORDER BY id"):
        cid = int(card["id"]); legs = [dict(x) for x in db.fetchall("SELECT * FROM hit_rate_legs WHERE card_id=? ORDER BY leg_order", (cid,))]
        changed = False
        for leg in legs:
            result = _normalize_result(leg.get("result")) or _source_result(db, leg)
            if result and result != _normalize_result(leg.get("result")):
                db.execute("UPDATE hit_rate_legs SET result=?,settled_at=? WHERE id=?", (result, utc_now_iso(), int(leg["id"])))
                leg["result"] = result; changed = True
        touched += int(changed)
        if not legs or any(_normalize_result(x.get("result")) is None for x in legs):
            continue
        combos = _line_combos(str(card["system_type"]), [int(x["leg_order"]) for x in legs])
        if not combos: continue
        by_order = {int(x["leg_order"]): x for x in legs}; line_stake = 1.0 / len(combos); total_return = 0.0
        for combo in combos:
            odds = 1.0; lost = False
            for order in combo:
                leg = by_order[order]; result = _normalize_result(leg.get("result"))
                if result == "LOSS": lost = True; break
                if result == "WIN": odds *= float(leg["entry_odds"])
            if not lost: total_return += line_stake * odds
        pnl = total_return - 1.0; wins = sum(_normalize_result(x.get("result")) == "WIN" for x in legs)
        result = "PROFIT" if pnl > 1e-12 else "LOSS" if pnl < -1e-12 else "BREAKEVEN"
        db.execute("""UPDATE hit_rate_cards SET status='SETTLED',result=?,winning_legs=?,return_units=?,pnl_units=?,roi_pct=?,
            reference_return_gbp=?,reference_pnl_gbp=?,settled_at=? WHERE id=?""", (
            result, wins, total_return, pnl, pnl * 100.0, total_return * REFERENCE_STAKE_GBP,
            pnl * REFERENCE_STAKE_GBP, utc_now_iso(), cid,
        )); settled += 1
    return {"touched": touched, "settled": settled}


def _stream_metrics(db, policy: str, exp: str, system: str) -> Dict[str, Any]:
    c = db.fetchone("""SELECT COUNT(*) AS total,SUM(CASE WHEN status='QUEUED' THEN 1 ELSE 0 END) AS queued,
        SUM(CASE WHEN status='CONSUMED' THEN 1 ELSE 0 END) AS consumed,SUM(CASE WHEN status='EXPIRED' THEN 1 ELSE 0 END) AS expired,
        COUNT(DISTINCT CASE WHEN status='QUEUED' THEN event_id END) AS queued_events FROM hit_rate_candidates
        WHERE policy_version=? AND experiment_key=? AND system_type=?""", (policy, exp, system)) or {}
    cards = [dict(x) for x in db.fetchall("SELECT * FROM hit_rate_cards WHERE policy_version=? AND experiment_key=? AND system_type=? ORDER BY id",
                                          (policy, exp, system))]
    settled = [x for x in cards if x.get("pnl_units") is not None]; pnl = sum(float(x.get("pnl_units") or 0) for x in settled)
    return {
        "candidates": int(c.get("total") or 0), "queued": int(c.get("queued") or 0), "queued_events": int(c.get("queued_events") or 0),
        "consumed": int(c.get("consumed") or 0), "expired": int(c.get("expired") or 0), "cards": len(cards), "settled": len(settled),
        "profitable": sum(float(x.get("pnl_units") or 0) > 1e-12 for x in settled), "pnl_units": pnl,
        "roi_pct": (pnl / len(settled) * 100.0) if settled else None,
        "at_least_2": sum(int(x.get("winning_legs") or 0) >= 2 for x in settled),
        "at_least_3": sum(int(x.get("winning_legs") or 0) >= 3 for x in settled),
        "full_house": sum(int(x.get("winning_legs") or 0) == int(SYSTEM_SPECS[system]["legs"]) for x in settled),
        "same_book_verified": sum(x.get("assembly_mode") == "SAME_BOOK_VERIFIED" for x in cards),
        "same_book_consolidated": sum(x.get("assembly_mode") == "SAME_BOOK_CONSOLIDATED" for x in cards),
        "cross_book": sum(x.get("assembly_mode") == "CROSS_BOOK_SYNTHETIC" for x in cards),
    }


def scoreboard(db) -> Dict[str, Any]:
    _ensure_schema(db); active = db.fetchone("SELECT * FROM hit_rate_policy_versions WHERE is_active=1 ORDER BY activated_at DESC LIMIT 1") or {}
    policy = str(active.get("policy_version") or POLICY_VERSION)
    return {
        "version": VERSION, "active_policy_version": policy, "activated_at": active.get("activated_at"),
        "policy": POLICY, "design_backtest": DESIGN_BACKTEST,
        "streams": {exp: {system: _stream_metrics(db, policy, exp, system) for system in SYSTEM_SPECS} for exp in POLICY},
        "quote_refresh_minutes": QUOTE_REFRESH_MINUTES, "quote_freshness_minutes": QUOTE_FRESHNESS_MINUTES,
    }


def run_maintenance(db, collector, settings, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc); now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    _activate_policy(db)
    try:
        payload = {"ok": True, "version": VERSION, "policy_version": POLICY_VERSION,
                   "expired": _expire(db, now), "ingested": _ingest(db, now)}
        payload["quotes"] = _refresh(db, collector, settings, now); payload["formed"] = _form(db, now)
        payload["settlement"] = settle_cards(db); payload["scoreboard"] = scoreboard(db)
        db.record_collector_run("HIT_RATE_MULTIPLES_MAINT", True, detail=json.dumps(payload, sort_keys=True, default=str))
        print("HIT_RATE_MULTIPLES_MAINT " + json.dumps(payload, sort_keys=True, default=str), flush=True)
        return payload
    except Exception as exc:
        err = sanitize_sensitive_text(exc); db.record_collector_run("HIT_RATE_MULTIPLES_MAINT", False, detail=err)
        print(f"HIT_RATE_MULTIPLES_MAINT_ERROR {type(exc).__name__}: {err}", flush=True); return {"ok": False, "error": err}


def _fmt_time(value: Any) -> str:
    dt = _parse_iso(value); return dt.strftime("%d %b %H:%M UTC") if dt else "—"


def _market_label(row: Mapping[str, Any]) -> str:
    market = str(row.get("market_key") or "").lower(); selection = str(row.get("selection") or "—"); point = row.get("point")
    if market == "h2h": return selection
    if market == "btts": return f"BTTS {selection}"
    if market == "totals": return f"{selection} {point}" if point is not None and str(point) not in selection else selection
    return f"{market.replace('_',' ').title()} {selection}".strip()


def _tone(value: Any) -> str:
    return "ok" if str(value or "").upper() in {"WIN", "PROFIT"} else "bad" if str(value or "").upper() == "LOSS" else "warn" if str(value or "").upper() in {"VOID", "PUSH", "BREAKEVEN"} else ""


def _money(value: Any) -> str:
    if value is None: return "—"
    return f"£{float(value):+.2f}" if abs(float(value)) > 1e-12 else "£0.00"


def _card_html(db, card: Mapping[str, Any]) -> str:
    legs = [dict(x) for x in db.fetchall("SELECT * FROM hit_rate_legs WHERE card_id=? ORDER BY leg_order", (int(card["id"]),))]
    rows = []
    for leg in legs:
        fixture = f"{leg.get('home_team') or ''} v {leg.get('away_team') or ''}".strip(); result = str(leg.get("result") or "PENDING").upper()
        rows.append(f"<tr><td>{int(leg.get('leg_order') or 0)}</td><td>{escape(str(leg.get('source_lane') or ''))}</td><td>{escape(fixture)}</td><td><strong>{escape(_market_label(leg))}</strong></td><td>{float(leg.get('entry_odds') or 0):.2f}</td><td>{escape(str(leg.get('bookmaker_title') or leg.get('bookmaker_key') or ''))}</td><td>{_fmt_time(leg.get('commence_time'))}</td><td class='{_tone(result)}'><strong>{escape(result)}</strong></td></tr>")
    pnl = card.get("reference_pnl_gbp"); result = str(card.get("result") or "PENDING")
    summary = (f"Winning legs <strong>{int(card.get('winning_legs') or 0)}/{len(legs)}</strong> · Return <strong>{_money(card.get('reference_return_gbp'))}</strong> · P/L <strong class='{_tone(result)}'>{_money(pnl)}</strong> · ROI <strong class='{_tone(result)}'>{float(card.get('roi_pct') or 0):+.1f}%</strong>" if pnl is not None else "Awaiting all constituent selections to settle.")
    note = "<div class='muted'>Cross-book synthetic: research P/L from recorded prices; not one verified one-book system.</div>" if card.get("assembly_mode") == "CROSS_BOOK_SYNTHETIC" else ""
    return f"<div class='card' style='margin:12px 0'><div><strong>{escape(str(card.get('experiment_key')))} {escape(str(card.get('system_type')).title())} #{int(card['id'])}</strong> · {escape(str(card.get('assembly_mode') or ''))} · <span class='{_tone(result)}'>{escape(result)}</span></div><div class='muted'>Policy {escape(str(card.get('policy_version')))} · {_fmt_time(card.get('created_at'))}</div><div class='table-wrap' style='margin-top:8px'><table><thead><tr><th>#</th><th>Pocket</th><th>Event</th><th>Pick</th><th>Odds</th><th>Book</th><th>Kickoff</th><th>Result</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div><div class='summary' style='margin-top:8px'>{summary}</div>{note}</div>"


def _page_html(web_module) -> str:
    db = web_module.db
    try: settle_cards(db)
    except Exception: pass
    s = scoreboard(db); active = str(s["active_policy_version"]); stream_rows = []
    for exp in ("HR1", "HR2"):
        for system in ("YANKEE", "HEINZ"):
            m = s["streams"][exp][system]; roi = "—" if m["roi_pct"] is None else f"{float(m['roi_pct']):+.1f}%"
            stream_rows.append(f"<tr><td>{exp}</td><td>{system.title()}</td><td>{m['queued']}</td><td>{m['queued_events']}</td><td>{m['cards']}</td><td>{m['settled']}</td><td>{m['profitable']}</td><td>{m['at_least_3']}</td><td>{m['full_house']}</td><td>{roi}</td></tr>")
    qrows = []
    now = datetime.now(timezone.utc)
    for c in db.fetchall("SELECT * FROM hit_rate_candidates WHERE policy_version=? AND system_type='YANKEE' AND status='QUEUED' ORDER BY first_seen_at,id LIMIT 80", (active,)):
        q = _latest_quotes(db, c, now, False); best = max([float(x["price"]) for x in q.values()], default=None)
        qrows.append(f"<tr><td>{escape(str(c.get('experiment_key') or ''))}</td><td>{escape(str(c.get('source_lane') or ''))}</td><td>{escape(str(c.get('home_team') or ''))} v {escape(str(c.get('away_team') or ''))}</td><td>{escape(_market_label(c))}</td><td>{float(c.get('source_odds') or 0):.2f}</td><td class='{'ok' if best else ''}'>{f'{best:.2f}' if best else 'WAIT'}</td><td>{len(q)}</td><td>{_fmt_time(c.get('commence_time'))}</td></tr>")
    details = "".join(_card_html(db, c) for c in db.fetchall("SELECT * FROM hit_rate_cards ORDER BY id DESC LIMIT 30")) or "<div class='muted'>No forward cards formed yet.</div>"
    hr1 = "".join(f"<li>{escape(x)}</li>" for x in POLICY["HR1"]["pockets"]); hr2 = "".join(f"<li>{escape(x)}</li>" for x in POLICY["HR2"]["pockets"]); b = DESIGN_BACKTEST
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Hit-Rate Multiples Lab</title><style>{web_module.BASE_STYLE}</style></head><body><a href='/'>← Betting Lab</a><h1>Hit-Rate Multiples Lab <span class='pill'>SHADOW · FORWARD ONLY · NO EXECUTION</span></h1><div class='sub'>Tests whether higher strike-rate/value pockets produce more robust Yankees. Heinz runs independently in parallel as a benchmark.</div><div class='grid'><div class='card'><div class='label'>POLICY</div><div class='value' style='font-size:18px'>{escape(active)}</div></div><div class='card'><div class='label'>STARTED</div><div class='value' style='font-size:18px'>{_fmt_time(s.get('activated_at'))}</div></div><div class='card'><div class='label'>QUOTE REFRESH</div><div class='value'>{QUOTE_REFRESH_MINUTES:.0f}m</div></div><div class='card'><div class='label'>STAKE CONTROL</div><div class='value'>£{REFERENCE_STAKE_GBP:.0f}</div></div></div><div class='section-grid'><div class='panel'><h2>HR1 — strict high-hit pool</h2><ul>{hr1}</ul><div class='muted'>Primary forward hypothesis.</div></div><div class='panel'><h2>HR2 — expanded challenger</h2><ul>{hr2}</ul><div class='muted'>Broader coverage; challenger only.</div></div></div><div class='panel'><h2>Frozen design backtest</h2><div class='summary'>12 Sep–12 Oct design sample: HR1 <strong>{b['hr1_pool']['selections']} selections · {b['hr1_pool']['hit_rate_pct']:.1f}% hit · {b['hr1_pool']['singles_roi_pct']:+.1f}% singles ROI</strong>. Yankees <strong>{b['hr1_yankee']['cards']} cards · {b['hr1_yankee']['roi_pct']:+.1f}% ROI</strong>, no 4/4. Heinz <strong>{b['hr1_heinz']['cards']} cards · {b['hr1_heinz']['roi_pct']:+.1f}% ROI</strong>. Design evidence only; below is clean forward.</div></div><div class='panel'><h2>Forward streams</h2><div class='table-wrap'><table><thead><tr><th>Pool</th><th>System</th><th>Queue</th><th>Events</th><th>Cards</th><th>Settled</th><th>Profitable</th><th>3+ wins</th><th>All legs</th><th>ROI</th></tr></thead><tbody>{''.join(stream_rows)}</tbody></table></div></div><div class='panel'><h2>Assembly mechanics</h2><div class='summary'>Chronological queue · one leg per event · exact dedupe · fresh supported-book prices ≤{QUOTE_FRESHNESS_MINUTES:.0f}m. <strong>Same-book verified → near-price same-book consolidation → cross-book synthetic.</strong> Consolidation retains ≥97.5% of each best price, ≥98.5% of source minimum and ≥95% of combined price. A cross-book card forms rather than waiting indefinitely for one bookmaker.</div><div class='muted'>Yankee and Heinz are independent experiments, so the same qualifying selection may be used once in each. Weekly band changes create a new policy version; historical cohorts never change.</div></div><div class='panel'><h2>Current Yankee queues</h2><div class='table-wrap'><table><thead><tr><th>Pool</th><th>Pocket</th><th>Event</th><th>Pick</th><th>Source odds</th><th>Best manual</th><th>Books</th><th>Kickoff</th></tr></thead><tbody>{''.join(qrows) or "<tr><td colspan='8'>Queue empty — waiting for qualifying future selections.</td></tr>"}</tbody></table></div></div><div class='panel'><h2>Forward cards & results</h2>{details}</div></body></html>"""


def _install_dashboard(web_module) -> None:
    app = web_module.app
    if not any(getattr(r, "path", None) == "/hit-rate-multiples" for r in app.router.routes):
        @app.get("/hit-rate-multiples", response_class=HTMLResponse)
        def hit_rate_multiples_page(): return HTMLResponse(_page_html(web_module))
        @app.get("/api/hit-rate-multiples/status")
        def hit_rate_multiples_status(): return scoreboard(web_module.db)
    root = next((r for r in list(app.router.routes) if getattr(r, "path", None) == "/" and "GET" in (getattr(r, "methods", set()) or set())), None)
    if root is None or getattr(root.endpoint, "_hit_rate_wrapped", False): return
    original = root.endpoint; name = root.name; include = getattr(root, "include_in_schema", True); app.router.routes.remove(root)
    async def wrapped_root():
        response = original(); response = await response if inspect.isawaitable(response) else response
        if not isinstance(response, HTMLResponse): return response
        html = response.body.decode("utf-8")
        if "href='/hit-rate-multiples'" not in html:
            link = "<a href='/hit-rate-multiples'>Hit-Rate Multiples</a>"; marker = "<a href='/live-yankee'>Live Yankee Feed</a>"
            html = html.replace(marker, marker + link, 1) if marker in html else html.replace("<div class='nav'>", "<div class='nav'>" + link, 1)
        try:
            m = scoreboard(web_module.db)["streams"]["HR1"]["YANKEE"]
            panel = f"<div class='panel'><h2>Hit-Rate Multiples Lab</h2><div class='summary'>Forward HR1 Yankee: queue <strong>{m['queued']}</strong> · cards <strong>{m['cards']}</strong> · settled <strong>{m['settled']}</strong> · ROI <strong>{'—' if m['roi_pct'] is None else f'{float(m['roi_pct']):+.1f}%'}</strong>. <a href='/hit-rate-multiples'>Open lab →</a></div></div>"
            html = html.replace("</body>", panel + "</body>", 1)
        except Exception: pass
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return HTMLResponse(html, status_code=response.status_code, headers=headers)
    wrapped_root._hit_rate_wrapped = True
    app.add_api_route("/", wrapped_root, methods=["GET"], response_class=HTMLResponse, name=name, include_in_schema=include)


def _install_exports() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        import exporter
        extra = ("hit_rate_policy_versions", "hit_rate_candidates", "hit_rate_quotes", "hit_rate_cards", "hit_rate_legs")
        exporter.EXPORT_TABLES = tuple(dict.fromkeys(tuple(exporter.EXPORT_TABLES) + extra)); out["full"] = list(extra)
    except Exception as exc: out["full_error"] = f"{type(exc).__name__}: {exc}"
    try:
        import split_exporter
        compact = ("hit_rate_policy_versions", "hit_rate_candidates", "hit_rate_cards", "hit_rate_legs")
        split_exporter.WEEKLY_TABLES = tuple(dict.fromkeys(tuple(split_exporter.WEEKLY_TABLES) + compact))
        split_exporter.MANUAL_TABLES = tuple(dict.fromkeys(tuple(split_exporter.MANUAL_TABLES) + compact))
        out["weekly"] = list(compact); out["manual"] = list(compact)
    except Exception as exc: out["split_error"] = f"{type(exc).__name__}: {exc}"
    return out


def install() -> Dict[str, Any]:
    global _INSTALLED, _THREAD
    if _INSTALLED: return {"installed": True, "already_installed": True, "version": VERSION, "policy_version": POLICY_VERSION}
    import web as web_module
    _activate_policy(web_module.db); _install_dashboard(web_module); export_tables = _install_exports()
    worker = web_module.worker; original_start = worker.start; original_stop = worker.stop
    def serialized():
        if not _LOCK.acquire(blocking=False): return
        try: run_maintenance(worker.db, worker.collector, web_module.settings)
        finally: _LOCK.release()
    def loop():
        print(f"HIT_RATE_MULTIPLES_HEARTBEAT started reaction_seconds={REACTION_SECONDS}", flush=True)
        if _STOP.wait(8): return
        while not _STOP.is_set():
            try: serialized()
            except Exception as exc: print(f"HIT_RATE_MULTIPLES_LOOP_ERROR {type(exc).__name__}: {sanitize_sensitive_text(exc)}", flush=True)
            _STOP.wait(REACTION_SECONDS)
    def start_with_hit_rate():
        global _THREAD
        original_start()
        if _THREAD is None or not _THREAD.is_alive():
            _STOP.clear(); _THREAD = threading.Thread(target=loop, daemon=True, name="hit-rate-multiples"); _THREAD.start()
    def stop_with_hit_rate(): _STOP.set(); original_stop()
    worker.start = start_with_hit_rate; worker.stop = stop_with_hit_rate; worker._hit_rate_multiples_installed = True
    web_module.VERSION = "0.19.50"; _INSTALLED = True
    return {"installed": True, "already_installed": False, "version": VERSION, "policy_version": POLICY_VERSION,
            "experiments": list(POLICY), "systems": list(SYSTEM_SPECS), "reaction_seconds": REACTION_SECONDS,
            "export_tables": export_tables, "dashboard_version": web_module.VERSION}
