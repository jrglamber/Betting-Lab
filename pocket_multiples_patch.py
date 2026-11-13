from __future__ import annotations
"""Prospective pocket multiples; independent of HR1/HR2 formation and notifications."""
from datetime import datetime,timedelta,timezone
from hashlib import sha256
from itertools import combinations
from math import prod
from html import escape
from fastapi.responses import HTMLResponse
import threading
import hit_rate_multiples_lab as hr
import telegram_alerts
from db import utc_now_iso,sanitize_sensitive_text

VERSION="POCKET_MULTIPLES_V1"
SPECS={"EUROLEAGUE_DOUBLE":("basketball_euroleague",1.5,4.0,2),
       "NCAAF_YANKEE":("americanfootball_ncaaf",3.0,5.0,4)}
BOOKS={"williamhill","ladbrokes_uk","betfred_uk","boylesports"}
_LOCK=threading.Lock()

def schema(db):
    pk="BIGSERIAL PRIMARY KEY" if db.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db.execute(f"""CREATE TABLE IF NOT EXISTS pocket_multiple_legs(
        id {pk}, pocket TEXT NOT NULL, source_id INTEGER NOT NULL, event_id TEXT NOT NULL,
        selection TEXT NOT NULL, bookmaker_key TEXT NOT NULL, odds REAL NOT NULL,
        first_seen_at TEXT NOT NULL, quote_at TEXT NOT NULL, commence_time TEXT,
        home_team TEXT,away_team TEXT, status TEXT NOT NULL DEFAULT 'QUEUED',
        card_id INTEGER, UNIQUE(pocket,source_id,bookmaker_key))""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS pocket_multiple_cards(
        id {pk}, card_key TEXT NOT NULL UNIQUE,pocket TEXT NOT NULL,
        bookmaker_key TEXT NOT NULL,created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'OPEN',result TEXT,
        return_units REAL,pnl_units REAL,settled_at TEXT,
        telegram_status TEXT NOT NULL DEFAULT 'PENDING',
        telegram_message_id TEXT,telegram_error TEXT)""")

def _parse(x):
    return hr._parse(x)

def _source(db,now):
    # Same underlying multisport research executions, without altering HR2.
    return db.fetchall("""SELECT b.id AS source_id,b.created_at AS first_seen_at,
        b.event_id,b.selection,b.offered_odds,e.sport_key,e.commence_time,
        e.home_team,e.away_team
        FROM multisport_execution_bets b JOIN multisport_events e ON e.event_id=b.event_id
        WHERE b.status='OPEN' AND e.status='UPCOMING'
        ORDER BY b.created_at,b.id LIMIT 2500""")

def ingest(db,now):
    for raw in _source(db,now):
        c=dict(raw)
        for pocket,(sport,lo,hi,need) in SPECS.items():
            if str(c.get("sport_key"))!=sport: continue
            if not (lo<=float(c.get("offered_odds") or 0)<hi): continue
            first=_parse(c.get("first_seen_at"));kick=_parse(c.get("commence_time"))
            if not first or not kick or kick<=now+timedelta(minutes=10): continue
            # Quotes are current, same bookmaker, and can differ from historical price.
            quote_candidate={"event_id":c["event_id"],"market_key":"h2h",
                "selection":c["selection"],"point":None,"first_seen_at":c["first_seen_at"],
                "band_min":1.01,"band_max":10000,"min_odds":1.01}
            for book,q in hr._quotes(db,quote_candidate,now,True).items():
                if book not in BOOKS: continue
                db.execute("""INSERT INTO pocket_multiple_legs(
                    pocket,source_id,event_id,selection,bookmaker_key,odds,
                    first_seen_at,quote_at,commence_time,home_team,away_team
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(pocket,source_id,bookmaker_key) DO NOTHING""",
                (pocket,int(c["source_id"]),c["event_id"],c["selection"],book,
                 float(q["price"]),c["first_seen_at"],q["captured_at"],c["commence_time"],
                 c.get("home_team"),c.get("away_team")))

