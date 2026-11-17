"""Lean EuroLeague-only Betting Lab. Research only: no execution or Telegram."""
from __future__ import annotations

import asyncio
import csv
import os
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from html import escape
from io import StringIO
from statistics import mean

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response

from db import Database, sanitize_sensitive_text
from the_odds_api import TheOddsApi

SPORT = "basketball_euroleague"
BANDS = ((1.50, 2.00), (3.00, 4.00))
BOOKS = tuple(x.strip() for x in os.getenv(
    "LITE_BOOKS", "williamhill,ladbrokes_uk,betfred_uk,boylesports,betfair_ex_uk"
).split(",") if x.strip())
EXCHANGES = {"betfair_ex_uk", "matchbook", "smarkets"}
MIN_REFERENCES = 2
EDGE_FLOOR = 0.04
MAX_CREDITS = max(1, min(30, int(os.getenv("LITE_CREDIT_CAP", "12"))))
ODDS_HOURS = max(4, int(os.getenv("LITE_ODDS_HOURS", "6")))
RESULT_HOURS = max(8, int(os.getenv("LITE_RESULTS_HOURS", "12")))
DB_URL = os.getenv("DATABASE_URL", "")
db = Database(database_url=DB_URL)
api = TheOddsApi(os.getenv("ODDS_API_KEY", ""))


def now_utc():
    return datetime.now(timezone.utc)


def stamp(raw):
    try:
        d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return d.astimezone(timezone.utc) if d.tzinfo else None
    except (TypeError, ValueError):
        return None


def normalize(value):
    return " ".join(str(value or "").casefold().split())


def schema():
    if not DB_URL.startswith(("postgres://", "postgresql://")):
        raise RuntimeError("Lite requires existing Postgres DATABASE_URL; refusing ephemeral SQLite")
    db.execute("""CREATE TABLE IF NOT EXISTS lite_runs(
        id BIGSERIAL PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL,
        status TEXT NOT NULL, credits INTEGER NOT NULL, info TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS lite_picks(
        event_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL,
        commence_time TEXT NOT NULL, home_team TEXT NOT NULL, away_team TEXT NOT NULL,
        selection TEXT NOT NULL, bookmaker_key TEXT NOT NULL,
        bookmaker_title TEXT NOT NULL, entry_odds DOUBLE PRECISION NOT NULL,
        reference_probability DOUBLE PRECISION NOT NULL,
        estimated_edge_pct DOUBLE PRECISION NOT NULL, reference_books INTEGER NOT NULL,
        commission_assumption DOUBLE PRECISION NOT NULL,
        result TEXT, home_score INTEGER, away_score INTEGER,
        pnl_units DOUBLE PRECISION, settled_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS lite_quotes(
        event_id TEXT NOT NULL, captured_at TEXT NOT NULL,
        bookmaker_key TEXT NOT NULL, selection TEXT NOT NULL,
        price DOUBLE PRECISION NOT NULL, commence_time TEXT NOT NULL,
        PRIMARY KEY(event_id,captured_at,bookmaker_key,selection))""")


def record(kind, at, status, cost=0, info=""):
    db.execute("INSERT INTO lite_runs(at,kind,status,credits,info) VALUES(?,?,?,?,?)",
               (at.isoformat(), kind, status, int(cost), str(info)[:350]))
    # Summarised, redacted operational evidence visible in Railway logs.
    print("BETTING_LITE_RUN kind=" + kind + " status=" + status +
          " credits=" + str(int(cost)) + " info=" +
          sanitize_sensitive_text(str(info)[:250]), flush=True)


