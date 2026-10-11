from __future__ import annotations

"""Forward-only live-like Yankee assembly feed.

Frozen V1 sources: PRED4, NFL and NCAAF 3.00-4.99. New underlying selections
are deduplicated as they arrive. Four distinct events with fresh supported
manual-book prices form a Yankee and trigger Telegram. A common bookmaker is
preferred; otherwise the card is explicitly CROSS_BOOK_SYNTHETIC. No bet is
ever submitted automatically.
"""

from datetime import datetime, timedelta, timezone
from html import escape
from math import ceil
import hashlib
import inspect
import json
from typing import Any, Dict, Mapping, Optional, Sequence

from fastapi.responses import HTMLResponse

import web as web_module
import manual_systems_shadow
from db import sanitize_sensitive_text, utc_now_iso
from manual_systems_shadow import DEFAULT_PLACEABLE_BOOKS
from quota import provider_actual_cost
from telegram_alerts import _send_telegram

ALGORITHM_VERSION = "YANKEE_LIVE_FEED_V1"
FROZEN_SOURCES = ("PRED4", "NFL", "NCAAF_3_TO_4_99")
QUOTE_REFRESH_MINUTES = 5.0
QUOTE_FRESHNESS_MINUTES = 15.0
FORMATION_HORIZON_HOURS = 30.0
MIN_LEAD_MINUTES = 10.0
MAX_REFRESH_EVENTS_PER_CYCLE = 8
DAILY_REFRESH_CREDIT_BUDGET = 500
REFERENCE_TOTAL_STAKE_GBP = 10.0
YANKEE_LINES = 11
_INSTALLED = False


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


def _candidate_key(event_id: Any, market: Any, selection: Any, point: Any) -> str:
    point_key = ""
    if point is not None:
        try:
            point_key = f"{float(point):.8f}".rstrip("0").rstrip(".")
        except Exception:
            point_key = _norm(point)
    raw = "|".join((_norm(event_id), _norm(market), _norm(selection), point_key))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ensure_schema(db) -> None:
    id_col = "BIGSERIAL PRIMARY KEY" if db.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db.execute("""CREATE TABLE IF NOT EXISTS live_yankee_state (
        singleton_id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
        algorithm_version TEXT NOT NULL, source_policy_json TEXT NOT NULL,
        quote_refresh_minutes REAL NOT NULL, quote_freshness_minutes REAL NOT NULL,
        last_maintenance_at TEXT, last_maintenance_ok INTEGER, last_error TEXT
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS live_yankee_candidates (
        id {id_col}, candidate_key TEXT NOT NULL UNIQUE,
        first_seen_at TEXT NOT NULL, source_created_at TEXT NOT NULL,
        source_lane TEXT NOT NULL, source_table TEXT NOT NULL, source_id INTEGER NOT NULL,
        event_id TEXT NOT NULL, sport_key TEXT, league TEXT, market_key TEXT NOT NULL,
        selection TEXT NOT NULL, point REAL, min_odds REAL NOT NULL, source_odds REAL,
        source_bookmaker_key TEXT, source_bookmaker_title TEXT, edge_pct REAL,
        commence_time TEXT NOT NULL, home_team TEXT, away_team TEXT,
        status TEXT NOT NULL DEFAULT 'QUEUED', consumed_card_id INTEGER, expired_at TEXT
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS live_yankee_quotes (
        id {id_col}, event_id TEXT NOT NULL, captured_at TEXT NOT NULL,
        bookmaker_key TEXT NOT NULL, bookmaker_title TEXT, market_key TEXT NOT NULL,
        selection TEXT NOT NULL, point REAL, price REAL NOT NULL
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS live_yankee_cards (
        id {id_col}, card_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
        algorithm_version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN',
        assembly_mode TEXT NOT NULL, source_mix TEXT, bookmaker_mix TEXT,
        oldest_quote_age_minutes REAL, telegram_status TEXT NOT NULL DEFAULT 'PENDING',
        telegram_attempts INTEGER NOT NULL DEFAULT 0, telegram_sent_at TEXT,
        telegram_error TEXT
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS live_yankee_legs (
        id {id_col}, card_id INTEGER NOT NULL, leg_order INTEGER NOT NULL,
        candidate_id INTEGER NOT NULL, source_lane TEXT NOT NULL, event_id TEXT NOT NULL,
        sport_key TEXT, league TEXT, market_key TEXT NOT NULL, selection TEXT NOT NULL,
        point REAL, entry_odds REAL NOT NULL, bookmaker_key TEXT NOT NULL,
        bookmaker_title TEXT, quote_captured_at TEXT NOT NULL, quote_age_minutes REAL,
        min_odds REAL, commence_time TEXT, home_team TEXT, away_team TEXT,
        UNIQUE(card_id,leg_order)
    )""")
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_live_yankee_candidate_status ON live_yankee_candidates(status,first_seen_at)",
        "CREATE INDEX IF NOT EXISTS idx_live_yankee_quote_lookup ON live_yankee_quotes(event_id,market_key,selection,captured_at)",
        "CREATE INDEX IF NOT EXISTS idx_live_yankee_card_created ON live_yankee_cards(created_at)",
    ):
        try:
            db.execute(sql)
        except Exception:
            pass