def form(db,now):
    made=0
    for pocket,(_,_,_,need) in SPECS.items():
        for book in sorted(BOOKS):
            rows=[dict(x) for x in db.fetchall("""SELECT * FROM pocket_multiple_legs
                WHERE pocket=? AND bookmaker_key=? AND status='QUEUED'
                ORDER BY first_seen_at,id LIMIT 200""",(pocket,book))]
            eligible=[]
            for c in rows:
                first=_parse(c["first_seen_at"]);kick=_parse(c["commence_time"])
                if first and kick and now-first<=timedelta(hours=24) and kick>now+timedelta(minutes=10):
                    eligible.append(c)
            # Each 24h window is anchored on the oldest member, with no fixture repeated.
            while len(eligible)>=need:
                group=[];events=set();anchor=_parse(eligible[0]["first_seen_at"])
                for c in eligible:
                    ts=_parse(c["first_seen_at"])
                    if ts and anchor and ts-anchor<=timedelta(hours=24) and c["event_id"] not in events:
                        group.append(c);events.add(c["event_id"])
                    if len(group)==need:break
                if len(group)<need:break
                ids=[int(x["id"]) for x in group]
                key=sha256((pocket+"|"+book+"|"+",".join(map(str,ids))).encode()).hexdigest()
                db.execute("""INSERT INTO pocket_multiple_cards(card_key,pocket,bookmaker_key,created_at)
                    VALUES(?,?,?,?) ON CONFLICT(card_key) DO NOTHING""",(key,pocket,book,utc_now_iso()))
                card=db.fetchone("SELECT id FROM pocket_multiple_cards WHERE card_key=?",(key,))
                for x in group:
                    db.execute("UPDATE pocket_multiple_legs SET status='USED',card_id=? WHERE id=? AND status='QUEUED'",
                               (int(card["id"]),int(x["id"])))
                eligible=[x for x in eligible if int(x["id"]) not in ids]
                made+=1
    return made

def settle(db):
    done=0
    for card in db.fetchall("SELECT * FROM pocket_multiple_cards WHERE status='OPEN' ORDER BY id LIMIT 200"):
        legs=[dict(x) for x in db.fetchall("SELECT * FROM pocket_multiple_legs WHERE card_id=? ORDER BY id",(card["id"],))]
        if len(legs) not in (2,4):continue
        outcomes=[]
        for leg in legs:
            outcome=hr._source_result(db,{"source_table":"multisport_execution_bets","source_id":leg["source_id"]})
            if outcome is None:break
            outcomes.append(outcome)
        if len(outcomes)!=len(legs):continue
        if "VOID" in outcomes or "PUSH" in outcomes:
            db.execute("UPDATE pocket_multiple_cards SET status='VOID',result='VOID',settled_at=? WHERE id=?",
                       (utc_now_iso(),card["id"]))
            continue
        odds=[float(x["odds"]) for x in legs]
        sizes=(2,) if len(legs)==2 else (2,3,4)
        lines=sum(len(list(combinations(range(len(legs)),n))) for n in sizes)
        returned=sum(prod(odds[i] for i in idx) for n in sizes for idx in combinations(range(len(legs)),n)
                     if all(outcomes[i]=='WIN' for i in idx))/lines
        pnl=returned-1
        db.execute("""UPDATE pocket_multiple_cards SET status='SETTLED',result=?,
            return_units=?,pnl_units=?,settled_at=? WHERE id=?""",
            ("WIN" if pnl>0 else "LOSS",returned,pnl,utc_now_iso(),card["id"]))
        done+=1
    return done

def notify(db,settings):
    if not getattr(settings,"telegram_alerts_enabled",False):return 0
    token=getattr(settings,"telegram_bot_token",None);chat=getattr(settings,"telegram_chat_id",None)
    if not token or not chat:return 0
    sent=0;now=datetime.now(timezone.utc)
    for card in db.fetchall("""SELECT * FROM pocket_multiple_cards
        WHERE status='OPEN' AND telegram_status='PENDING' ORDER BY id LIMIT 20"""):
        legs=[dict(x) for x in db.fetchall("SELECT * FROM pocket_multiple_legs WHERE card_id=? ORDER BY id",(card["id"],))]
        if not legs or any((_parse(x["commence_time"]) or now)<=now+timedelta(minutes=10) for x in legs):
            db.execute("UPDATE pocket_multiple_cards SET telegram_status='EXPIRED' WHERE id=?",(card["id"],))
            continue
        title="EUROLEAGUE 24H DOUBLE" if card["pocket"]=="EUROLEAGUE_DOUBLE" else "NCAAF 24H YANKEE"
        lines=[f"🎯 POCKET MULTIPLES — {title}",f"Bookmaker: {card['bookmaker_key']}",
               f"Card #{card['id']} · RECHECK LIVE PRICES"]
        for i,x in enumerate(legs,1):
            lines.append(f"{i}. {x['selection']} @ {float(x['odds']):.2f} — {x['home_team']} v {x['away_team']}")
        lines.extend(["Research card only. Odds are observed, not guaranteed.",
                      "Confirm all legs and current bookmaker prices before manually placing."])
        # Claim first; uncertain network attempts must not create repeated prompts.
        db.execute("UPDATE pocket_multiple_cards SET telegram_status='SENDING' WHERE id=? AND telegram_status='PENDING'",(card["id"],))
        try:
            result=telegram_alerts._send_telegram(token,chat,"\n".join(lines),source="POCKET_MULTIPLES")
            if not result.get("ok"):raise RuntimeError("Telegram not confirmed")
            db.execute("""UPDATE pocket_multiple_cards SET telegram_status='SENT',telegram_message_id=?
                WHERE id=?""",(str(result.get("message_id") or ""),card["id"]))
            sent+=1
        except Exception as exc:
            db.execute("UPDATE pocket_multiple_cards SET telegram_status='UNCERTAIN',telegram_error=? WHERE id=?",
                       (sanitize_sensitive_text(exc),card["id"]))
    return sent