def latest(kind):
    return db.fetchone("SELECT at,status,credits,info FROM lite_runs WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,))


def is_due(kind, hours, when):
    row = latest(kind)
    last = stamp(row["at"]) if row else None
    return not last or when-last >= timedelta(hours=hours)


def used_today(when):
    row = db.fetchone("SELECT COALESCE(SUM(credits),0) AS n FROM lite_runs WHERE at>=? AND at<?",
                      (when.date().isoformat(), (when.date()+timedelta(days=1)).isoformat()))
    return int(row["n"] if row else 0)


def verified_books(event, when):
    home,away = event.get("home_team"),event.get("away_team")
    if not home or not away or normalize(home)==normalize(away):
        return {}
    found = {}
    for book in event.get("bookmakers") or []:
        key = str(book.get("key") or "")
        updated = stamp(book.get("last_update"))
        if not key or (updated and when-updated>timedelta(minutes=90)):
            continue
        for market in book.get("markets") or []:
            if market.get("key") != "h2h":
                continue
            prices={}
            for outcome in market.get("outcomes") or []:
                side=outcome.get("name")
                try:
                    price=float(outcome.get("price"))
                except (ValueError,TypeError):
                    continue
                if side in (home,away) and 1.0<price<100:
                    prices[side]=price
            if set(prices)=={home,away}:
                found[key]=(str(book.get("title") or key),prices)
                break
    return found


def propose(event, books):
    """Leave-one-book-out no-vig price comparison: an unproven model."""
    home,away=event["home_team"],event["away_team"]
    opportunities=[]
    for key,(title,quotes) in books.items():
        if key not in BOOKS:
            continue
        references=[]
        for other,(_,q) in books.items():
            if key==other:
                continue
            denom=1/q[home]+1/q[away]
            references.append({home:(1/q[home])/denom,away:(1/q[away])/denom})
        if len(references)<MIN_REFERENCES:
            continue
        commission=0.02 if key in EXCHANGES else 0.0
        for side in (home,away):
            price=quotes[side]
            if not any(lo<=price<hi for lo,hi in BANDS):
                continue
            probability=mean(r[side] for r in references)
            net_return=1+(price-1)*(1-commission)
            edge=probability*net_return-1
            if edge>=EDGE_FLOOR:
                opportunities.append({
                    "selection":side, "bookmaker_key":key, "bookmaker_title":title,
                    "price":price, "probability":probability,
                    "edge":100*edge, "ref_count":len(references),
                    "commission":commission,
                })
    if not opportunities:
        return None
    return sorted(opportunities,key=lambda c:(-c["edge"],-c["ref_count"],c["bookmaker_key"]))[0]


def poll_odds(when):
    if used_today(when)>=MAX_CREDITS:
        record("odds",when,"QUOTA_BLOCKED",0,"Hard daily cap reached")
        return
    cost=1
    if not api.api_key:
        record("odds",when,"CONFIG_ERROR",0,"Missing ODDS_API_KEY")
        return
    try:
        resp=api.sport_odds(SPORT,"uk",("h2h",))
        cost=max(0,int(resp.last_cost)) if resp.last_cost is not None else 1
        candidates=0
        saved=0
        for event in resp.data if isinstance(resp.data,list) else []:
            eid=str(event.get("id") or "")
            start=stamp(event.get("commence_time"))
            home,away=str(event.get("home_team") or ""),str(event.get("away_team") or "")
            if not eid or not start or not home or not away:
                continue
            if not when+timedelta(minutes=20)<=start<=when+timedelta(days=10):
                continue
            books=verified_books(event,when)
            candidates+=1
            for bk,(_,prices) in books.items():
                for side,price in prices.items():
                    db.execute("""INSERT INTO lite_quotes(event_id,captured_at,bookmaker_key,selection,price,commence_time)
                        VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                        (eid,when.isoformat(),bk,side,price,start.isoformat()))
            if db.fetchone("SELECT event_id FROM lite_picks WHERE event_id=?",(eid,)):
                continue
            proposal=propose(event,books)
            if not proposal:
                continue
            db.execute("""INSERT INTO lite_picks(
                event_id,observed_at,commence_time,home_team,away_team,selection,
                bookmaker_key,bookmaker_title,entry_odds,reference_probability,
                estimated_edge_pct,reference_books,commission_assumption)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO NOTHING""",
                (eid,when.isoformat(),start.isoformat(),home,away,proposal["selection"],
                 proposal["bookmaker_key"],proposal["bookmaker_title"],proposal["price"],
                 proposal["probability"],proposal["edge"],proposal["ref_count"],proposal["commission"]))
            saved+=1
        record("odds",when,"OK",cost,f"Events={candidates}, fresh singles={saved}")
    except Exception as exc:
        record("odds",when,"ERROR",cost,sanitize_sensitive_text(exc))


def poll_results(when):
    pending=db.fetchall("""SELECT * FROM lite_picks WHERE result IS NULL
        AND commence_time<? AND commence_time>=? ORDER BY commence_time LIMIT 75""",
        ((when-timedelta(hours=2)).isoformat(),(when-timedelta(days=3)).isoformat()))
    if not pending:
        record("results",when,"NO_DUE",0,"No completed ungraded fixtures in three-day window")
        return
    if used_today(when)>=MAX_CREDITS:
        record("results",when,"QUOTA_BLOCKED",0,"Hard daily cap reached")
        return
    if not api.api_key:
        record("results",when,"CONFIG_ERROR",0,"Missing ODDS_API_KEY")
        return
    cost=1
    try:
        resp=api.scores(SPORT,event_ids=[p["event_id"] for p in pending],days_from=3)
        cost=max(0,int(resp.last_cost)) if resp.last_cost is not None else 1
        pending_by_id={p["event_id"]:p for p in pending}
        resolved=0
        for item in resp.data if isinstance(resp.data,list) else []:
            eid=str(item.get("id") or "")
            if eid not in pending_by_id or not item.get("completed"):
                continue
            pick=pending_by_id[eid]
            scores={}
            for score in item.get("scores") or []:
                try:
                    scores[normalize(score.get("name"))]=int(score.get("score"))
                except (TypeError,ValueError):
                    continue
            h,a=normalize(pick["home_team"]),normalize(pick["away_team"])
            if h not in scores or a not in scores:
                continue
            hs,aw=scores[h],scores[a]
            if hs==aw:
                result,pnl="VOID",0.0
            else:
                winner=pick["home_team"] if hs>aw else pick["away_team"]
                result="WIN" if normalize(winner)==normalize(pick["selection"]) else "LOSS"
                pnl=(float(pick["entry_odds"])-1)*(1-float(pick["commission_assumption"])) if result=="WIN" else -1
            db.execute("""UPDATE lite_picks SET result=?,home_score=?,away_score=?,pnl_units=?,settled_at=?
                WHERE event_id=? AND result IS NULL""",
                (result,hs,aw,pnl,when.isoformat(),eid))
            resolved+=1
        record("results",when,"OK",cost,f"Resolved={resolved}/{len(pending)}")
    except Exception as exc:
        record("results",when,"ERROR",cost,sanitize_sensitive_text(exc))


def cycle():
    when=now_utc()
    try:
        if is_due("results",RESULT_HOURS,when):
            poll_results(when)
        if is_due("odds",ODDS_HOURS,when):
            poll_odds(when)
    except Exception as exc:
        print("LITE_CYCLE_ERROR",sanitize_sensitive_text(exc),flush=True)


async def schedule():
    while True:
        await asyncio.to_thread(cycle)
        await asyncio.sleep(600)


@asynccontextmanager
async def lifespan(app):
    schema()
    task=asyncio.create_task(schedule())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app=FastAPI(title="Betting Lab Lite",lifespan=lifespan)


def summary():
    all_rows=db.fetchall("SELECT * FROM lite_picks ORDER BY observed_at DESC")
    settled=[p for p in all_rows if p["result"] in ("WIN","LOSS","VOID")]
    pnl=sum(float(x["pnl_units"] or 0) for x in settled)
    groups=[]
    for lo,hi in BANDS:
        rows=[p for p in settled if lo<=float(p["entry_odds"])<hi]
        gpnl=sum(float(p["pnl_units"] or 0) for p in rows)
        groups.append({"band":f"{lo:.2f}-{hi:.2f}","settled":len(rows),
                       "wins":sum(p["result"]=="WIN" for p in rows),
                       "pnl":round(gpnl,2),
                       "roi":round(100*gpnl/len(rows),2) if rows else None})
    last50=settled[:50]
    return {
        "mode":"LITE","research_only":True,"live_execution":False,
        "sport":SPORT,"bands":groups,"observed":len(all_rows),
        "settled":len(settled),"pnl_units":round(pnl,2),
        "roi":round(100*pnl/len(settled),2) if settled else None,
        "rolling_50_roi":round(100*sum(float(p["pnl_units"] or 0) for p in last50)/len(last50),2) if last50 else None,
        "used_credits_today":used_today(now_utc()),"daily_credit_cap":MAX_CREDITS,
        "odds":latest("odds"),"results":latest("results"),
        "disclosure":"Independent-book no-vig consensus is an unvalidated price estimate; prices are observed, not guaranteed."
    }


@app.get("/health")
def health():
    try:
        db.fetchone("SELECT 1 AS ok")
        return {"ok":True,"mode":"lite"}
    except Exception:
        raise HTTPException(status_code=503,detail="Database unavailable")


@app.get("/api/status")
def api_status():
    return JSONResponse(summary())


@app.get("/api/selections")
def selections():
    return {"rows":db.fetchall("SELECT * FROM lite_picks ORDER BY observed_at DESC LIMIT 200")}


@app.get("/export/lite.csv")
def export_csv():
    cols=("event_id","observed_at","commence_time","home_team","away_team",
          "selection","bookmaker_key","bookmaker_title","entry_odds",
          "reference_probability","estimated_edge_pct","reference_books",
          "commission_assumption","result","home_score","away_score",
          "pnl_units","settled_at")
    io=StringIO()
    out=csv.DictWriter(io,fieldnames=cols,extrasaction="ignore")
    out.writeheader()
    for pick in db.fetchall("SELECT * FROM lite_picks ORDER BY observed_at"):
        out.writerow(pick)
    return Response(io.getvalue(),media_type="text/csv",
                    headers={"Content-Disposition":'attachment; filename="betting-lab-lite.csv"'})


@app.get("/",response_class=HTMLResponse)
def home():
    s=summary()
    picks=db.fetchall("SELECT * FROM lite_picks ORDER BY observed_at DESC LIMIT 50")
    def e(value):
        return escape(str(value)) if value is not None else "—"
    def pct(value):
        return "—" if value is None else f"{float(value):+.1f}%"
    body=[]
    for p in picks:
        pnl=e(round(float(p["pnl_units"]),2)) if p["pnl_units"] is not None else "—"
        body.append("<tr><td>"+e(p["home_team"])+" v "+e(p["away_team"])+
                    "</td><td>"+e(p["selection"])+"</td><td>"+e(p["bookmaker_title"])+
                    "</td><td>"+f"{float(p['entry_odds']):.2f}"+
                    "</td><td>"+pct(p["estimated_edge_pct"])+
                    "</td><td>"+e(p["result"] or "OPEN")+"</td><td>"+pnl+"</td></tr>")
    band_html="".join("<tr><td>"+e(b["band"])+"</td><td>"+str(b["settled"])+
                      "</td><td>"+str(b["wins"])+"</td><td>"+pct(b["roi"])+
                      "</td></tr>" for b in s["bands"])
    last=s["odds"] or {}
    html="""<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Betting Lab Lite</title>
<style>
:root{color-scheme:dark}body{background:#10151d;color:#eaf1fa;font:15px system-ui,sans-serif;margin:0}
main{max-width:1100px;margin:auto;padding:28px 18px}h1{font-size:32px;margin:6px 0 12px}
h2{font-size:19px}p,small{color:#aebbd0}a{color:#84beff}.label{font-size:12px;color:#a9bbd0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.tile,.panel{border:1px solid #2f3c50;background:#19212d;padding:17px;border-radius:12px}
.panel{margin-top:16px}.big{font-size:28px;font-weight:750;margin-top:7px}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:620px}
td,th{text-align:left;padding:11px;border-bottom:1px solid #2d3b4c}
th{color:#c2d0e2;font-size:12px}
</style></head><body><main>
<div class='label'>RESEARCH ONLY · NO LIVE BETTING · LITE MODE</div>
<h1>Betting Lab Lite</h1>
<p>EuroLeague H2H only. 1.50–1.99 and 3.00–3.99 singles.
No legacy collectors, HR2, multiples, Telegram or execution bots.</p>
<div class='grid'>
<div class='tile'><div class='label'>Settled singles</div><div class='big'>"""+str(s["settled"])+"""</div></div>
<div class='tile'><div class='label'>Net ROI (hypothetical)</div><div class='big'>"""+pct(s["roi"])+"""</div></div>
<div class='tile'><div class='label'>Profit, 1u singles</div><div class='big'>"""+f"{s['pnl_units']:+.2f}u"+"""</div></div>
<div class='tile'><div class='label'>Daily API credits</div><div class='big'>"""+str(s["used_credits_today"])+"/"+str(MAX_CREDITS)+"""</div></div>
</div>
<div class='panel'><h2>Evidence by odds band</h2><div class='scroll'><table>
<thead><tr><th>Band</th><th>Settled</th><th>Wins</th><th>ROI</th></tr></thead>
<tbody>"""+band_html+"""</tbody></table></div>
<small>Fresh forward sample only. No historical optimisation or backfill.</small></div>
<div class='panel'><h2>Current and settled selections</h2><div class='scroll'><table>
<thead><tr><th>Fixture</th><th>Pick</th><th>Bookmaker</th><th>Odds</th><th>Est. edge</th><th>Result</th><th>P&amp;L</th></tr></thead>
<tbody>"""+("".join(body) or "<tr><td colspan='7'>No qualifying new selections yet.</td></tr>")+"""</tbody></table></div></div>
<div class='panel'><h2>Operations and controls</h2><p>Latest odds scan: """+e(last.get("at"))+" · "+e(last.get("status"))+""".
Odds every """+str(ODDS_HOURS)+""" hours; results every """+str(RESULT_HOURS)+""" hours if due.
API credit ceiling """+str(MAX_CREDITS)+""" credits/day.
One frozen selection per fixture; at least two independent reference bookmakers.</p>
<p>Estimated edges rely on an <strong>unvalidated market-consensus proxy</strong>, not a calibrated
model. Available prices and exchange commission must be verified independently.</p>
<p><a href='/export/lite.csv'>Download Lite CSV</a> · <a href='/api/status'>Status API</a></p>
<small>Historic research remains untouched in the existing database; legacy functionality
is stopped, not deleted.</small></div></main></body></html>"""
    return HTMLResponse(html)