def _state(db) -> Dict[str, Any]:
    _ensure_schema(db)
    row = db.fetchone("SELECT * FROM live_yankee_state WHERE singleton_id=1")
    if row:
        return dict(row)
    stamp = utc_now_iso()
    policy = json.dumps({
        "sources": list(FROZEN_SOURCES),
        "pred4": "all current PRED4 H2H/derived-market shadows",
        "nfl": "current Multi-Sport NFL H2H shadows",
        "ncaaf": "current Multi-Sport NCAAF H2H shadows at 3.00-4.99",
        "dedupe": "event+market+selection+point",
        "one_leg_per_event_per_card": True,
        "manual_books": list(DEFAULT_PLACEABLE_BOOKS),
    }, sort_keys=True)
    db.execute("""INSERT INTO live_yankee_state(
        singleton_id,started_at,algorithm_version,source_policy_json,
        quote_refresh_minutes,quote_freshness_minutes,last_maintenance_ok
    ) VALUES(1,?,?,?,?,?,?)""", (
        stamp, ALGORITHM_VERSION, policy, QUOTE_REFRESH_MINUTES,
        QUOTE_FRESHNESS_MINUTES, 1,
    ))
    return dict(db.fetchone("SELECT * FROM live_yankee_state WHERE singleton_id=1") or {})


def _actionable(commence_time: Any, now: datetime) -> bool:
    kickoff = _parse_iso(commence_time)
    return bool(kickoff and now + timedelta(minutes=MIN_LEAD_MINUTES) < kickoff <= now + timedelta(hours=FORMATION_HORIZON_HOURS))


def _source_rows(db, start_iso: str, now: datetime):
    out = []
    pred_specs = (
        ("football_predictive4_bets", """SELECT b.id AS source_id,b.created_at AS source_created_at,b.event_id,
            'h2h' AS market_key,b.selection,NULL AS point,b.min_odds,b.offered_odds AS source_odds,
            b.bookmaker_key AS source_bookmaker_key,b.bookmaker_title AS source_bookmaker_title,b.edge_pct,
            e.sport_key,e.league,e.commence_time,e.home_team,e.away_team
            FROM football_predictive4_bets b JOIN events e ON e.event_id=b.event_id
            WHERE b.created_at>=? AND b.status='OPEN' AND e.status='UPCOMING' ORDER BY b.created_at,b.id"""),
        ("football_predictive4_market_bets", """SELECT b.id AS source_id,b.created_at AS source_created_at,b.event_id,
            b.market_key,b.selection,b.point,b.min_odds,b.offered_odds AS source_odds,
            b.bookmaker_key AS source_bookmaker_key,b.bookmaker_title AS source_bookmaker_title,b.edge_pct,
            e.sport_key,e.league,e.commence_time,e.home_team,e.away_team
            FROM football_predictive4_market_bets b JOIN events e ON e.event_id=b.event_id
            WHERE b.created_at>=? AND b.status='OPEN' AND e.status='UPCOMING' ORDER BY b.created_at,b.id"""),
    )
    for table, sql in pred_specs:
        try:
            rows = db.fetchall(sql, (start_iso,))
        except Exception:
            rows = []
        for raw in rows:
            row = dict(raw)
            if _actionable(row.get("commence_time"), now) and float(row.get("min_odds") or 0) > 1:
                row.update(source_lane="PRED4", source_table=table)
                out.append(row)
    try:
        multi = db.fetchall("""SELECT b.id AS source_id,b.created_at AS source_created_at,b.event_id,
            'h2h' AS market_key,b.selection,NULL AS point,b.min_odds,b.offered_odds AS source_odds,
            b.bookmaker_key AS source_bookmaker_key,b.bookmaker_title AS source_bookmaker_title,b.edge_pct,
            e.sport_key,e.league_title AS league,e.commence_time,e.home_team,e.away_team
            FROM multisport_execution_bets b JOIN multisport_events e ON e.event_id=b.event_id
            WHERE b.created_at>=? AND b.status='OPEN' AND e.status='UPCOMING' ORDER BY b.created_at,b.id""", (start_iso,))
    except Exception:
        multi = []
    for raw in multi:
        row = dict(raw)
        if not _actionable(row.get("commence_time"), now):
            continue
        key = _norm(row.get("sport_key")); odds = float(row.get("source_odds") or 0)
        if "ncaaf" in key and 3.0 <= odds < 5.0:
            row["source_lane"] = "NCAAF_3_TO_4_99"
        elif "nfl" in key:
            row["source_lane"] = "NFL"
        else:
            continue
        if float(row.get("min_odds") or 0) <= 1:
            continue
        row["source_table"] = "multisport_execution_bets"; out.append(row)
    return sorted(out, key=lambda r:(str(r.get("source_created_at") or ""), str(r.get("source_lane") or ""), int(r.get("source_id") or 0)))


