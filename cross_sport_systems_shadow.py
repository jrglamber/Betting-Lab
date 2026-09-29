from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import combinations
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from db import Database, utc_now_iso

APP_VERSION = "0.19.2"
ALGORITHM_VERSION = "XS1_CROSS_SPORT_SAME_BOOK_V2"
SYSTEM_SPECS = {"HEINZ": (6, 57), "GOLIATH": (8, 247)}
FORMATION_HORIZON_HOURS = 30.0
MAX_SOURCE_AGE_MINUTES = 45.0
MAX_CARD_QUOTE_SPREAD_MINUTES = 15.0


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ensure_schema(db: Database) -> None:
    id_col = "BIGSERIAL PRIMARY KEY" if db.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    statements = [
        """CREATE TABLE IF NOT EXISTS cross_sport_system_state (
            singleton_id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, algorithm_version TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS cross_sport_system_bets (
            id {id_col}, system_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
            algorithm_version TEXT NOT NULL, system_type TEXT NOT NULL, leg_count INTEGER NOT NULL,
            line_count INTEGER NOT NULL, pricing_mode TEXT NOT NULL, total_stake_units REAL NOT NULL DEFAULT 1,
            line_stake_units REAL NOT NULL, singles_control_stake_units REAL NOT NULL,
            first_kickoff TEXT NOT NULL, last_kickoff TEXT NOT NULL, sports_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN', result TEXT, winning_legs INTEGER,
            system_return_units REAL, system_pnl_units REAL, system_roi_pct REAL,
            singles_return_units REAL, singles_pnl_units REAL, singles_roi_pct REAL,
            avg_leg_clv_pct REAL, ab_clv_samples INTEGER NOT NULL DEFAULT 0, settled_at TEXT,
            app_version TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS cross_sport_system_legs (
            id {id_col}, system_bet_id INTEGER NOT NULL, leg_order INTEGER NOT NULL,
            selection_key TEXT NOT NULL, source_family TEXT NOT NULL, source_table TEXT NOT NULL,
            source_id INTEGER NOT NULL, event_id TEXT NOT NULL, sport_key TEXT NOT NULL,
            market_key TEXT NOT NULL, selection TEXT NOT NULL, entry_odds REAL NOT NULL,
            fair_probability REAL NOT NULL, min_odds REAL NOT NULL, bookmaker_key TEXT,
            source_created_at TEXT NOT NULL, commence_time TEXT NOT NULL, closing_odds REAL,
            clv_pct REAL, clv_quality TEXT, result TEXT, UNIQUE(system_bet_id,leg_order),
            FOREIGN KEY(system_bet_id) REFERENCES cross_sport_system_bets(id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS cross_sport_system_lines (
            id {id_col}, system_bet_id INTEGER NOT NULL, line_order INTEGER NOT NULL,
            line_size INTEGER NOT NULL, leg_orders_json TEXT NOT NULL, entry_odds REAL NOT NULL,
            fair_probability REAL NOT NULL, stake_units REAL NOT NULL, result TEXT,
            return_units REAL, pnl_units REAL, UNIQUE(system_bet_id,line_order),
            FOREIGN KEY(system_bet_id) REFERENCES cross_sport_system_bets(id)
        )""",
    ]
    for sql in statements:
        db.execute(sql)


def ensure_cross_sport_state(db: Database) -> Dict[str, Any]:
    _ensure_schema(db)
    row = db.fetchone("SELECT * FROM cross_sport_system_state WHERE singleton_id=1")
    if row:
        return dict(row)
    stamp = utc_now_iso()
    db.execute("INSERT INTO cross_sport_system_state(singleton_id,started_at,algorithm_version) VALUES(1,?,?)",
               (stamp, ALGORITHM_VERSION))
    return {"singleton_id": 1, "started_at": stamp, "algorithm_version": ALGORITHM_VERSION}


