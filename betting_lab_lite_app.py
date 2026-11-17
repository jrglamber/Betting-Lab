"""Lean EuroLeague-only Betting Lab. Research only: no execution or Telegram."""
from __future__ import annotations

import asyncio
import csv
import os
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from html import escape
from io import StringIO
from statistics import mean, median

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
PAID_ALLOWANCE_END = datetime(2026, 12, 15, tzinfo=timezone.utc)
# Paid cap is a safety ceiling, NOT a target to exhaust purchased credits.
PAID_CAP = max(12, min(2000, int(os.getenv("LITE_PAID_CREDIT_CAP", "1500"))))
FREE_CAP = max(1, min(12, int(os.getenv("LITE_FREE_CREDIT_CAP", "12"))))
ODDS_HOURS = max(4, int(os.getenv("LITE_ODDS_HOURS", "6")))
RESULT_HOURS = max(8, int(os.getenv("LITE_RESULTS_HOURS", "12")))
SQLITE_PATH = os.getenv("LITE_SQLITE_PATH", "/data/betting-lab-lite.sqlite")
LEGACY_PG = os.getenv("DATABASE_URL", "")
db = Database(database_url="", db_path=SQLITE_PATH)

def credit_cap(when):
    # Scheduled cutover on user's stated 15 December renewal date.
    return PAID_CAP if when < PAID_ALLOWANCE_END else FREE_CAP

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
    # A Railway persistent mount is mandatory: never silently run ephemeral
    # SQLite and lose collected picks at the next deployment.
    if not os.path.ismount(os.path.dirname(SQLITE_PATH)):
        raise RuntimeError("SQLite persistent volume at /data is not mounted")
    db.execute("""CREATE TABLE IF NOT EXISTS lite_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL,
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
        pnl_units DOUBLE PRECISION, settled_at TEXT,
        closing_odds DOUBLE PRECISION, closing_at TEXT,
        closing_clv_pct DOUBLE PRECISION)""")
    db.execute("""CREATE TABLE IF NOT EXISTS lite_quotes(
        event_id TEXT NOT NULL, captured_at TEXT NOT NULL,
        bookmaker_key TEXT NOT NULL, selection TEXT NOT NULL,
        price DOUBLE PRECISION NOT NULL, commence_time TEXT NOT NULL,
        PRIMARY KEY(event_id,captured_at,bookmaker_key,selection))""")
    db.execute("""CREATE TABLE IF NOT EXISTS lite_migration_status (
        id INTEGER PRIMARY KEY, migrated_at TEXT NOT NULL, run_rows INTEGER,
        pick_rows INTEGER, quote_rows INTEGER)""")
    migrate_legacy_lite_records()


def migrate_legacy_lite_records():
    """One-time, verified copy of *only* the new Lite tables from old Postgres.

    No legacy betting tables are migrated. Migration failure aborts startup;
    Postgres is not removed until this record and API health are verified.
    """
    done = db.fetchone("SELECT * FROM lite_migration_status WHERE id=1")
    if done:
        return
    if not LEGACY_PG.startswith(("postgres://", "postgresql://")):
        raise RuntimeError("Lite migration source unavailable: keep old database until migration complete")
    source = Database(database_url=LEGACY_PG)
    columns = {
        "lite_runs": (
            "id","at","kind","status","credits","info",
        ),
        "lite_picks": (
            "event_id","observed_at","commence_time","home_team","away_team",
            "selection","bookmaker_key","bookmaker_title","entry_odds",
            "reference_probability","estimated_edge_pct","reference_books",
            "commission_assumption","result","home_score","away_score",
            "pnl_units","settled_at",
        ),
        "lite_quotes": (
            "event_id","captured_at","bookmaker_key","selection","price",
            "commence_time",
        ),
    }
    copied = {}
    for table, fields in columns.items():
        rows = source.fetchall("SELECT " + ",".join(fields) + " FROM " + table)
        sql = ("INSERT OR IGNORE INTO " + table + "(" + ",".join(fields) + ") "
               "VALUES(" + ",".join("?" for _ in fields) + ")")
        if rows:
            db.executemany(sql, [tuple(item.get(field) for field in fields) for item in rows])
        count = db.fetchone("SELECT COUNT(*) AS n FROM " + table)["n"]
        if int(count) < len(rows):
            raise RuntimeError("Lite migration row count incomplete for " + table)
        copied[table] = int(count)
    db.execute("""INSERT INTO lite_migration_status(
        id,migrated_at,run_rows,pick_rows,quote_rows)
        VALUES(1,?,?,?,?)""",
        (now_utc().isoformat(),copied["lite_runs"],copied["lite_picks"],
         copied["lite_quotes"]))
    print("BETTING_LITE_SQLITE_MIGRATED "+str(copied),flush=True)


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
    if used_today(when)+1>credit_cap(when):
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
            if not when+timedelta(minutes=1)<=start<=when+timedelta(days=10):
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
        remaining = resp.remaining if resp.remaining is not None else "unknown"
        spent = resp.used if resp.used is not None else "unknown"
        record("odds",when,"OK",cost,
               f"Events={candidates}, fresh singles={saved}; provider_remaining={remaining}; provider_used={spent}")
    except Exception as exc:
        record("odds",when,"ERROR",cost,sanitize_sensitive_text(exc))