def _ingest(db, start_iso: str, now: datetime) -> Dict[str, int]:
    seen = inserted = 0
    for row in _source_rows(db, start_iso, now):
        seen += 1
        key = _candidate_key(row.get("event_id"), row.get("market_key"), row.get("selection"), row.get("point"))
        try:
            changed = db.execute("""INSERT INTO live_yankee_candidates(
                candidate_key,first_seen_at,source_created_at,source_lane,source_table,source_id,
                event_id,sport_key,league,market_key,selection,point,min_odds,source_odds,
                source_bookmaker_key,source_bookmaker_title,edge_pct,commence_time,home_team,away_team,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(candidate_key) DO NOTHING""", (
                key, utc_now_iso(), str(row.get("source_created_at") or utc_now_iso()), row["source_lane"], row["source_table"],
                int(row.get("source_id") or 0), str(row.get("event_id") or ""), str(row.get("sport_key") or ""),
                str(row.get("league") or ""), str(row.get("market_key") or ""), str(row.get("selection") or ""),
                row.get("point"), float(row.get("min_odds") or 0), float(row.get("source_odds") or 0),
                str(row.get("source_bookmaker_key") or ""), str(row.get("source_bookmaker_title") or ""),
                float(row.get("edge_pct") or 0), str(row.get("commence_time") or ""), str(row.get("home_team") or ""),
                str(row.get("away_team") or ""), "QUEUED",
            ))
            inserted += int(changed > 0)
        except Exception:
            pass
    return {"source_rows": seen, "inserted": inserted}


def _expire(db, now: datetime) -> int:
    total = 0
    for row in db.fetchall("SELECT id,commence_time FROM live_yankee_candidates WHERE status='QUEUED'"):
        kickoff = _parse_iso(row.get("commence_time"))
        if kickoff is None or kickoff <= now + timedelta(minutes=MIN_LEAD_MINUTES):
            total += db.execute("UPDATE live_yankee_candidates SET status='EXPIRED',expired_at=? WHERE id=? AND status='QUEUED'", (utc_now_iso(), int(row["id"])))
    return total


def _credits_today(db, now: datetime) -> int:
    row = db.fetchone("""SELECT COALESCE(SUM(actual_cost),0) AS n FROM collector_runs
        WHERE run_type='LIVE_YANKEE_QUOTES' AND started_at>=?""", (now.date().isoformat()+"T00:00:00+00:00",)) or {}
    return int(row.get("n") or 0)


def _targets(db, now: datetime):
    rows = db.fetchall("SELECT * FROM live_yankee_candidates WHERE status='QUEUED' ORDER BY first_seen_at,id")
    by_event = {}
    for row in rows:
        if not _actionable(row.get("commence_time"), now): continue
        eid = str(row.get("event_id") or "")
        item = by_event.setdefault(eid, {"event_id":eid,"sport_key":str(row.get("sport_key") or ""),"commence_time":str(row.get("commence_time") or ""),"first_seen_at":str(row.get("first_seen_at") or ""),"markets":set()})
        item["markets"].add(str(row.get("market_key") or "h2h"))
    if len(by_event) < 4: return []
    due = []
    for item in by_event.values():
        last = db.fetchone("SELECT MAX(started_at) AS at FROM collector_runs WHERE run_type='LIVE_YANKEE_QUOTES' AND event_id=? AND ok=1", (item["event_id"],)) or {}
        last_dt = _parse_iso(last.get("at"))
        if last_dt and (now-last_dt).total_seconds()/60 < QUOTE_REFRESH_MINUTES: continue
        item["markets"] = tuple(sorted(item["markets"])); due.append(item)
    return sorted(due, key=lambda x:(x["first_seen_at"],x["commence_time"],x["event_id"]))