def _candidates(db: Database, now: datetime, started_at: datetime) -> List[Dict[str, Any]]:
    horizon = now + timedelta(hours=FORMATION_HORIZON_HOURS)
    out: List[Dict[str, Any]] = []
    sources = (
        ("FOOTBALL", """SELECT b.id,b.created_at,b.event_id,e.sport_key,e.commence_time,
             b.market_key,b.selection,b.offered_odds,b.fair_probability,b.min_odds,b.edge_pct,
             b.bookmaker_key,b.status
             FROM execution_shadow_bets b JOIN events e ON e.event_id=b.event_id
             WHERE b.status='OPEN' AND e.status='UPCOMING'""", "execution_shadow_bets"),
        ("MULTISPORT", """SELECT b.id,b.created_at,b.event_id,b.sport_key,e.commence_time,
             'h2h' AS market_key,b.selection,b.offered_odds,b.fair_probability,b.min_odds,b.edge_pct,
             b.bookmaker_key,b.status
             FROM multisport_execution_bets b JOIN multisport_events e ON e.event_id=b.event_id
             WHERE b.status='OPEN' AND e.status='UPCOMING'""", "multisport_execution_bets"),
    )
    for family, sql, table in sources:
        try:
            rows = db.fetchall(sql)
        except Exception:
            continue
        for raw in rows:
            row = dict(raw)
            try:
                created = _parse_iso(str(row["created_at"]))
                kickoff = _parse_iso(str(row["commence_time"]))
                odds = float(row["offered_odds"])
                prob = float(row["fair_probability"])
            except Exception:
                continue
            if created < started_at or not (now < kickoff <= horizon):
                continue
            age = (now-created).total_seconds()/60.0
            if age < 0 or age > MAX_SOURCE_AGE_MINUTES or odds <= 1.0 or prob <= 0:
                continue
            row.update(source_family=family, source_table=table, source_id=int(row["id"]),
                       kickoff_dt=kickoff, entry_odds=odds,
                       selection_key=f"{family}|{row['event_id']}|{row['market_key']}|{row['selection']}")
            out.append(row)
    # Exact selection dedup, then one selection per event. Ranking is frozen:
    # highest source edge, then best price, then earliest source row.
    exact: Dict[str, Dict[str, Any]] = {}
    for row in out:
        book = str(row.get("bookmaker_key") or "")
        if not book:
            continue
        key = book + "|" + str(row["selection_key"])
        prev = exact.get(key)
        rank = (float(row.get("edge_pct") or 0), float(row["entry_odds"]), -int(row["source_id"]))
        if prev is None or rank > (float(prev.get("edge_pct") or 0), float(prev["entry_odds"]), -int(prev["source_id"])):
            exact[key] = row
    by_event: Dict[str, Dict[str, Any]] = {}
    for row in exact.values():
        key = str(row.get("bookmaker_key") or "") + "|" + str(row["source_family"]) + "|" + str(row["event_id"])
        prev = by_event.get(key)
        if prev is None or (float(row.get("edge_pct") or 0), float(row["entry_odds"])) > (float(prev.get("edge_pct") or 0), float(prev["entry_odds"])):
            by_event[key] = row
    return sorted(by_event.values(), key=lambda r: (-float(r.get("edge_pct") or 0), -float(r["entry_odds"]), str(r["selection_key"])))


def _line_combos(system_type: str, n: int) -> List[Tuple[int,...]]:
    if system_type == "HEINZ" and n == 6:
        sizes = range(2,7)
    elif system_type == "GOLIATH" and n == 8:
        sizes = range(2,9)
    else:
        return []
    orders = tuple(range(1,n+1))
    out: List[Tuple[int,...]] = []
    for size in sizes:
        out.extend(combinations(orders,size))
    return out