def poll_results(when):
    pending=db.fetchall("""SELECT * FROM lite_picks WHERE result IS NULL
        AND commence_time<? AND commence_time>=? ORDER BY commence_time LIMIT 75""",
        ((when-timedelta(hours=2)).isoformat(),(when-timedelta(days=3)).isoformat()))
    if not pending:
        record("results",when,"NO_DUE",0,"No completed ungraded fixtures in three-day window")
        return
    if used_today(when)+2>credit_cap(when):
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
        remaining = resp.remaining if resp.remaining is not None else "unknown"
        record("results",when,"OK",cost,
               f"Resolved={resolved}/{len(pending)}; provider_remaining={remaining}")
    except Exception as exc:
        record("results",when,"ERROR",cost,sanitize_sensitive_text(exc))


def odds_poll_hours(when):
    """High-frequency snapshots until Dec 15; free-tier throttle thereafter.

    Scan against every observed fixture, not only qualifying picks. No futile
    10-minute polling on days without imminent EuroLeague fixtures.
    """
    if when >= PAID_ALLOWANCE_END:
        return max(6,ODDS_HOURS)

    row = db.fetchone("""SELECT MIN(commence_time) AS next_game FROM lite_quotes
        WHERE commence_time>?""",(when.isoformat(),))
    next_start = stamp(row["next_game"]) if row and row["next_game"] else None
    if next_start:
        until=next_start-when
        if until <= timedelta(hours=3):
            return 10/60
        if until <= timedelta(hours=24):
            return 20/60
    return 1




def refresh_closing_prices(when):
    """Freeze actual same-book pre-tipoff price; never use an in-play quote."""
    due = db.fetchall("""SELECT event_id,commence_time,selection,bookmaker_key,entry_odds
        FROM lite_picks WHERE closing_odds IS NULL AND commence_time<=?
        AND commence_time>=?""",
        (when.isoformat(),(when-timedelta(days=4)).isoformat()))
    written = 0
    for pick in due:
        kickoff = stamp(pick["commence_time"])
        if kickoff is None:
            continue
        quote = db.fetchone("""SELECT price,captured_at FROM lite_quotes
            WHERE event_id=? AND selection=? AND bookmaker_key=?
              AND captured_at<=? AND captured_at>=?
            ORDER BY captured_at DESC LIMIT 1""",
            (pick["event_id"],pick["selection"],pick["bookmaker_key"],
             kickoff.isoformat(),(kickoff-timedelta(minutes=90)).isoformat()))
        if not quote or float(quote["price"])<=1:
            continue
        closing=float(quote["price"])
        pct=(float(pick["entry_odds"])/closing-1)*100
        db.execute("""UPDATE lite_picks SET closing_odds=?,closing_at=?,closing_clv_pct=?
            WHERE event_id=? AND closing_odds IS NULL""",
            (closing,quote["captured_at"],pct,pick["event_id"]))
        written+=1
    return written


def probe_provider_balance():
    """Read quota headers from provider's free /sports endpoint on startup."""
    when = now_utc()
    if not api.api_key:
        record("provider_balance",when,"CONFIG_ERROR",0,"Missing ODDS_API_KEY")
        return
    try:
        data=api.sports()
        remain=data.remaining if data.remaining is not None else "unknown"
        used=data.used if data.used is not None else "unknown"
        record("provider_balance",when,"OK",0,
               f"provider_remaining={remain}; provider_used={used}; verify renewal separately")
    except Exception as exc:
        record("provider_balance",when,"ERROR",0,sanitize_sensitive_text(exc))