def _store_quotes(db, payload: Any, event_id: str, captured_at: str, markets: Sequence[str]) -> int:
    events = [payload] if isinstance(payload, Mapping) else [x for x in payload if isinstance(x, Mapping)] if isinstance(payload, list) else []
    wanted = set(markets); rows = []
    for event in events:
        pid = str(event.get("id") or event_id)
        if pid != event_id: continue
        for book in event.get("bookmakers") or []:
            bkey = str(book.get("key") or ""); btitle = str(book.get("title") or bkey)
            for market in book.get("markets") or []:
                mkey = str(market.get("key") or "")
                if mkey not in wanted: continue
                stamp = str(market.get("last_update") or captured_at)
                for outcome in market.get("outcomes") or []:
                    try: price = float(outcome.get("price"))
                    except Exception: continue
                    selection = str(outcome.get("name") or "")
                    if bkey and selection and price > 1:
                        rows.append((event_id,stamp,bkey,btitle,mkey,selection,outcome.get("point"),price))
    if rows:
        db.executemany("INSERT INTO live_yankee_quotes(event_id,captured_at,bookmaker_key,bookmaker_title,market_key,selection,point,price) VALUES(?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def _refresh(db, collector, settings, now: datetime) -> Dict[str, Any]:
    books = tuple(dict.fromkeys(str(x) for x in (getattr(settings,"manual_systems_placeable_bookmaker_keys",None) or DEFAULT_PLACEABLE_BOOKS) if str(x)))
    targets = _targets(db, now)
    if not targets: return {"polled":0,"inserted":0,"reason":"fewer_than_four_due_or_all_fresh"}
    spent = _credits_today(db, now); polled = inserted = 0
    for target in targets[:MAX_REFRESH_EVENTS_PER_CYCLE]:
        markets = target["markets"]; estimated = max(1,ceil(len(books)/10))*max(1,len(markets))
        if spent + estimated > DAILY_REFRESH_CREDIT_BUDGET: break
        decision = collector.quota.decide(estimated)
        if not decision.allowed: return {"polled":polled,"inserted":inserted,"reason":decision.reason,"spent":spent}
        actual = 0
        try:
            result = collector.api.event_odds(target["sport_key"],target["event_id"],collector.region,markets,bookmaker_keys=books)
            actual = provider_actual_cost(result.last_cost, estimated)
            collector.quota.update(remaining=result.remaining,used=result.used,last_cost=result.last_cost)
            added = _store_quotes(db,result.data,target["event_id"],utc_now_iso(),markets)
            inserted += added; polled += 1; spent += actual
            db.record_collector_run("LIVE_YANKEE_QUOTES",True,event_id=target["event_id"],sport_key=target["sport_key"],requested_markets=",".join(markets),estimated_cost=estimated,actual_cost=actual,detail=f"books={','.join(books)}; rows={added}")
        except Exception as exc:
            db.record_collector_run("LIVE_YANKEE_QUOTES",False,event_id=target["event_id"],sport_key=target["sport_key"],requested_markets=",".join(markets),estimated_cost=estimated,actual_cost=actual,detail=f"error={sanitize_sensitive_text(exc)}")
    return {"polled":polled,"inserted":inserted,"spent":spent}


def _valid_quotes(db, candidate: Mapping[str,Any], now: datetime):
    rows = db.fetchall("""SELECT * FROM live_yankee_quotes WHERE event_id=? AND market_key=? AND selection=? ORDER BY captured_at DESC,id DESC""", (candidate["event_id"],candidate["market_key"],candidate["selection"]))
    cutoff = now-timedelta(minutes=QUOTE_FRESHNESS_MINUTES); source_at = _parse_iso(candidate.get("source_created_at")); best = {}
    for raw in rows:
        row = dict(raw)
        if not _same_point(row.get("point"),candidate.get("point")): continue
        ts = _parse_iso(row.get("captured_at")); price = float(row.get("price") or 0)
        if ts is None or ts < cutoff or ts > now or (source_at and ts < source_at): continue
        if price + 1e-12 < float(candidate.get("min_odds") or 0): continue
        if candidate.get("source_lane") == "NCAAF_3_TO_4_99" and not (3 <= price < 5): continue
        book = str(row.get("bookmaker_key") or "")
        if not book: continue
        previous = best.get(book)
        if previous is None or (_parse_iso(previous.get("captured_at")) or cutoff) < ts:
            row["quote_age_minutes"] = max(0,(now-ts).total_seconds()/60); best[book] = row
    return best


def _pick_four(db, now: datetime):
    available = []; events = set()
    for raw in db.fetchall("SELECT * FROM live_yankee_candidates WHERE status='QUEUED' ORDER BY first_seen_at,id"):
        c = dict(raw); eid = str(c.get("event_id") or "")
        if eid in events or not _actionable(c.get("commence_time"),now): continue
        quotes = _valid_quotes(db,c,now)
        if not quotes: continue
        available.append((c,quotes)); events.add(eid)
        if len(available)==4: break
    if len(available)<4: return None
    common = set(available[0][1])
    for _,quotes in available[1:]: common.intersection_update(quotes)
    if common:
        book = max(common,key=lambda b:(sum(float(q[b]["price"]) for _,q in available),b))
        chosen = [dict(q[book]) for _,q in available]; mode="SAME_BOOK_VERIFIED"
    else:
        chosen = [dict(max(q.values(),key=lambda x:(float(x["price"]),str(x["bookmaker_key"])))) for _,q in available]
        mode="CROSS_BOOK_SYNTHETIC"
    return [x[0] for x in available],chosen,mode


def _form(db, now: datetime, max_cards: int=5):
    formed=same=cross=0
    for _ in range(max_cards):
        picked = _pick_four(db,now)
        if not picked: break
        candidates,quotes,mode = picked; ids=[int(x["id"]) for x in candidates]
        card_key = hashlib.sha256((ALGORITHM_VERSION+"|"+"|".join(map(str,ids))).encode()).hexdigest()
        source_mix=",".join(sorted({str(x["source_lane"]) for x in candidates})); book_mix=",".join(sorted({str(x["bookmaker_key"]) for x in quotes})); oldest=max(float(x.get("quote_age_minutes") or 0) for x in quotes)
        db.execute("""INSERT INTO live_yankee_cards(card_key,created_at,algorithm_version,status,assembly_mode,source_mix,bookmaker_mix,oldest_quote_age_minutes,telegram_status) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(card_key) DO NOTHING""", (card_key,utc_now_iso(),ALGORITHM_VERSION,"OPEN",mode,source_mix,book_mix,oldest,"PENDING"))
        card=db.fetchone("SELECT * FROM live_yankee_cards WHERE card_key=?",(card_key,));
        if not card: break
        cid=int(card["id"]); existing=int((db.fetchone("SELECT COUNT(*) AS n FROM live_yankee_legs WHERE card_id=?",(cid,)) or {}).get("n") or 0)
        if existing: break
        for order,(c,q) in enumerate(zip(candidates,quotes),1):
            db.execute("""INSERT INTO live_yankee_legs(card_id,leg_order,candidate_id,source_lane,event_id,sport_key,league,market_key,selection,point,entry_odds,bookmaker_key,bookmaker_title,quote_captured_at,quote_age_minutes,min_odds,commence_time,home_team,away_team) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (cid,order,int(c["id"]),c["source_lane"],c["event_id"],c.get("sport_key"),c.get("league"),c["market_key"],c["selection"],c.get("point"),float(q["price"]),q["bookmaker_key"],q.get("bookmaker_title"),q["captured_at"],float(q.get("quote_age_minutes") or 0),float(c.get("min_odds") or 0),c.get("commence_time"),c.get("home_team"),c.get("away_team")))
            db.execute("UPDATE live_yankee_candidates SET status='CONSUMED',consumed_card_id=? WHERE id=? AND status='QUEUED'",(cid,int(c["id"])))
        formed+=1; same+=int(mode=="SAME_BOOK_VERIFIED"); cross+=int(mode=="CROSS_BOOK_SYNTHETIC")
    return {"formed":formed,"same_book":same,"cross_book":cross}


def _market_label(leg: Mapping[str,Any]) -> str:
    market=str(leg.get("market_key") or "").lower(); selection=str(leg.get("selection") or "—"); point=leg.get("point")
    if market=="h2h": return selection
    if market=="btts": return f"BTTS {selection}"
    if market=="totals": return f"{selection} {point}" if point is not None and str(point) not in selection else selection
    return f"{market.replace('_',' ').title()} {selection}".strip()


def _message(card: Mapping[str,Any],legs: Sequence[Mapping[str,Any]]) -> str:
    same=card.get("assembly_mode")=="SAME_BOOK_VERIFIED"
    lines=["🟢 YANKEE FEED — SAME-BOOK VERIFIED" if same else "🟠 YANKEE FEED — CROSS-BOOK ASSEMBLY","",f"🎯 Yankee #{card.get('id')} · 4 selections · 11 lines",f"💷 Reference stake: £{REFERENCE_TOTAL_STAKE_GBP:.2f} total (~£{REFERENCE_TOTAL_STAKE_GBP/YANKEE_LINES:.2f}/line)",f"Sources: {card.get('source_mix') or '—'}",""]
    for i,leg in enumerate(legs,1):
        fixture=f"{leg.get('home_team') or ''} v {leg.get('away_team') or ''}".strip(); kickoff=_parse_iso(leg.get("commence_time")); kt=kickoff.strftime("%d %b %H:%M UTC") if kickoff else str(leg.get("commence_time") or "—")
        lines += [f"{i}. [{leg.get('source_lane')}] {_market_label(leg)} @ {float(leg.get('entry_odds') or 0):.2f}",f"   {leg.get('bookmaker_title') or leg.get('bookmaker_key')} · {fixture} · {kt}"]
    lines += ["",f"Oldest quote: {float(card.get('oldest_quote_age_minutes') or 0):.1f} min"]
    if same:
        lines += [f"Book: {legs[0].get('bookmaker_title') or legs[0].get('bookmaker_key')}","VERIFY ALL FOUR PRICES/MARKETS BEFORE PLACING.","Manual feed only — Betting Lab does not submit bets."]
    else:
        lines += [f"Books: {', '.join(sorted({str(x.get('bookmaker_title') or x.get('bookmaker_key')) for x in legs}))}","CROSS-BOOK SYNTHETIC: these are the best current leg prices across supported books.","This is NOT one verified bookmaker-placeable Yankee; use it as the manual assembly signal and re-check availability.","Betting Lab does not submit bets."]
    return "\n".join(lines)


def _send(db,settings,limit:int=5):
    if not bool(getattr(settings,"telegram_alerts_enabled",False)): return {"enabled":False,"sent":0,"reason":"disabled"}
    token=str(getattr(settings,"telegram_bot_token","") or "").strip(); chat=str(getattr(settings,"telegram_chat_id","") or "").strip()
    if not token or not chat: return {"enabled":True,"sent":0,"reason":"credentials_missing"}
    cards=db.fetchall("SELECT * FROM live_yankee_cards WHERE telegram_status!='SENT' ORDER BY id LIMIT ?",(limit,)); sent=failed=0
    for card in cards:
        legs=db.fetchall("SELECT * FROM live_yankee_legs WHERE card_id=? ORDER BY leg_order",(int(card["id"]),))
        if len(legs)!=4: continue
        try:
            _send_telegram(token,chat,_message(card,legs)); db.execute("UPDATE live_yankee_cards SET telegram_status='SENT',telegram_attempts=telegram_attempts+1,telegram_sent_at=?,telegram_error=NULL WHERE id=?",(utc_now_iso(),int(card["id"]))); sent+=1
        except Exception as exc:
            db.execute("UPDATE live_yankee_cards SET telegram_status='FAILED',telegram_attempts=telegram_attempts+1,telegram_error=? WHERE id=?",(sanitize_sensitive_text(exc),int(card["id"]))); failed+=1
    return {"enabled":True,"eligible":len(cards),"sent":sent,"failed":failed}


def scoreboard(db):
    state=_state(db); c=db.fetchone("""SELECT COUNT(*) AS total,SUM(CASE WHEN status='QUEUED' THEN 1 ELSE 0 END) AS queued,SUM(CASE WHEN status='CONSUMED' THEN 1 ELSE 0 END) AS consumed,SUM(CASE WHEN status='EXPIRED' THEN 1 ELSE 0 END) AS expired FROM live_yankee_candidates""") or {}; cards=db.fetchone("""SELECT COUNT(*) AS cards,SUM(CASE WHEN assembly_mode='SAME_BOOK_VERIFIED' THEN 1 ELSE 0 END) AS same_book,SUM(CASE WHEN assembly_mode='CROSS_BOOK_SYNTHETIC' THEN 1 ELSE 0 END) AS cross_book,SUM(CASE WHEN telegram_status='SENT' THEN 1 ELSE 0 END) AS telegram_sent,MAX(created_at) AS latest_card_at FROM live_yankee_cards""") or {}
    source_counts=db.fetchall("""SELECT source_lane,COUNT(*) AS n,SUM(CASE WHEN status='QUEUED' THEN 1 ELSE 0 END) AS queued,SUM(CASE WHEN status='CONSUMED' THEN 1 ELSE 0 END) AS consumed FROM live_yankee_candidates GROUP BY source_lane ORDER BY source_lane""")
    return {"algorithm_version":ALGORITHM_VERSION,"started_at":state.get("started_at"),"sources":list(FROZEN_SOURCES),"manual_books":list(getattr(web_module.settings,"manual_systems_placeable_bookmaker_keys",DEFAULT_PLACEABLE_BOOKS) or DEFAULT_PLACEABLE_BOOKS),"quote_refresh_minutes":QUOTE_REFRESH_MINUTES,"quote_freshness_minutes":QUOTE_FRESHNESS_MINUTES,"daily_refresh_credit_budget":DAILY_REFRESH_CREDIT_BUDGET,"candidates":int(c.get("total") or 0),"queued":int(c.get("queued") or 0),"consumed":int(c.get("consumed") or 0),"expired":int(c.get("expired") or 0),"cards":int(cards.get("cards") or 0),"same_book":int(cards.get("same_book") or 0),"cross_book":int(cards.get("cross_book") or 0),"telegram_sent":int(cards.get("telegram_sent") or 0),"latest_card_at":cards.get("latest_card_at"),"last_maintenance_at":state.get("last_maintenance_at"),"last_maintenance_ok":bool(state.get("last_maintenance_ok")),"last_error":state.get("last_error"),"source_counts":[dict(x) for x in source_counts]}


def run_maintenance(db,collector,settings,now:Optional[datetime]=None):
    now=now or datetime.now(timezone.utc); now=now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc); state=_state(db)
    try:
        payload={"ok":True,"version":ALGORITHM_VERSION,"expired":_expire(db,now),"ingested":_ingest(db,str(state["started_at"]),now)}
        payload["quotes"]=_refresh(db,collector,settings,now); payload["formed"]=_form(db,now); payload["telegram"]=_send(db,settings); payload["scoreboard"]=scoreboard(db)
        db.execute("UPDATE live_yankee_state SET last_maintenance_at=?,last_maintenance_ok=1,last_error=NULL WHERE singleton_id=1",(utc_now_iso(),)); db.record_collector_run("LIVE_YANKEE_MAINT",True,detail=repr(payload)); print(f"LIVE_YANKEE_MAINT {payload}",flush=True); return payload
    except Exception as exc:
        err=sanitize_sensitive_text(exc); db.execute("UPDATE live_yankee_state SET last_maintenance_at=?,last_maintenance_ok=0,last_error=? WHERE singleton_id=1",(utc_now_iso(),err)); db.record_collector_run("LIVE_YANKEE_MAINT",False,detail=err); print(f"LIVE_YANKEE_MAINT_ERROR {type(exc).__name__}: {err}",flush=True); return {"ok":False,"error":err}


def _fmt_time(value:Any):
    dt=_parse_iso(value); return dt.strftime("%d %b %H:%M UTC") if dt else "—"


def _page_html():
    db=web_module.db; s=scoreboard(db); queued=db.fetchall("""SELECT c.*,(SELECT MAX(q.captured_at) FROM live_yankee_quotes q WHERE q.event_id=c.event_id AND q.market_key=c.market_key AND q.selection=c.selection) AS latest_quote_at FROM live_yankee_candidates c WHERE c.status='QUEUED' ORDER BY c.first_seen_at,c.id LIMIT 80"""); cards=db.fetchall("SELECT * FROM live_yankee_cards ORDER BY id DESC LIMIT 40")
    qhtml="".join(f"<tr><td>{escape(str(x.get('source_lane') or ''))}</td><td>{escape(str(x.get('league') or ''))}</td><td>{escape(str(x.get('home_team') or ''))} v {escape(str(x.get('away_team') or ''))}</td><td>{escape(_market_label(x))}</td><td>{float(x.get('source_odds') or 0):.2f}</td><td>{_fmt_time(x.get('latest_quote_at'))}</td><td>{_fmt_time(x.get('commence_time'))}</td></tr>" for x in queued) or "<tr><td colspan='7'>Queue empty — waiting for new frozen-source selections.</td></tr>"
    chtml="".join(f"<tr><td>{int(x['id'])}</td><td class='{'ok' if x.get('assembly_mode')=='SAME_BOOK_VERIFIED' else 'warn'}'>{escape(str(x.get('assembly_mode') or ''))}</td><td>{escape(str(x.get('source_mix') or ''))}</td><td>{escape(str(x.get('bookmaker_mix') or ''))}</td><td>{float(x.get('oldest_quote_age_minutes') or 0):.1f}m</td><td class='{'ok' if x.get('telegram_status')=='SENT' else 'bad' if x.get('telegram_status')=='FAILED' else ''}'>{escape(str(x.get('telegram_status') or ''))}</td><td>{_fmt_time(x.get('created_at'))}</td></tr>" for x in cards) or "<tr><td colspan='7'>No Yankees formed yet.</td></tr>"
    sources="".join(f"<tr><td>{escape(str(x.get('source_lane') or ''))}</td><td>{int(x.get('n') or 0)}</td><td>{int(x.get('queued') or 0)}</td><td>{int(x.get('consumed') or 0)}</td></tr>" for x in s["source_counts"]) or "<tr><td colspan='4'>No candidates yet.</td></tr>"
    health="HEALTHY" if s.get("last_maintenance_ok") else "WAITING"
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Live Yankee Feed</title><style>{web_module.BASE_STYLE}</style></head><body><a href='/'>← Betting Lab</a><h1>Live Yankee Feed <span class='pill'>LIVE-LIKE · MANUAL · NO AUTO EXECUTION</span></h1><div class='sub'>Frozen V1 sources: <strong>PRED4</strong> · <strong>NFL</strong> · <strong>NCAAF 3.00–4.99</strong>. New selections are deduplicated; four distinct events with fresh supported-book prices form a Yankee and trigger Telegram.</div><div class='grid'><div class='card'><div class='label'>QUEUE</div><div class='value'>{s['queued']}</div></div><div class='card'><div class='label'>YANKEES</div><div class='value'>{s['cards']}</div></div><div class='card'><div class='label'>TELEGRAM</div><div class='value'>{s['telegram_sent']}</div></div><div class='card'><div class='label'>SAME-BOOK</div><div class='value'>{s['same_book']}</div></div><div class='card'><div class='label'>CROSS-BOOK</div><div class='value'>{s['cross_book']}</div></div><div class='card'><div class='label'>QUOTE FRESHNESS</div><div class='value'>{QUOTE_FRESHNESS_MINUTES:.0f}m</div></div></div><div class='panel'><h2>Execution model</h2><div class='summary'>Supported manual books: <strong>{escape(', '.join(s['manual_books']))}</strong>. Targeted prices refresh every ~{QUOTE_REFRESH_MINUTES:.0f} minutes once four distinct queued events exist. A common qualifying bookmaker is preferred; otherwise the best fresh quote per leg is used.</div><div class='muted' style='margin-top:10px'><strong>Cross-book synthetic does not mean a literal one-book Yankee.</strong> The fourfold still requires a common bookmaker, so cross-book cards are manual assembly signals and must be re-checked before placement.</div></div><div class='section-grid'><div class='panel'><h2>Frozen source intake</h2><table><thead><tr><th>Source</th><th>Captured</th><th>Queued</th><th>Used</th></tr></thead><tbody>{sources}</tbody></table></div><div class='panel'><h2>Feed health</h2><div class='summary'>Started: <strong>{_fmt_time(s.get('started_at'))}</strong><br>Last maintenance: <strong>{_fmt_time(s.get('last_maintenance_at'))}</strong><br>Status: <strong class='{'ok' if health=='HEALTHY' else 'warn'}'>{health}</strong><br>Dedicated quote cap: <strong>{DAILY_REFRESH_CREDIT_BUDGET} credits/day</strong></div></div></div><div class='panel'><h2>Current deduplicated queue</h2><div class='table-wrap'><table><thead><tr><th>Source</th><th>League</th><th>Event</th><th>Selection</th><th>Source odds</th><th>Latest manual quote</th><th>Kickoff</th></tr></thead><tbody>{qhtml}</tbody></table></div></div><div class='panel'><h2>Yankee cards</h2><div class='table-wrap'><table><thead><tr><th>ID</th><th>Assembly</th><th>Sources</th><th>Books</th><th>Oldest quote</th><th>Telegram</th><th>Created</th></tr></thead><tbody>{chtml}</tbody></table></div></div></body></html>"""


def _home_panel():
    s=scoreboard(web_module.db); health="HEALTHY" if s.get("last_maintenance_ok") else "WAITING"
    return f"""<div class='panel priority'><h2>Live Yankee Feed <span class='pill {'ok' if health=='HEALTHY' else 'warn'}'>{health}</span></h2><div class='muted'>Forward-only live-like assembly from PRED4 · NFL · NCAAF 3.00–4.99. Selections are deduplicated and four distinct events make one Yankee.</div><div class='grid compact'><div class='card'><div class='label'>Queue</div><div class='value'>{s['queued']}</div></div><div class='card'><div class='label'>Yankees</div><div class='value'>{s['cards']}</div></div><div class='card'><div class='label'>Telegram</div><div class='value'>{s['telegram_sent']}</div></div><div class='card'><div class='label'>Same-book</div><div class='value'>{s['same_book']}</div></div><div class='card'><div class='label'>Cross-book</div><div class='value'>{s['cross_book']}</div></div></div><div style='margin-top:12px'><a href='/live-yankee'>Open Live Yankee Feed →</a></div></div>"""


def _install_routes():
    app=web_module.app
    @app.get('/api/live-yankee/status')
    def live_yankee_status_api(): return scoreboard(web_module.db)
    @app.get('/live-yankee',response_class=HTMLResponse)
    def live_yankee_page(): return HTMLResponse(_page_html())
    route=next((r for r in list(app.router.routes) if getattr(r,'path',None)=='/' and 'GET' in (getattr(r,'methods',set()) or set())),None)
    if route is None: return
    original=route.endpoint; name=route.name; include=getattr(route,'include_in_schema',True); app.router.routes.remove(route)
    async def home_with_yankee():
        response=original(); response=await response if inspect.isawaitable(response) else response
        if not isinstance(response,HTMLResponse): return response
        html=response.body.decode('utf-8'); panel=_home_panel(); marker="<div class='panel'><h2>Deep dives</h2>"; html=html.replace(marker,panel+marker,1) if marker in html else html.replace('</body>',panel+'</body>',1)
        headers={k:v for k,v in response.headers.items() if k.lower()!='content-length'}; return HTMLResponse(content=html,status_code=response.status_code,headers=headers)
    app.add_api_route('/',home_with_yankee,methods=['GET'],response_class=HTMLResponse,name=name,include_in_schema=include)


def _disable_legacy_telegram():
    def disabled(*args,**kwargs): return {"enabled":True,"sent":0,"reason":"replaced_by_live_yankee_feed","replacement":ALGORITHM_VERSION}
    web_module.send_pending_heinz_alerts=disabled; web_module.send_telegram_test_now=disabled; manual_systems_shadow.send_pending_heinz_alerts=disabled


def _install_worker_hook():
    worker=web_module.worker
    if getattr(worker,'_live_yankee_hook_installed',False): return
    original=worker.one_cycle
    def wrapped():
        result=original(); run_maintenance(worker.db,worker.collector,web_module.settings); return result
    worker.one_cycle=wrapped; worker._live_yankee_hook_installed=True


def install():
    global _INSTALLED
    if _INSTALLED: return {"installed":True,"already_installed":True,"version":ALGORITHM_VERSION}
    state=_state(web_module.db); _disable_legacy_telegram(); _install_worker_hook(); _install_routes(); web_module.VERSION='0.19.38'; _INSTALLED=True
    return {"installed":True,"already_installed":False,"version":ALGORITHM_VERSION,"forward_start_at":state.get('started_at'),"sources":list(FROZEN_SOURCES),"dashboard_version":web_module.VERSION}
