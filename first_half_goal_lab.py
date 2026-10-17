from __future__ import annotations

"""FHG1: forward-only first-half-goal research lane. No betting authority."""
import csv, inspect, json, math, zipfile
from datetime import datetime, timedelta, timezone
from html import escape
from io import BytesIO, StringIO
from zoneinfo import ZoneInfo
import requests
from fastapi.responses import HTMLResponse
from db import sanitize_sensitive_text, utc_now_iso
from predictive_football import (FOOTBALL_DATA_BASE_URL,_fetch_football_data_csv,_normalize_text,_parse_football_data_date,_season_code)

MODEL_VERSION="FHG1_SPLIT_BETA_FORWARD_V1"
THRESHOLD=.70
PRIOR_GAMES=6.0
LOOKBACK_DAYS=730
SPLIT_N=12
HORIZON_HOURS=96
LONDON=ZoneInfo("Europe/London")
LEAGUES={"soccer_epl":"E0","soccer_efl_champ":"E1","soccer_england_league1":"E2","soccer_england_league2":"E3","soccer_spl":"SC0","soccer_scotland_championship":"SC1","soccer_scotland_league_one":"SC2","soccer_scotland_league_two":"SC3"}
TABLES=("first_half_goal_source_state","first_half_goal_history","first_half_goal_candidates","first_half_goal_cards")