def cycle():
    when=now_utc()
    try:
        refresh_closing_prices(when)
        if is_due("results",RESULT_HOURS,when):
            poll_results(when)
        if is_due("odds",odds_poll_hours(when),when):
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
    await asyncio.to_thread(probe_provider_balance)
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
    with_close = [p for p in settled if p.get("closing_clv_pct") is not None]
    return {
        "mode":"LITE","research_only":True,"live_execution":False,
        "sport":SPORT,"bands":groups,"observed":len(all_rows),
        "settled":len(settled),"pnl_units":round(pnl,2),
        "roi":round(100*pnl/len(settled),2) if settled else None,
        "rolling_50_roi":round(100*sum(float(p["pnl_units"] or 0) for p in last50)/len(last50),2) if last50 else None,
        "closing_line_sample":len(with_close),
        "median_clv_pct":round(median(float(p["closing_clv_pct"]) for p in with_close),3) if with_close else None,
        "beat_close_pct":round(100*sum(float(p["closing_clv_pct"])>0 for p in with_close)/len(with_close),1) if with_close else None,
        "used_credits_today":used_today(now_utc()),"daily_credit_cap":credit_cap(now_utc()),
        "paid_allowance_end_utc":PAID_ALLOWANCE_END.isoformat(),
        "odds_poll_minutes":round(odds_poll_hours(now_utc())*60),
        "storage":"persistent SQLite /data","migration":db.fetchone("SELECT * FROM lite_migration_status WHERE id=1"),
        "odds":latest("odds"),"results":latest("results"),
        "provider_balance":latest("provider_balance"),
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
          "pnl_units","settled_at","closing_odds","closing_at","closing_clv_pct")
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
<div class='tile'><div class='label'>Daily API credits</div><div class='big'>"""+str(s["used_credits_today"])+"/"+str(credit_cap(now_utc()))+"""</div></div>
</div>
<div class='panel'><h2>Evidence by odds band</h2><div class='scroll'><table>
<thead><tr><th>Band</th><th>Settled</th><th>Wins</th><th>ROI</th></tr></thead>
<tbody>"""+band_html+"""</tbody></table></div>
<small>Fresh forward sample only. No historical optimisation or backfill.</small></div>
<div class='panel'><h2>Current and settled selections</h2><div class='scroll'><table>
<thead><tr><th>Fixture</th><th>Pick</th><th>Bookmaker</th><th>Odds</th><th>Est. edge</th><th>Result</th><th>P&amp;L</th></tr></thead>
<tbody>"""+("".join(body) or "<tr><td colspan='7'>No qualifying new selections yet.</td></tr>")+"""</tbody></table></div></div>
<div class='panel'><h2>Closing-line evidence</h2>
<p>Compared at the same bookmaker, at most 90 minutes before tip-off.
Measured: """+str(s["closing_line_sample"])+""" settled selections.
Median CLV: """+pct(s["median_clv_pct"])+""".
Beat closing price: """+pct(s["beat_close_pct"])+""". Missing qualifying close quotes remain unscored.</p></div>
<div class='panel'><h2>Operations and controls</h2><p>Latest odds scan: """+e(last.get("at"))+" · "+e(last.get("status"))+""".
Odds every """+str(round(odds_poll_hours(now_utc())*60))+""" minutes under the current schedule; results every """+str(RESULT_HOURS)+""" hours if due.
API credit ceiling """+str(credit_cap(now_utc()))+""" credits/day.
One frozen selection per fixture; at least two independent reference bookmakers.</p>
<p>Estimated edges rely on an <strong>unvalidated market-consensus proxy</strong>, not a calibrated
model. Available prices and exchange commission must be verified independently.</p>
<p>Provider quota (last free probe): """+e((s["provider_balance"] or {}).get("info"))+"""</p>
<p><a href='/export/lite.csv'>Download Lite CSV</a> · <a href='/api/status'>Status API</a></p>
<small>New research is stored in persistent SQLite. The old Betting Lab Postgres service
and its legacy research history were deliberately retired.</small></div></main></body></html>"""
    return HTMLResponse(html)