def generate_cross_sport_systems(db: Database, now: Optional[datetime]=None) -> int:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    state = ensure_cross_sport_state(db)
    started = _parse_iso(str(state["started_at"]))
    pool = _candidates(db, now, started)
    existing = {str(r["system_key"]) for r in db.fetchall("SELECT system_key FROM cross_sport_system_bets")}
    used = {str(r["selection_key"]) for r in db.fetchall(
        """SELECT l.selection_key FROM cross_sport_system_legs l
           JOIN cross_sport_system_bets b ON b.id=l.system_bet_id
           WHERE b.status IN ('OPEN','SETTLED')""")}
    created = 0
    for system_type, (n, expected_lines) in SYSTEM_SPECS.items():
        eligible = [r for r in pool if str(r["selection_key"]) not in used]
        books = sorted({str(r.get("bookmaker_key") or "") for r in eligible if r.get("bookmaker_key")})
        book_pools = [(b,[r for r in eligible if str(r.get("bookmaker_key") or "")==b]) for b in books]
        book_pools = [(b,p) for b,p in book_pools if len(p) >= n]
        if not book_pools:
            continue
        book, eligible = max(book_pools, key=lambda bp: sum(float(r.get("edge_pct") or 0) for r in bp[1][:n]))
        if len(eligible) < n:
            continue
        chosen = eligible[:n]
        quote_times=[_parse_iso(str(r["created_at"])) for r in chosen]
        if (max(quote_times)-min(quote_times)).total_seconds()/60.0 > MAX_CARD_QUOTE_SPREAD_MINUTES:
            continue
        # Cross-sport means at least two sport keys. No sport quotas are fitted.
        if len({str(r["sport_key"]) for r in chosen}) < 2:
            continue
        signature = "|".join(sorted(str(r["selection_key"]) for r in chosen))
        key = f"{ALGORITHM_VERSION}|{book}|{system_type}|{signature}"
        if key in existing:
            continue
        combos = _line_combos(system_type,n)
        if len(combos) != expected_lines:
            continue
        line_stake = 1.0/expected_lines
        first_kickoff=min(r["kickoff_dt"] for r in chosen).isoformat()
        last_kickoff=max(r["kickoff_dt"] for r in chosen).isoformat()
        sports=sorted({str(r["sport_key"]) for r in chosen})
        db.execute("""INSERT INTO cross_sport_system_bets(
            system_key,created_at,algorithm_version,system_type,leg_count,line_count,pricing_mode,
            total_stake_units,line_stake_units,singles_control_stake_units,first_kickoff,last_kickoff,
            sports_json,status,app_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?)""",
            (key,utc_now_iso(),ALGORITHM_VERSION,system_type,n,expected_lines,
             f"SAME_BOOK_SIMULTANEOUS:{book}",1.0,line_stake,1.0,first_kickoff,last_kickoff,
             json.dumps(sports,separators=(",",":")),APP_VERSION))
        bet_id=int(db.fetchone("SELECT id FROM cross_sport_system_bets WHERE system_key=?",(key,))["id"])
        for i,row in enumerate(chosen,1):
            db.execute("""INSERT INTO cross_sport_system_legs(
                system_bet_id,leg_order,selection_key,source_family,source_table,source_id,event_id,
                sport_key,market_key,selection,entry_odds,fair_probability,min_odds,bookmaker_key,
                source_created_at,commence_time) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (bet_id,i,row["selection_key"],row["source_family"],row["source_table"],row["source_id"],
                 row["event_id"],row["sport_key"],row["market_key"],row["selection"],row["entry_odds"],
                 row["fair_probability"],row["min_odds"],row.get("bookmaker_key"),row["created_at"],
                 row["commence_time"]))
        for j,combo in enumerate(combos,1):
            legs=[chosen[i-1] for i in combo]
            odds=1.0; prob=1.0
            for leg in legs:
                odds*=float(leg["entry_odds"]); prob*=float(leg["fair_probability"])
            db.execute("""INSERT INTO cross_sport_system_lines(
                system_bet_id,line_order,line_size,leg_orders_json,entry_odds,fair_probability,stake_units)
                VALUES(?,?,?,?,?,?,?)""",(bet_id,j,len(combo),json.dumps(combo),odds,prob,line_stake))
        used.update(str(r["selection_key"]) for r in chosen)
        existing.add(key); created+=1
    return created


def settle_cross_sport_systems(db: Database) -> int:
    _ensure_schema(db)
    settled=0
    cards=db.fetchall("SELECT * FROM cross_sport_system_bets WHERE status='OPEN'")
    for card in cards:
        legs=db.fetchall("SELECT * FROM cross_sport_system_legs WHERE system_bet_id=? ORDER BY leg_order",(card["id"],))
        ready=True; wins=0; clvs=[]
        for leg in legs:
            src=db.fetchone(f"SELECT result,closing_odds,clv_pct,clv_quality,status FROM {leg['source_table']} WHERE id=?",(leg["source_id"],))
            if not src or str(src.get("status") or "")!="SETTLED" or not src.get("result"):
                ready=False; break
            result=str(src["result"]).upper()
            if result=="WIN": wins+=1
            if src.get("clv_pct") is not None:
                clvs.append(float(src["clv_pct"]))
            db.execute("""UPDATE cross_sport_system_legs SET result=?,closing_odds=?,clv_pct=?,clv_quality=?
                          WHERE id=?""",(result,src.get("closing_odds"),src.get("clv_pct"),src.get("clv_quality"),leg["id"]))
        if not ready:
            continue
        lines=db.fetchall("SELECT * FROM cross_sport_system_lines WHERE system_bet_id=? ORDER BY line_order",(card["id"],))
        system_return=0.0
        for line in lines:
            orders=json.loads(str(line["leg_orders_json"]))
            picked=[legs[int(i)-1] for i in orders]
            won=all(str(x["result"]).upper()=="WIN" for x in picked)
            ret=float(line["stake_units"])*float(line["entry_odds"]) if won else 0.0
            system_return+=ret
            db.execute("UPDATE cross_sport_system_lines SET result=?,return_units=?,pnl_units=? WHERE id=?",
                       ("WIN" if won else "LOSS",ret,ret-float(line["stake_units"]),line["id"]))
        singles_return=sum((float(x["entry_odds"])/len(legs)) if str(x["result"]).upper()=="WIN" else 0.0 for x in legs)
        pnl=system_return-1.0; spnl=singles_return-1.0
        db.execute("""UPDATE cross_sport_system_bets SET status='SETTLED',result=?,winning_legs=?,
            system_return_units=?,system_pnl_units=?,system_roi_pct=?,singles_return_units=?,
            singles_pnl_units=?,singles_roi_pct=?,avg_leg_clv_pct=?,ab_clv_samples=?,settled_at=? WHERE id=?""",
            ("PROFIT" if pnl>0 else ("LOSS" if pnl<0 else "PUSH"),wins,system_return,pnl,pnl*100,
             singles_return,spnl,spnl*100,(sum(clvs)/len(clvs) if clvs else None),len(clvs),utc_now_iso(),card["id"]))
        settled+=1
    return settled


def run_cross_sport_systems_maintenance(db: Database, now: Optional[datetime]=None) -> Dict[str,Any]:
    ensure_cross_sport_state(db)
    settled=settle_cross_sport_systems(db)
    created=generate_cross_sport_systems(db,now=now)
    return {"algorithm_version":ALGORITHM_VERSION,"created":created,"settled":settled,
            "systems":list(SYSTEM_SPECS),"shadow_only":True}


def cross_sport_systems_scoreboard(db: Database) -> Dict[str, Any]:
    _ensure_schema(db)
    state = db.fetchone("SELECT * FROM cross_sport_system_state WHERE singleton_id=1") or {}
    rows = db.fetchall("""SELECT system_type,status,system_pnl_units,singles_pnl_units,
                                system_roi_pct,singles_roi_pct,avg_leg_clv_pct
                         FROM cross_sport_system_bets""")
    def summarize(items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        settled=[r for r in items if str(r.get("status") or "")=="SETTLED"]
        return {
            "cards": len(items), "open": sum(1 for r in items if str(r.get("status") or "")=="OPEN"),
            "settled": len(settled),
            "system_pnl_units": sum(float(r.get("system_pnl_units") or 0) for r in settled),
            "singles_pnl_units": sum(float(r.get("singles_pnl_units") or 0) for r in settled),
            "system_roi_pct": (sum(float(r.get("system_pnl_units") or 0) for r in settled)/len(settled)*100) if settled else None,
            "singles_roi_pct": (sum(float(r.get("singles_pnl_units") or 0) for r in settled)/len(settled)*100) if settled else None,
            "avg_leg_clv_pct": (sum(float(r["avg_leg_clv_pct"]) for r in settled if r.get("avg_leg_clv_pct") is not None) /
                                sum(1 for r in settled if r.get("avg_leg_clv_pct") is not None)) if any(r.get("avg_leg_clv_pct") is not None for r in settled) else None,
        }
    return {"algorithm_version": ALGORITHM_VERSION, "started_at": state.get("started_at"),
            "shadow_only": True, "overall": summarize(rows),
            "by_system": {k:summarize([r for r in rows if str(r.get("system_type"))==k]) for k in SYSTEM_SPECS}}


def latest_cross_sport_system_cards(db: Database, limit: int=100) -> List[Dict[str, Any]]:
    _ensure_schema(db)
    cards=db.fetchall("SELECT * FROM cross_sport_system_bets ORDER BY id DESC LIMIT ?",(int(limit),))
    out=[]
    for card in cards:
        row=dict(card)
        row["legs"]=db.fetchall("""SELECT leg_order,source_family,sport_key,event_id,market_key,selection,
                                         entry_odds,result,clv_pct FROM cross_sport_system_legs
                                  WHERE system_bet_id=? ORDER BY leg_order""",(card["id"],))
        out.append(row)
    return out