def _dt(v):
    try:
        x=datetime.fromisoformat(str(v).replace("Z","+00:00")); return (x if x.tzinfo else x.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except Exception:return None

def _tier(p):
    return "A" if p>=.80 else "B" if p>=.75 else "C" if p>=.70 else "WATCH"

def _slate(v):
    x=_dt(v); return x.astimezone(LONDON).strftime("%Y-%m-%dT%H:%M") if x else "UNKNOWN"

def ensure_schema(db):
    iid="BIGSERIAL PRIMARY KEY" if db.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    for sql in (
      """CREATE TABLE IF NOT EXISTS first_half_goal_source_state(source_key TEXT PRIMARY KEY,sport_key TEXT NOT NULL,season_code TEXT NOT NULL,last_attempt_at TEXT,last_success_at TEXT,rows_seen INTEGER NOT NULL DEFAULT 0,last_error TEXT)""",
      f"""CREATE TABLE IF NOT EXISTS first_half_goal_history(id {iid},match_key TEXT NOT NULL UNIQUE,sport_key TEXT NOT NULL,league_code TEXT NOT NULL,played_at TEXT NOT NULL,home_team TEXT NOT NULL,away_team TEXT NOT NULL,home_key TEXT NOT NULL,away_key TEXT NOT NULL,ht_home INTEGER NOT NULL,ht_away INTEGER NOT NULL,fhg_hit INTEGER NOT NULL,source_ref TEXT NOT NULL,imported_at TEXT NOT NULL)""",
      f"""CREATE TABLE IF NOT EXISTS first_half_goal_candidates(id {iid},event_id TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,sport_key TEXT NOT NULL,league TEXT NOT NULL,commence_time TEXT NOT NULL,slate_key TEXT NOT NULL,home_team TEXT NOT NULL,away_team TEXT NOT NULL,home_key TEXT NOT NULL,away_key TEXT NOT NULL,model_version TEXT NOT NULL,league_n INTEGER NOT NULL,league_hits INTEGER NOT NULL,league_rate REAL NOT NULL,home_n INTEGER NOT NULL,home_hits INTEGER NOT NULL,home_rate REAL NOT NULL,away_n INTEGER NOT NULL,away_hits INTEGER NOT NULL,away_rate REAL NOT NULL,estimated_probability REAL NOT NULL,tier TEXT NOT NULL,sample_quality TEXT NOT NULL,pred1_total_xg REAL,pred2_total_xg REAL,pred3_total_xg REAL,pred4_total_xg REAL,evidence_mode TEXT NOT NULL DEFAULT 'FORWARD',status TEXT NOT NULL DEFAULT 'OPEN',ht_home INTEGER,ht_away INTEGER,fhg_hit INTEGER,settled_at TEXT)""",
      f"""CREATE TABLE IF NOT EXISTS first_half_goal_cards(id {iid},slate_key TEXT NOT NULL,card_size INTEGER NOT NULL,model_version TEXT NOT NULL,created_at TEXT NOT NULL,kickoff_time TEXT NOT NULL,event_ids_json TEXT NOT NULL,minimum_probability REAL NOT NULL,status TEXT NOT NULL DEFAULT 'OPEN',settled_legs INTEGER NOT NULL DEFAULT 0,hit_legs INTEGER NOT NULL DEFAULT 0,all_hit INTEGER,settled_at TEXT,UNIQUE(slate_key,card_size,model_version))""",
    ): db.execute(sql)

def _active(db):
    try:
        rows=db.fetchall("SELECT DISTINCT sport_key FROM events WHERE sport_key LIKE ?",("soccer_%",)); keys=[str(r["sport_key"]) for r in rows if str(r.get("sport_key") or "") in LEAGUES]
        return keys or list(LEAGUES)[:5]
    except Exception:return list(LEAGUES)[:5]

def _source_due(db,key,now,hours):
    r=db.fetchone("SELECT last_attempt_at FROM first_half_goal_source_state WHERE source_key=?",(key,)); last=_dt((r or {}).get("last_attempt_at")); return not last or (now-last).total_seconds()>=hours*3600

def _source_state(db,key,sport,season,now,ok,n=0,error=""):
    db.execute("""INSERT INTO first_half_goal_source_state(source_key,sport_key,season_code,last_attempt_at,last_success_at,rows_seen,last_error) VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET sport_key=excluded.sport_key,season_code=excluded.season_code,last_attempt_at=excluded.last_attempt_at,last_success_at=CASE WHEN excluded.last_success_at IS NOT NULL THEN excluded.last_success_at ELSE first_half_goal_source_state.last_success_at END,rows_seen=excluded.rows_seen,last_error=excluded.last_error""",(key,sport,season,now.isoformat(),now.isoformat() if ok else None,n,None if ok else sanitize_sensitive_text(error)[:800]))

def _import_csv(db,sport,code,season,text):
    n=0
    for r in csv.DictReader(StringIO(text)):
        d=_parse_football_data_date(r.get("Date","")); home=str(r.get("HomeTeam") or "").strip(); away=str(r.get("AwayTeam") or "").strip()
        if not d or not home or not away:continue
        try: hh=int(float(r.get("HTHG"))); ha=int(float(r.get("HTAG")))
        except Exception:continue
        raw="|".join((sport,d.date().isoformat(),_normalize_text(home),_normalize_text(away)))
        import hashlib; key=hashlib.sha256(raw.encode()).hexdigest(); hit=1 if hh+ha else 0
        db.execute("""INSERT INTO first_half_goal_history(match_key,sport_key,league_code,played_at,home_team,away_team,home_key,away_key,ht_home,ht_away,fhg_hit,source_ref,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(match_key) DO UPDATE SET ht_home=excluded.ht_home,ht_away=excluded.ht_away,fhg_hit=excluded.fhg_hit,source_ref=excluded.source_ref,imported_at=excluded.imported_at""",(key,sport,code,d.isoformat(),home,away,_normalize_text(home),_normalize_text(away),hh,ha,hit,f"{season}/{code}.csv",utc_now_iso())); n+=1
    return n

def sync_history(db,now=None):
    ensure_schema(db); now=now or datetime.now(timezone.utc); s=requests.Session(); out={"attempted":0,"ok":0,"failed":0,"rows_seen":0}
    plan=[]
    for sport in _active(db): plan.append((sport,LEAGUES[sport],_season_code(now,0),6)); plan.append((sport,LEAGUES[sport],_season_code(now,-1),720))
    for sport,code,season,hours in plan:
        key=f"FHG1:{sport}:{season}"
        if not _source_due(db,key,now,hours):continue
        out["attempted"]+=1; url=f"{FOOTBALL_DATA_BASE_URL}/{season}/{code}.csv"
        try:
            res=_fetch_football_data_csv(s,url,timeout=20,headers={"User-Agent":"Project-Exit-Plan-Betting-Lab/FHG1"}); text=res.content.decode("utf-8-sig",errors="replace"); n=_import_csv(db,sport,code,season,text); _source_state(db,key,sport,season,now,True,n); out["ok"]+=1; out["rows_seen"]+=n
        except Exception as e:_source_state(db,key,sport,season,now,False,error=str(e)); out["failed"]+=1
    return out

def _league_stats(db,sport,ko):
    r=db.fetchone("SELECT COUNT(*) n,COALESCE(SUM(fhg_hit),0) hits FROM first_half_goal_history WHERE sport_key=? AND played_at<? AND played_at>=?",(sport,ko.isoformat(),(ko-timedelta(days=LOOKBACK_DAYS)).isoformat())) or {}; n=int(r.get("n") or 0); h=int(r.get("hits") or 0); return n,h,(h/n if n else .70)

def _split(db,key,home,ko):
    col="home_key" if home else "away_key"; rows=db.fetchall(f"SELECT fhg_hit FROM first_half_goal_history WHERE {col}=? AND played_at<? AND played_at>=? ORDER BY played_at DESC,id DESC LIMIT ?",(key,ko.isoformat(),(ko-timedelta(days=LOOKBACK_DAYS)).isoformat(),SPLIT_N)); n=len(rows); h=sum(int(x.get("fhg_hit") or 0) for x in rows); return n,h,(h/n if n else 0.0)

def _xg(db,event_id):
    out=[]
    for table in ("football_predictive_predictions","football_predictive2_predictions","football_predictive3_predictions","football_predictive4_predictions"):
        try:r=db.fetchone(f"SELECT expected_home_goals,expected_away_goals FROM {table} WHERE event_id=? ORDER BY created_at DESC,id DESC LIMIT 1",(event_id,))
        except Exception:r=None
        try:out.append(float(r["expected_home_goals"])+float(r["expected_away_goals"]) if r else None)
        except Exception:out.append(None)
    return out

def create_candidates(db,now=None):
    ensure_schema(db); now=now or datetime.now(timezone.utc); end=now+timedelta(hours=HORIZON_HOURS); active=_active(db); ph=",".join("?" for _ in active); created=0
    rows=db.fetchall(f"SELECT event_id,sport_key,league,commence_time,home_team,away_team FROM events WHERE sport_key IN ({ph}) AND commence_time>? AND commence_time<=? AND status='UPCOMING' ORDER BY commence_time",tuple(active)+(now.isoformat(),end.isoformat()))
    for e in rows:
        if db.fetchone("SELECT id FROM first_half_goal_candidates WHERE event_id=?",(e["event_id"],)):continue
        ko=_dt(e["commence_time"]); sport=str(e["sport_key"]); ln,lh,lr=_league_stats(db,sport,ko)
        if ln<20:continue
        hk=_normalize_text(e["home_team"]); ak=_normalize_text(e["away_team"]); hn,hh,hr=_split(db,hk,True,ko); an,ah,ar=_split(db,ak,False,ko)
        hp=(hh+PRIOR_GAMES*lr)/(hn+PRIOR_GAMES); ap=(ah+PRIOR_GAMES*lr)/(an+PRIOR_GAMES); p=max(.01,min(.99,(hp+ap)/2)); quality="GOOD" if min(hn,an)>=5 else "THIN" if min(hn,an)>=2 else "LEAGUE_HEAVY"; xg=_xg(db,e["event_id"])
        db.execute("""INSERT INTO first_half_goal_candidates(event_id,created_at,sport_key,league,commence_time,slate_key,home_team,away_team,home_key,away_key,model_version,league_n,league_hits,league_rate,home_n,home_hits,home_rate,away_n,away_hits,away_rate,estimated_probability,tier,sample_quality,pred1_total_xg,pred2_total_xg,pred3_total_xg,pred4_total_xg,evidence_mode,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(e["event_id"],now.isoformat(),sport,str(e.get("league") or sport),ko.isoformat(),_slate(ko.isoformat()),e["home_team"],e["away_team"],hk,ak,MODEL_VERSION,ln,lh,lr,hn,hh,hr,an,ah,ar,p,_tier(p),quality,*xg,"FORWARD","OPEN")); created+=1
    return created

def settle_candidates(db,now=None):
    now=now or datetime.now(timezone.utc); settled=0
    for c in db.fetchall("SELECT * FROM first_half_goal_candidates WHERE status='OPEN' AND commence_time<?",((now-timedelta(hours=2)).isoformat(),)):
        ko=_dt(c["commence_time"]); day=ko.astimezone(LONDON).date(); start=datetime(day.year,day.month,day.day,tzinfo=timezone.utc); end=start+timedelta(days=1)
        rows=db.fetchall("SELECT * FROM first_half_goal_history WHERE sport_key=? AND played_at>=? AND played_at<?",(c["sport_key"],start.isoformat(),end.isoformat())); m=next((r for r in rows if r["home_key"]==c["home_key"] and r["away_key"]==c["away_key"]),None)
        if not m:continue
        db.execute("UPDATE first_half_goal_candidates SET status='SETTLED',ht_home=?,ht_away=?,fhg_hit=?,settled_at=? WHERE id=?",(m["ht_home"],m["ht_away"],m["fhg_hit"],now.isoformat(),c["id"])); settled+=1
    return settled

def create_cards(db,now=None):
    now=now or datetime.now(timezone.utc); created=0
    slates=db.fetchall("SELECT DISTINCT slate_key FROM first_half_goal_candidates WHERE commence_time>? AND estimated_probability>=?",(now.isoformat(),THRESHOLD))
    for s in slates:
        cs=db.fetchall("SELECT * FROM first_half_goal_candidates WHERE slate_key=? AND commence_time>? AND estimated_probability>=? ORDER BY estimated_probability DESC,event_id",(s["slate_key"],now.isoformat(),THRESHOLD))
        for size in (8,9,10):
            if len(cs)<size or db.fetchone("SELECT id FROM first_half_goal_cards WHERE slate_key=? AND card_size=? AND model_version=?",(s["slate_key"],size,MODEL_VERSION)):continue
            pick=cs[:size]; db.execute("INSERT INTO first_half_goal_cards(slate_key,card_size,model_version,created_at,kickoff_time,event_ids_json,minimum_probability,status) VALUES(?,?,?,?,?,?,?,?)",(s["slate_key"],size,MODEL_VERSION,now.isoformat(),pick[0]["commence_time"],json.dumps([x["event_id"] for x in pick]),min(float(x["estimated_probability"]) for x in pick),"OPEN")); created+=1
    return created

def settle_cards(db,now=None):
    now=now or datetime.now(timezone.utc); n=0
    for card in db.fetchall("SELECT * FROM first_half_goal_cards WHERE status='OPEN'"):
        ids=json.loads(card["event_ids_json"] or "[]"); rows=[]
        for eid in ids:
            r=db.fetchone("SELECT status,fhg_hit FROM first_half_goal_candidates WHERE event_id=?",(eid,)); rows.append(r)
        if not rows or any(not r or r["status"]!="SETTLED" for r in rows):continue
        hits=sum(int(r.get("fhg_hit") or 0) for r in rows); db.execute("UPDATE first_half_goal_cards SET status='SETTLED',settled_legs=?,hit_legs=?,all_hit=?,settled_at=? WHERE id=?",(len(rows),hits,1 if hits==len(rows) else 0,now.isoformat(),card["id"])); n+=1
    return n

def run_maintenance(db,now=None):
    ensure_schema(db); now=now or datetime.now(timezone.utc); hist=sync_history(db,now); c=create_candidates(db,now); s=settle_candidates(db,now); cc=create_cards(db,now); cs=settle_cards(db,now); return {"model":MODEL_VERSION,"history":hist,"candidates_created":c,"candidates_settled":s,"cards_created":cc,"cards_settled":cs,"provider_credits":0,"betting_authority":False}

def _metrics(rows):
    st=[r for r in rows if r.get("status")=="SETTLED" and r.get("fhg_hit") is not None]; hits=sum(int(r["fhg_hit"]) for r in st); avg=sum(float(r["estimated_probability"]) for r in st)/len(st) if st else None; actual=hits/len(st) if st else None
    return {"candidates":len(rows),"settled":len(st),"hits":hits,"hit_rate_pct":actual*100 if actual is not None else None,"avg_estimated_hit_pct":avg*100 if avg is not None else None,"calibration_gap_pct_points":(actual-avg)*100 if actual is not None and avg is not None else None}

def scoreboard(db):
    ensure_schema(db); rows=db.fetchall("SELECT * FROM first_half_goal_candidates ORDER BY id"); q=[r for r in rows if float(r["estimated_probability"])>=THRESHOLD]; h=db.fetchone("SELECT COUNT(*) n,COALESCE(SUM(fhg_hit),0) hits FROM first_half_goal_history") or {}; cards={}
    for size in (8,9,10):
        cr=db.fetchall("SELECT * FROM first_half_goal_cards WHERE card_size=?",(size,)); st=[r for r in cr if r["status"]=="SETTLED"]; wins=sum(int(r.get("all_hit") or 0) for r in st); cards[str(size)]={"created":len(cr),"settled":len(st),"all_hit":wins,"card_hit_rate_pct":wins/len(st)*100 if st else None}
    hn=int(h.get("n") or 0); return {"model_version":MODEL_VERSION,"research_only":True,"price_capture":False,"ev_claim":False,"qualified_threshold_pct":70.0,"historical_half_time_matches":hn,"historical_first_half_goal_rate_pct":int(h.get("hits") or 0)/hn*100 if hn else None,"all_forward":_metrics(rows),"qualified_forward":_metrics(q),"tiers":{t:_metrics([r for r in rows if r["tier"]==t]) for t in ("A","B","C","WATCH")},"cards":cards}

def _fmt(v):
    return "—" if v is None else f"{float(v):.1f}%"

def _dashboard(db,style):
    sc=scoreboard(db); q=sc["qualified_forward"]; rows=db.fetchall("SELECT * FROM first_half_goal_candidates ORDER BY CASE WHEN status='OPEN' THEN 0 ELSE 1 END,commence_time ASC LIMIT 120"); trs=[]
    for r in rows:
        dt=_dt(r["commence_time"]); k=dt.astimezone(LONDON).strftime("%a %d %b %H:%M") if dt else "—"; res="—" if r.get("fhg_hit") is None else "HIT" if int(r["fhg_hit"]) else "MISS"; xgs=[r.get(f"pred{i}_total_xg") for i in range(1,5)]; xgt=" · ".join(f"P{i+1} {float(v):.2f}" for i,v in enumerate(xgs) if v is not None) or "—"; trs.append(f"<tr><td>{k}</td><td><b>{escape(str(r['home_team']))} v {escape(str(r['away_team']))}</b><br><span class='muted'>{escape(str(r['league']))}</span></td><td>{float(r['estimated_probability'])*100:.1f}% {escape(str(r['tier']))}</td><td>{int(r['home_hits'])}/{int(r['home_n'])}</td><td>{int(r['away_hits'])}/{int(r['away_n'])}</td><td>{escape(xgt)}</td><td>{escape(str(r['sample_quality']))}</td><td>{res}</td></tr>")
    if not trs:trs=["<tr><td colspan='8'>No forward FHG1 candidates yet.</td></tr>"]
    crows=[]
    for r in db.fetchall("SELECT * FROM first_half_goal_cards ORDER BY kickoff_time DESC,card_size LIMIT 40"):
        res="OPEN" if r["status"]!="SETTLED" else "WON" if int(r.get("all_hit") or 0) else "LOST"; crows.append(f"<tr><td>{escape(str(r['slate_key']))}</td><td>{r['card_size']}</td><td>{float(r['minimum_probability'])*100:.1f}%</td><td>{r['hit_legs']}/{r['settled_legs']}</td><td>{res}</td></tr>")
    if not crows:crows=["<tr><td colspan='5'>No qualified 8/9/10-leg slates yet.</td></tr>"]
    cards=sc["cards"]
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>First-Half Goal Lab</title><style>{style}.fhg{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}.box{{padding:12px;border:1px solid #444;border-radius:10px}}.table-wrap{{overflow-x:auto}}table{{width:100%;border-collapse:collapse}}th,td{{padding:8px;border-bottom:1px solid #333;text-align:left;white-space:nowrap}}</style></head><body><main><div class='nav'><a href='/'>Home</a><a href='/research-progress'>Research Progress</a></div><h1>First-Half Goal Lab</h1><div class='panel'><b>Forward-only FHG1.</b> Score uses pre-kickoff first-half history only. PRED1–PRED4 full-match xG is captured for comparison, not used in v1. No first-half prices are captured, so hit rate is not an EV/profit claim.</div><div class='fhg'><div class='box'>History<br><b>{sc['historical_half_time_matches']}</b><br>{_fmt(sc['historical_first_half_goal_rate_pct'])}</div><div class='box'>Qualified ≥70%<br><b>{q['candidates']}</b><br>{_fmt(q['hit_rate_pct'])} hit</div><div class='box'>Calibration<br><b>{_fmt(q['avg_estimated_hit_pct'])}</b><br>gap {_fmt(q['calibration_gap_pct_points'])}</div><div class='box'>8-folds<br><b>{cards['8']['all_hit']}/{cards['8']['settled']}</b></div><div class='box'>9-folds<br><b>{cards['9']['all_hit']}/{cards['9']['settled']}</b></div><div class='box'>10-folds<br><b>{cards['10']['all_hit']}/{cards['10']['settled']}</b></div></div><div class='panel'><h2>Forward candidates</h2><div class='table-wrap'><table><thead><tr><th>Kickoff</th><th>Match</th><th>FHG1</th><th>Home split</th><th>Away split</th><th>xG comparison</th><th>Sample</th><th>Result</th></tr></thead><tbody>{''.join(trs)}</tbody></table></div></div><div class='panel'><h2>Qualified slate cards</h2><p class='muted'>Exact same-kickoff slates only; top 8/9/10 candidates frozen before kickoff.</p><table><thead><tr><th>Slate</th><th>Legs</th><th>Weakest</th><th>Hits</th><th>Result</th></tr></thead><tbody>{''.join(crows)}</tbody></table></div></main></body></html>"""

def _augment(blob,db,settings):
    import exporter as ex
    files={}
    with zipfile.ZipFile(BytesIO(blob),"r") as z:
        for i in z.infolist():files[i.filename]=z.read(i.filename)
    sec=tuple(x for x in (str(getattr(settings,"odds_api_key","") or ""),str(getattr(settings,"proxy_xg_api_football_key","") or "")) if x); man=json.loads(files.get("manifest.json",b"{}").decode()); summ=json.loads(files.get("analysis_summary.json",b"{}").decode()); counts=dict(man.get("row_counts") or {})
    for t in TABLES:
        try:r=db.fetchall(f"SELECT * FROM {t}")
        except Exception:r=[]
        files[f"tables/{t}.csv"]=ex._rows_to_csv(r,secrets=sec).encode(); counts[t]=len(r)
    man["row_counts"]=counts; man["first_half_goal_lab_tables"]=list(TABLES); summ["first_half_goal_lab"]=scoreboard(db); files["manifest.json"]=json.dumps(ex._json_safe(man),indent=2).encode(); files["analysis_summary.json"]=json.dumps(ex._json_safe(summ),indent=2).encode(); out=BytesIO()
    with zipfile.ZipFile(out,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for n,d in files.items():z.writestr(n,d)
    return out.getvalue()

def install(app,db,base_style):
    if getattr(app.state,"fhg1_installed",False):return {"installed":True,"already":True}
    ensure_schema(db)
    import worker as wm, web as web
    if not getattr(wm.Worker,"_fhg1",False):
        original=wm.Worker.one_cycle
        def wrapped(self,*a,**k):
            result=original(self,*a,**k)
            try:p=run_maintenance(self.db); self.db.record_collector_run("FIRST_HALF_GOAL_MAINT",True,detail=json.dumps(p,default=str)[:5000])
            except Exception as e:self.db.record_collector_run("FIRST_HALF_GOAL_MAINT",False,detail=sanitize_sensitive_text(e)[:1000])
            return result
        wm.Worker.one_cycle=wrapped; wm.Worker._fhg1=True
    if not getattr(web,"_fhg1_exports",False):
        full,weekly=web.build_research_export,web.build_weekly_research_export
        def f(db_,st,v):b,n=full(db_,st,v); return _augment(b,db_,st),n
        def w(db_,st,v):b,n=weekly(db_,st,v); return _augment(b,db_,st),n
        web.build_research_export=f; web.build_weekly_research_export=w; web._fhg1_exports=True
    @app.get('/first-half-goals',response_class=HTMLResponse)
    def page():return HTMLResponse(_dashboard(db,base_style))
    @app.get('/api/first-half-goals/scoreboard')
    def api_score():return scoreboard(db)
    @app.get('/api/first-half-goals/candidates')
    def api_candidates(limit:int=100):return db.fetchall("SELECT * FROM first_half_goal_candidates ORDER BY commence_time DESC,id DESC LIMIT ?",(max(1,min(limit,500)),))
    route=next((r for r in list(app.router.routes) if getattr(r,'path',None)=='/' and 'GET' in (getattr(r,'methods',set()) or set())),None)
    if route:
        original=route.endpoint; name=route.name; schema=getattr(route,'include_in_schema',True); app.router.routes.remove(route)
        async def home():
            res=original(); res=await res if inspect.isawaitable(res) else res
            if not isinstance(res,HTMLResponse):return res
            html=res.body.decode(); link="<a href='/first-half-goals'>First-Half Goal Lab</a>"; html=html if "href='/first-half-goals'" in html else html.replace("<div class='nav'>","<div class='nav'>"+link,1); headers={k:v for k,v in res.headers.items() if k.lower()!='content-length'}; return HTMLResponse(html,status_code=res.status_code,headers=headers)
        app.add_api_route('/',home,methods=['GET'],response_class=HTMLResponse,name=name,include_in_schema=schema)
    app.state.fhg1_installed=True
    return {"installed":True,"model_version":MODEL_VERSION,"research_only":True,"provider_credits":0,"qualified_threshold_pct":70.0}