def status(db):
    schema(db)
    data={}
    for pocket in SPECS:
        cards=[dict(x) for x in db.fetchall("SELECT * FROM pocket_multiple_cards WHERE pocket=?",(pocket,))]
        queue=db.fetchone("""SELECT COUNT(*) AS n FROM pocket_multiple_legs
            WHERE pocket=? AND status='QUEUED'""",(pocket,))
        settled=[x for x in cards if x["status"]=="SETTLED"]
        data[pocket]={"queued":int(queue["n"]),"cards":len(cards),"settled":len(settled),
            "profit_units":round(sum(float(x.get("pnl_units") or 0) for x in settled),3),
            "roi_pct":round(100*sum(float(x.get("pnl_units") or 0) for x in settled)/len(settled),2) if settled else None,
            "telegram_sent":sum(x["telegram_status"]=="SENT" for x in cards)}
    return {"version":VERSION,"streams":data}

def install(app,db,settings):
    schema(db)
    @app.get("/api/pocket-multiples/status")
    def pocket_status():return status(db)
    @app.get("/hit-rate-multiples/pocket-multiples",response_class=HTMLResponse)
    def pocket_page():
        info=status(db)
        panels="".join(f"<section style='padding:20px;border:1px solid #ddd;margin:12px 0;border-radius:12px'><h2>{escape(p)}</h2><pre>{escape(str(v))}</pre></section>" for p,v in info["streams"].items())
        return HTMLResponse("<html><head><meta name='viewport' content='width=device-width,initial-scale=1'></head><body style='font:16px system-ui;max-width:900px;margin:30px auto;padding:12px'><a href='/hit-rate-multiples'>← Hit Rate Lab</a><h1>Pocket Multiples</h1><p>Forward-only · 24-hour chronological grouping · manual betting only · HR2 research remains active</p>"+panels+"</body></html>")
    original_page=hr._page
    def pocket_lab_page(web_module):
        html=original_page(web_module)
        link="<div class='panel'><h2>Pocket Multiples</h2><p>EuroLeague 24h doubles and NCAAF 24h Yankees. HR2 research continues.</p><a href='/hit-rate-multiples/pocket-multiples'>Open Pocket Multiples</a></div>"
        return html.replace("</body>",link+"</body>",1)
    hr._page=pocket_lab_page
    # Preserve existing HR2 worker and research; run after its formation cycle.
    previous=hr._form
    def wrapped(db_obj,now):
        result=previous(db_obj,now)
        if not _LOCK.acquire(blocking=False):return result
        try:
            ingest(db_obj,now)
            made=form(db_obj,now);closed=settle(db_obj);sent=notify(db_obj,settings)
            if made or closed or sent:print(f"POCKET_MULTIPLES_MAINT formed={made} settled={closed} sent={sent}",flush=True)
        except Exception as exc:
            print(f"POCKET_MULTIPLES_ERROR {type(exc).__name__}: {sanitize_sensitive_text(exc)}",flush=True)
        finally:_LOCK.release()
        return result
    hr._form=wrapped
    try:
        import split_exporter
        split_exporter.WEEKLY_TABLES=tuple(dict.fromkeys(tuple(split_exporter.WEEKLY_TABLES)+("pocket_multiple_legs","pocket_multiple_cards")))
    except Exception:pass
    return {"installed":True,"version":VERSION,"streams":list(SPECS)}
