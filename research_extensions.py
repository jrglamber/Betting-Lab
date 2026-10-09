from __future__ import annotations

"""Research-only challengers and audits for Betting Lab.

No live-betting, staking, Telegram, XS1, or provider-call authority. PRED5 and
PRED6 are fixed ex-ante challengers built only from already-frozen PRED4 bets
and stored data. A persisted first-run timestamp keeps reconstructed BACKFILL
separate from genuine FORWARD evidence.
"""

from datetime import datetime, timedelta, timezone
from html import escape
import json, math, re, threading, time, unicodedata
from statistics import mean, median
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from db import utc_now_iso

VERSION="RESEARCH_EXTENSIONS_V1"
PRED5="PRED5_CROSS_MODEL_RESIDUAL_V1"
PRED6="PRED6_PXG_FORM_TRANSITION_V1"
EDGE=3.0
FORM_COEFF=0.10
RUN_EVERY=600
EXPORT_TABLES=("football_predictive_research_samples","football_predictive_research_audits","football_predictive_research_state")
_STARTED=False
_LOCK=threading.Lock()
EXCHANGES={"betfair_ex_uk","matchbook","smarkets"}
ALIASES={"man united":"manchester united","man utd":"manchester united","man city":"manchester city","nottm forest":"nottingham forest","wolves":"wolverhampton wanderers","spurs":"tottenham hotspur","inter":"inter milan","psg":"paris saint germain","caykur rizespor":"rizespor","wehen wiesbaden":"wehen"}


def now(): return datetime.now(timezone.utc)
def dt(v):
    try: return datetime.fromisoformat(str(v).replace("Z","+00:00")).astimezone(timezone.utc)
    except Exception: return None
def f(v):
    try:
        x=float(v); return x if math.isfinite(x) else None
    except Exception: return None
def norm(v):
    s=unicodedata.normalize("NFKD",str(v or "")); s="".join(c for c in s if not unicodedata.combining(c)).lower()
    s=re.sub(r"\b(fc|cf|afc|sc|ac|club|the)\b"," ",s); s=re.sub(r"[^a-z0-9]+"," ",s); s=re.sub(r"\s+"," ",s).strip()
    return ALIASES.get(s,s)
def point_eq(a,b):
    if a in (None,"") and b in (None,""): return True
    try: return abs(float(a)-float(b))<1e-9
    except Exception: return str(a or "")==str(b or "")
def avg(xs):
    ys=[float(x) for x in xs if f(x) is not None]; return mean(ys) if ys else None
def med(xs):
    ys=[float(x) for x in xs if f(x) is not None]; return median(ys) if ys else None
def fmt(v,d=2):
    try: return f"{float(v):.{d}f}"
    except Exception: return "—"
def pct(v): return "—" if v is None else fmt(v)+"%"
def tone(v):
    try: return "ok" if float(v)>=0 else "bad"
    except Exception: return ""


def ensure_schema(db):
    ident="BIGSERIAL PRIMARY KEY" if bool(getattr(db,"is_postgres",False)) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    for sql in (
        "CREATE TABLE IF NOT EXISTS football_predictive_research_state(state_key TEXT PRIMARY KEY,state_value TEXT NOT NULL,updated_at TEXT NOT NULL)",
        f"""CREATE TABLE IF NOT EXISTS football_predictive_research_samples(
          id {ident}, model_name TEXT NOT NULL, sample_key TEXT NOT NULL, evidence_mode TEXT NOT NULL,
          created_at TEXT NOT NULL, source_table TEXT NOT NULL, source_bet_id INTEGER NOT NULL,
          event_id TEXT NOT NULL, commence_time TEXT, market_key TEXT NOT NULL, selection TEXT NOT NULL,
          point REAL, bookmaker_key TEXT, offered_odds REAL, base_probability REAL, challenger_probability REAL,
          market_probability REAL, edge_pct REAL, bucket TEXT, decision TEXT NOT NULL, reason TEXT NOT NULL,
          features_json TEXT, status TEXT NOT NULL DEFAULT 'OPEN', result TEXT, pnl_units REAL,
          clv_pct REAL, clv_quality TEXT, brier_score REAL, settled_at TEXT, model_version TEXT,
          UNIQUE(model_name,sample_key))""",
        f"""CREATE TABLE IF NOT EXISTS football_predictive_research_audits(
          id {ident}, audit_key TEXT NOT NULL, captured_at TEXT NOT NULL, status TEXT NOT NULL, detail_json TEXT NOT NULL)""",
    ): db.execute(sql)


def state(db,key):
    r=db.fetchone("SELECT state_value FROM football_predictive_research_state WHERE state_key=?",(key,)); return r.get("state_value") if r else None
def set_state(db,key,value):
    db.execute("""INSERT INTO football_predictive_research_state(state_key,state_value,updated_at) VALUES(?,?,?)
                  ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at=excluded.updated_at""",(key,str(value),utc_now_iso()))
def forward_start(db):
    key="research_extensions_forward_start_at"; v=state(db,key)
    if not v: v=utc_now_iso(); set_state(db,key,v)
    return dt(v) or now()


def poisson(k,l): return math.exp(-l)*(l**k)/math.factorial(k)
def score_probs(h,a):
    hp=dp=ap=0.0
    for i in range(11):
        pi=poisson(i,h)
        for j in range(11):
            p=pi*poisson(j,a)
            if i>j: hp+=p
            elif i==j: dp+=p
            else: ap+=p
    z=hp+dp+ap; return (hp/z,dp/z,ap/z) if z else (1/3,1/3,1/3)
def prob_from_lambdas(market,sel,point,h,a,home,away):
    if market=="h2h":
        hp,dp,ap=score_probs(h,a); return hp if sel==home else ap if sel==away else dp if sel=="Draw" else None
    if market=="btts":
        yes=(1-math.exp(-h))*(1-math.exp(-a)); return yes if sel=="Yes" else 1-yes if sel=="No" else None
    if market=="totals" and f(point) is not None:
        lam=h+a; cutoff=math.floor(float(point)); under=sum(poisson(k,lam) for k in range(cutoff+1)); return 1-under if sel=="Over" else under if sel=="Under" else None
    return None


def candidates(db):
    out=[]
    for table,family in (("football_predictive4_bets","H2H"),("football_predictive4_market_bets","MARKET")):
        try: rows=db.fetchall(f"SELECT * FROM {table} ORDER BY id")
        except Exception: continue
        for r0 in rows:
            r=dict(r0); r["source_table"]=table; r["source_family"]=family
            r["market_key"]="h2h" if family=="H2H" else str(r.get("market_key") or "")
            r["point"]=None if family=="H2H" else r.get("point")
            out.append(r)
    return out


def prediction(db,table,pid):
    try: return db.fetchone(f"SELECT * FROM {table} WHERE id=?",(pid,))
    except Exception: return None
def model_probability(db,prefix,c):
    market=c["market_key"]; pid=c.get("prediction_id"); event=c["event_id"]; sel=str(c["selection"]); point=c.get("point")
    if market=="h2h":
        p=prediction(db,f"football_predictive{prefix}_predictions" if prefix else "football_predictive_predictions",pid)
        if not p:
            try: p=db.fetchone((f"SELECT * FROM football_predictive{prefix}_predictions" if prefix else "SELECT * FROM football_predictive_predictions")+" WHERE event_id=? ORDER BY id DESC LIMIT 1",(event,))
            except Exception: p=None
        if not p: return None
        if sel==str(p.get("home_team")): return f(p.get("home_probability"))
        if sel==str(p.get("away_team")): return f(p.get("away_probability"))
        if sel=="Draw": return f(p.get("draw_probability"))
        return None
    table=f"football_predictive{prefix}_market_predictions" if prefix else "football_predictive_market_predictions"
    try: rows=db.fetchall(f"SELECT * FROM {table} WHERE event_id=? AND market_key=? AND selection=?",(event,market,sel))
    except Exception: return None
    for r in rows:
        if point_eq(r.get("point"),point): return f(r.get("probability"))
    return None
def market_probability(db,c):
    try:
        rows=db.fetchall("""SELECT * FROM consensus_snapshots WHERE event_id=? AND captured_at<=? AND market_key=? AND selection=? ORDER BY captured_at DESC,id DESC""",(c["event_id"],c["created_at"],c["market_key"],c["selection"]))
    except Exception: rows=[]
    for r in rows:
        if point_eq(r.get("point"),c.get("point")) and f(r.get("fair_probability")) is not None: return f(r.get("fair_probability"))
    o=f(c.get("offered_odds")); return 1/o if o and o>1 else None
def source_prediction(db,c): return prediction(db,"football_predictive4_predictions",c.get("prediction_id"))


def insert_sample(db,model,key,mode,c,base,challenger,marketp,edge,bucket,decision,reason,features,version):
    db.execute("""INSERT INTO football_predictive_research_samples(
      model_name,sample_key,evidence_mode,created_at,source_table,source_bet_id,event_id,commence_time,market_key,selection,point,
      bookmaker_key,offered_odds,base_probability,challenger_probability,market_probability,edge_pct,bucket,decision,reason,features_json,model_version)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(model_name,sample_key) DO NOTHING""",
      (model,key,mode,c["created_at"],c["source_table"],c["id"],c["event_id"],c.get("commence_time"),c["market_key"],c["selection"],c.get("point"),c.get("bookmaker_key"),c.get("offered_odds"),base,challenger,marketp,edge,bucket,decision,reason,json.dumps(features,sort_keys=True,default=str),version))


def build_pred5(db,start):
    n=0
    for c in candidates(db):
        key=f"{c['source_table']}:{c['id']}"; mode="FORWARD" if (dt(c.get("created_at")) or now())>=start else "BACKFILL"
        p4=f(c.get("model_probability")); o=f(c.get("offered_odds"))
        if p4 is None or o is None: continue
        p1=model_probability(db,"",c); p2=model_probability(db,"2",c); mp=market_probability(db,c)
        comps=[x for x in (p1,p2) if x is not None]; comp=mean(comps) if comps else None; ens=(p4+comp)/2 if comp is not None else p4
        e=(ens*o-1)*100; disagreement=(max([p4]+comps)-min([p4]+comps))*100 if comps else None
        decision="ACCEPT" if comp is not None and e>=EDGE else "OBSERVE"; reason="FIXED_BLEND_EDGE" if decision=="ACCEPT" else "NO_COMPARATOR" if comp is None else "BELOW_3PCT_EDGE"
        insert_sample(db,"PRED5",key,mode,c,p4,ens,mp,e,None,decision,reason,{"pred1_p":p1,"pred2_p":p2,"pred4_p":p4,"comparator_p":comp,"model_disagreement_pp":disagreement,"residual_vs_market_pp":((ens-mp)*100 if mp is not None else None)},PRED5); n+=1
    return n


def team_form(rows,team):
    t=norm(team); games=[]
    for r in rows:
        h,a=norm(r.get("home_team")),norm(r.get("away_team")); hx,ax=f(r.get("home_proxy_xg")),f(r.get("away_proxy_xg"))
        if hx is None or ax is None: continue
        if h==t: games.append((dt(r.get("played_at")),hx,ax))
        elif a==t: games.append((dt(r.get("played_at")),ax,hx))
    games=[g for g in games if g[0]]; games.sort(key=lambda x:x[0]); games=games[-8:]
    if len(games)<6: return None
    recent=games[-3:]; prior=games[:-3][-5:]
    return {"attack_shift":mean(x[1] for x in recent)-mean(x[1] for x in prior),"defense_shift":mean(x[2] for x in recent)-mean(x[2] for x in prior),"matches":len(games),"latest":games[-1][0]}

def build_pred6(db,start):
    n=0
    for c in candidates(db):
        key=f"{c['source_table']}:{c['id']}"; mode="FORWARD" if (dt(c.get("created_at")) or now())>=start else "BACKFILL"
        pred=source_prediction(db,c); o=f(c.get("offered_odds")); base=f(c.get("model_probability"))
        if not pred or o is None or base is None: continue
        h0,a0=f(pred.get("expected_home_goals")),f(pred.get("expected_away_goals")); created=dt(c.get("created_at")); kickoff=dt(pred.get("commence_time")); decision="OBSERVE"; reason="INSUFFICIENT_FORM_HISTORY"; adj=None; edge=None; features={}
        if h0 is not None and a0 is not None and created:
            try: rows=db.fetchall("""SELECT * FROM football_pxg_current_matches WHERE sport_key=? AND played_at<? ORDER BY played_at""",(pred.get("sport_key"),created.isoformat()))
            except Exception: rows=[]
            home=str(pred.get("mapped_home_team") or pred.get("home_team") or ""); away=str(pred.get("mapped_away_team") or pred.get("away_team") or ""); hf,af=team_form(rows,home),team_form(rows,away)
            if hf and af:
                hd=hf["attack_shift"]+af["defense_shift"]; ad=af["attack_shift"]+hf["defense_shift"]; hm=max(.8,min(1.2,math.exp(FORM_COEFF*hd))); am=max(.8,min(1.2,math.exp(FORM_COEFF*ad))); h1,a1=h0*hm,a0*am
                adj=prob_from_lambdas(c["market_key"],str(c["selection"]),c.get("point"),h1,a1,str(pred.get("home_team")),str(pred.get("away_team")))
                if adj is not None: edge=(adj*o-1)*100; decision="ACCEPT" if edge>=EDGE else "OBSERVE"; reason="FIXED_FORM_EDGE" if decision=="ACCEPT" else "BELOW_3PCT_EDGE"
                features={"base_home_lambda":h0,"base_away_lambda":a0,"adjusted_home_lambda":h1,"adjusted_away_lambda":a1,"home_attack_shift":hf["attack_shift"],"home_defense_shift":hf["defense_shift"],"away_attack_shift":af["attack_shift"],"away_defense_shift":af["defense_shift"],"home_history_matches":hf["matches"],"away_history_matches":af["matches"],"home_rest_days":((kickoff-hf["latest"]).total_seconds()/86400 if kickoff else None),"away_rest_days":((kickoff-af["latest"]).total_seconds()/86400 if kickoff else None),"form_coeff":FORM_COEFF}
        insert_sample(db,"PRED6",key,mode,c,base,adj,market_probability(db,c),edge,None,decision,reason,features,PRED6); n+=1
    return n


def build_agreement(db,start):
    n=0
    for c in candidates(db):
        key=f"{c['source_table']}:{c['id']}"; mode="FORWARD" if (dt(c.get("created_at")) or now())>=start else "BACKFILL"; o=f(c.get("offered_odds")); p4=f(c.get("model_probability"))
        if o is None or p4 is None: continue
        p1,p2=model_probability(db,"",c),model_probability(db,"2",c); vals={"PRED1":p1,"PRED2":p2,"PRED4":p4}; positive={k for k,p in vals.items() if p is not None and (p*o-1)*100>=EDGE}; available=[p for p in vals.values() if p is not None]
        if len(positive)==3: bucket="ALL_3_POSITIVE"
        elif positive=={"PRED1","PRED4"}: bucket="PRED4_PLUS_PRED1"
        elif positive=={"PRED2","PRED4"}: bucket="PRED4_PLUS_PRED2"
        elif positive=={"PRED4"}: bucket="PRED4_ONLY"
        elif "PRED4" not in positive and positive: bucket="PRED1_PRED2_ONLY"
        elif len(available)<2: bucket="MISSING_COMPARATOR"
        else: bucket="NO_MULTI_MODEL_CONFIRMATION"
        dis=(max(available)-min(available))*100 if len(available)>1 else None
        insert_sample(db,"AGREEMENT",key,mode,c,p4,p4,market_probability(db,c),(p4*o-1)*100,bucket,"OBSERVE","FROZEN_AGREEMENT_BUCKET",{"pred1_p":p1,"pred2_p":p2,"pred4_p":p4,"positive_models":sorted(positive),"model_disagreement_pp":dis},"AGREEMENT_V1"); n+=1
    return n


def build_micro(db,start):
    n=0
    for c in candidates(db):
        key=f"{c['source_table']}:{c['id']}"; mode="FORWARD" if (dt(c.get("created_at")) or now())>=start else "BACKFILL"
        try: rows=db.fetchall("""SELECT * FROM odds_snapshots WHERE event_id=? AND market_key=? AND captured_at<=? ORDER BY captured_at,id""",(c["event_id"],c["market_key"],c["created_at"]))
        except Exception: rows=[]
        rows=[r for r in rows if str(r.get("outcome_name") or "")==str(c["selection"]) and point_eq(r.get("point"),c.get("point")) and f(r.get("price")) and f(r.get("price"))>1]; waves={}
        for r in rows: waves.setdefault(str(r.get("captured_at")),[]).append(r)
        feat={"quote_rows":len(rows),"waves":len(waves),"books_seen":len({str(r.get('bookmaker_key')) for r in rows})}
        if waves:
            keys=sorted(waves); medians=[]; leaders=[]
            for w in keys:
                rs=waves[w]; ps=[float(r["price"]) for r in rs]; medians.append((w,median(ps))); leaders.append(max(rs,key=lambda r:float(r["price"])).get("bookmaker_key"))
            first_t,last_t=dt(medians[0][0]),dt(medians[-1][0]); hours=max((last_t-first_t).total_seconds()/3600,1/60) if first_t and last_t else None
            feat.update({"first_median_odds":medians[0][1],"last_median_odds":medians[-1][1],"latest_best_odds":max(float(r["price"]) for r in waves[keys[-1]]),"latest_dispersion":max(float(r["price"]) for r in waves[keys[-1]])-min(float(r["price"]) for r in waves[keys[-1]]),"leader_changes":sum(1 for a,b in zip(leaders,leaders[1:]) if a!=b),"current_leader":leaders[-1],"implied_probability_velocity_pp_per_hour":(((1/medians[-1][1])-(1/medians[0][1]))*100/hours if hours else None)})
            qualifying=[]; mino=f(c.get("min_odds"))
            if mino:
                for w,rs in waves.items():
                    if max(float(r["price"]) for r in rs)>=mino: qualifying.append(dt(w))
            qualifying=[x for x in qualifying if x]
            if qualifying: feat["anomaly_persistence_minutes"]=(max(qualifying)-min(qualifying)).total_seconds()/60
            latest=waves[keys[-1]]; ex=[float(r["price"]) for r in latest if str(r.get("bookmaker_key")) in EXCHANGES]; fx=[float(r["price"]) for r in latest if str(r.get("bookmaker_key")) not in EXCHANGES]
            if ex and fx: feat.update({"best_exchange_odds":max(ex),"best_fixed_odds":max(fx),"exchange_fixed_gap_pct":(max(ex)/max(fx)-1)*100})
        insert_sample(db,"MICRO",key,mode,c,f(c.get("model_probability")),None,market_probability(db,c),f(c.get("edge_pct")),None,"OBSERVE","MARKET_MICROSTRUCTURE_CAPTURE",feat,"MICROSTRUCTURE_V1"); n+=1
    return n


def settle(db):
    rows=db.fetchall("SELECT * FROM football_predictive_research_samples WHERE status='OPEN' ORDER BY id"); n=0
    for s in rows:
        try: src=db.fetchone(f"SELECT * FROM {s['source_table']} WHERE id=?",(s["source_bet_id"],))
        except Exception: src=None
        if not src or str(src.get("status") or "") not in {"SETTLED","VOID"}: continue
        result=str(src.get("result") or "VOID"); accepted=str(s.get("decision"))=="ACCEPT"; odds=f(s.get("offered_odds")); pnl=None
        if accepted and result in {"WIN","LOSS","VOID","PUSH"}: pnl=(odds-1 if result=="WIN" and odds else -1 if result=="LOSS" else 0)
        elif s["model_name"] in {"AGREEMENT","MICRO"}: pnl=f(src.get("net_pnl_units")); pnl=pnl if pnl is not None else f(src.get("pnl_units"))
        p=f(s.get("challenger_probability")); brier=((p-(1 if result=="WIN" else 0))**2 if p is not None and result in {"WIN","LOSS"} else None)
        db.execute("""UPDATE football_predictive_research_samples SET status='SETTLED',result=?,pnl_units=?,clv_pct=?,clv_quality=?,brier_score=?,settled_at=? WHERE id=?""",(result,pnl,src.get("clv_pct"),src.get("clv_quality"),brier,utc_now_iso(),s["id"])); n+=1
    return n


def audit_euro(db):
    try: rows=db.fetchall("SELECT * FROM multisport_execution_bets WHERE sport_key='basketball_euroleague' AND status='SETTLED'")
    except Exception: rows=[]
    pnl=sum(f(r.get("net_pnl_units")) if f(r.get("net_pnl_units")) is not None else f(r.get("pnl_units")) or 0 for r in rows); clv=[float(r["clv_pct"]) for r in rows if str(r.get("clv_quality")) in {"A","B"} and f(r.get("clv_pct")) is not None]; cons=[float(r["clv_vs_consensus_pct"]) for r in rows if f(r.get("clv_vs_consensus_pct")) is not None]; trim=sorted(clv); k=int(len(trim)*.1); trim=trim[k:len(trim)-k] if k and len(trim)>2*k else trim; bybook={}
    for r in rows:
        b=str(r.get("bookmaker_key") or "UNKNOWN"); x=bybook.setdefault(b,{"settled":0,"pnl":0.0,"clv":[]}); x["settled"]+=1; x["pnl"]+=f(r.get("net_pnl_units")) if f(r.get("net_pnl_units")) is not None else f(r.get("pnl_units")) or 0
        if str(r.get("clv_quality")) in {"A","B"} and f(r.get("clv_pct")) is not None: x["clv"].append(float(r["clv_pct"]))
    bybook={b:{"settled":x["settled"],"pnl_units":x["pnl"],"roi_pct":x["pnl"]/x["settled"]*100 if x["settled"] else None,"avg_ab_clv_pct":avg(x["clv"]),"median_ab_clv_pct":med(x["clv"])} for b,x in bybook.items()}; detail={"settled_bets":len(rows),"pnl_units":pnl,"roi_pct":pnl/len(rows)*100 if rows else None,"ab_clv_samples":len(clv),"avg_ab_clv_pct":avg(clv),"median_ab_clv_pct":med(clv),"trimmed_ab_clv_pct":avg(trim),"beat_close_pct":sum(1 for x in clv if x>0)/len(clv)*100 if clv else None,"abs_clv_gt_50_count":sum(1 for x in clv if abs(x)>50),"abs_clv_gt_100_count":sum(1 for x in clv if abs(x)>100),"avg_consensus_clv_pct":avg(cons),"by_book":bybook}; status="OUTLIER_HEAVY" if detail["abs_clv_gt_50_count"]>=2 else "OK"; detail["status"]=status
    db.execute("INSERT INTO football_predictive_research_audits(audit_key,captured_at,status,detail_json) VALUES(?,?,?,?)",("EUROLEAGUE_CLV",utc_now_iso(),status,json.dumps(detail,sort_keys=True))); return detail


def audit_coverage(db):
    t0=now(); t1=t0+timedelta(hours=72)
    try: events=db.fetchall("SELECT * FROM events WHERE commence_time>? AND commence_time<=? ORDER BY commence_time",(t0.isoformat(),t1.isoformat()))
    except Exception: events=[]
    reasons={}; predicted=eligible=due=missing=0
    for e in events:
        exists=db.fetchone("SELECT id FROM football_predictive4_predictions WHERE event_id=? ORDER BY id DESC LIMIT 1",(e["event_id"],)); predicted+=1 if exists else 0; ko=dt(e.get("commence_time")); is_due=bool(ko and ko<=t0+timedelta(hours=24.5)); sport=str(e.get("sport_key") or "")
        try: rows=db.fetchall("SELECT * FROM football_pxg_current_matches WHERE sport_key=? AND played_at<? ORDER BY played_at",(sport,(ko or t0).isoformat()))
        except Exception: rows=[]
        reason="ELIGIBLE"
        if len(rows)<20: reason="INSUFFICIENT_LEAGUE_HISTORY"
        else:
            hn,an=norm(e.get("home_team")),norm(e.get("away_team")); home=[r for r in rows if hn in {norm(r.get('home_team')),norm(r.get('away_team'))}]; away=[r for r in rows if an in {norm(r.get('home_team')),norm(r.get('away_team'))}]
            if not home: reason="HOME_UNMAPPED"
            elif not away: reason="AWAY_UNMAPPED"
            elif len(home)<5: reason="INSUFFICIENT_HOME_HISTORY"
            elif len(away)<5: reason="INSUFFICIENT_AWAY_HISTORY"
            else:
                latest=max([dt(r.get("played_at")) for r in home+away if dt(r.get("played_at"))],default=None)
                if latest and ko and (ko-latest).total_seconds()/86400>30: reason="STALE_PXG_HISTORY"
        if reason=="ELIGIBLE": eligible+=1; due+=1 if is_due else 0
        if reason=="ELIGIBLE" and is_due and not exists: missing+=1; reason="DUE_ELIGIBLE_NO_PREDICTION"
        elif reason=="ELIGIBLE" and not is_due: reason="ELIGIBLE_NOT_YET_DUE"
        reasons[reason]=reasons.get(reason,0)+1
    detail={"status":"OK","upcoming_events":len(events),"predicted_events":predicted,"broadly_eligible_events":eligible,"due_eligible_events":due,"eligible_without_prediction":missing,"reason_counts":reasons,"window_hours":72,"note":"Broad audit mirrors raw gates approximately; PRED4 engine remains authority for effective-weight/fuzzy-mapping decisions."}; db.execute("INSERT INTO football_predictive_research_audits(audit_key,captured_at,status,detail_json) VALUES(?,?,?,?)",("PRED4_COVERAGE",utc_now_iso(),"OK",json.dumps(detail,sort_keys=True))); return detail


def latest_audit(db,key):
    r=db.fetchone("SELECT * FROM football_predictive_research_audits WHERE audit_key=? ORDER BY id DESC LIMIT 1",(key,))
    if not r: return {}
    try: return json.loads(r.get("detail_json") or "{}")
    except Exception: return {}
def model_score(db,name):
    rows=db.fetchall("SELECT * FROM football_predictive_research_samples WHERE model_name=? AND evidence_mode='FORWARD'",(name,)); accepted=[r for r in rows if r.get("decision")=="ACCEPT"]; settled=[r for r in accepted if r.get("status")=="SETTLED" and f(r.get("pnl_units")) is not None]; pnl=sum(float(r["pnl_units"]) for r in settled); clv=[float(r["clv_pct"]) for r in settled if str(r.get("clv_quality")) in {"A","B"} and f(r.get("clv_pct")) is not None]; b=[float(r["brier_score"]) for r in settled if f(r.get("brier_score")) is not None]; return {"forward_samples":len(rows),"forward_accepted":len(accepted),"settled":len(settled),"pnl_units":pnl,"roi_pct":pnl/len(settled)*100 if settled else None,"avg_ab_clv_pct":avg(clv),"avg_brier":avg(b)}
def agreement_score(db):
    rows=db.fetchall("SELECT * FROM football_predictive_research_samples WHERE model_name='AGREEMENT' AND evidence_mode='FORWARD' AND status='SETTLED'"); out={}
    for r in rows:
        b=str(r.get("bucket") or "UNKNOWN"); x=out.setdefault(b,{"settled":0,"pnl_units":0.0,"clv":[]}); x["settled"]+=1; x["pnl_units"]+=f(r.get("pnl_units")) or 0
        if str(r.get("clv_quality")) in {"A","B"} and f(r.get("clv_pct")) is not None: x["clv"].append(float(r["clv_pct"]))
    return {"buckets":{b:{"settled":x["settled"],"pnl_units":x["pnl_units"],"roi_pct":x["pnl_units"]/x["settled"]*100 if x["settled"] else None,"avg_ab_clv_pct":avg(x["clv"])} for b,x in out.items()}}
def scoreboard(db): return {"version":VERSION,"forward_start_at":state(db,"research_extensions_forward_start_at"),"pred5":model_score(db,"PRED5"),"pred6":model_score(db,"PRED6"),"agreement":agreement_score(db),"euroleague_clv_audit":latest_audit(db,"EUROLEAGUE_CLV"),"pred4_coverage_audit":latest_audit(db,"PRED4_COVERAGE")}


def run(db):
    ensure_schema(db); start=forward_start(db); summary={"version":VERSION,"forward_start_at":start.isoformat()}
    try:
        summary.update({"pred5_processed":build_pred5(db,start),"pred6_processed":build_pred6(db,start),"agreement_processed":build_agreement(db,start),"micro_processed":build_micro(db,start),"settled":settle(db),"euroleague":audit_euro(db),"coverage":audit_coverage(db)}); summary["scoreboard"]=scoreboard(db)
        try: db.record_collector_run("RESEARCH_EXTENSIONS_MAINT",True,detail=json.dumps(summary,sort_keys=True,default=str))
        except Exception: pass
        print("RESEARCH_EXTENSIONS "+json.dumps(summary,sort_keys=True,default=str),flush=True)
    except Exception as exc:
        summary["error"]=f"{type(exc).__name__}: {exc}"
        try: db.record_collector_run("RESEARCH_EXTENSIONS_MAINT",False,detail=summary["error"])
        except Exception: pass
        print("RESEARCH_EXTENSIONS_ERROR "+summary["error"],flush=True)
    return summary


def patch_exports():
    try:
        import exporter; exporter.EXPORT_TABLES=tuple(dict.fromkeys(tuple(exporter.EXPORT_TABLES)+EXPORT_TABLES))
    except Exception: pass
    try:
        import split_exporter; split_exporter.WEEKLY_TABLES=tuple(dict.fromkeys(tuple(split_exporter.WEEKLY_TABLES)+EXPORT_TABLES))
        if not getattr(split_exporter,"_research_extensions_patched",False):
            old=split_exporter._summary
            def wrapped(db,settings):
                x=old(db,settings); x["research_extensions"]=scoreboard(db); return x
            split_exporter._summary=wrapped; split_exporter._research_extensions_patched=True
    except Exception: pass
def loop(db):
    time.sleep(8)
    while True:
        run(db); time.sleep(RUN_EVERY)


def install(app:FastAPI,db,base_style:str):
    ensure_schema(db); forward_start(db); patch_exports()
    @app.get('/api/research-extensions/status')
    def status_api(): return scoreboard(db)
    @app.get('/api/research-extensions/samples')
    def samples_api(model:str=Query("PRED5"),limit:int=Query(100,ge=1,le=1000)):
        m=model.upper()
        if m not in {"PRED5","PRED6","AGREEMENT","MICRO"}: return JSONResponse({"error":"unknown model"},status_code=400)
        return db.fetchall("SELECT * FROM football_predictive_research_samples WHERE model_name=? ORDER BY id DESC LIMIT ?",(m,int(limit)))
    @app.get('/research-extensions',response_class=HTMLResponse)
    def page():
        s=scoreboard(db); p5=s["pred5"]; p6=s["pred6"]; eu=s["euroleague_clv_audit"]; cov=s["pred4_coverage_audit"]; buckets=s["agreement"].get("buckets",{}); br="".join(f"<tr><td>{escape(k)}</td><td>{v['settled']}</td><td>{fmt(v['pnl_units'])}u</td><td>{pct(v['roi_pct'])}</td><td>{pct(v['avg_ab_clv_pct'])}</td></tr>" for k,v in buckets.items()) or "<tr><td colspan=5>No forward settlements yet.</td></tr>"
        return HTMLResponse(f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Research Extensions</title><style>{base_style}.table-wrap{{overflow-x:auto}}body{{max-width:1400px;margin:auto}}</style></head><body><a href='/'>← Betting Lab</a><h1>Research Extensions <span class=pill>SHADOW · NO EXECUTION AUTHORITY</span></h1><p class=muted>Frozen forward challengers. Backfill never counts as forward evidence.</p><div class=grid><div class=panel><h2>PRED5 · residual/value</h2><p>Forward {p5['forward_samples']} · accepted {p5['forward_accepted']} · settled {p5['settled']}</p><p>P&L <strong class='{tone(p5['pnl_units'])}'>{fmt(p5['pnl_units'])}u</strong> · ROI {pct(p5['roi_pct'])} · A/B CLV {pct(p5['avg_ab_clv_pct'])} · Brier {fmt(p5['avg_brier'],4)}</p></div><div class=panel><h2>PRED6 · proxy-xG form transition</h2><p>Forward {p6['forward_samples']} · accepted {p6['forward_accepted']} · settled {p6['settled']}</p><p>P&L <strong class='{tone(p6['pnl_units'])}'>{fmt(p6['pnl_units'])}u</strong> · ROI {pct(p6['roi_pct'])} · A/B CLV {pct(p6['avg_ab_clv_pct'])} · Brier {fmt(p6['avg_brier'],4)}</p></div></div><div class=panel><h2>PRED1/PRED2/PRED4 agreement matrix</h2><div class=table-wrap><table><thead><tr><th>Bucket</th><th>Settled</th><th>P&L</th><th>ROI</th><th>A/B CLV</th></tr></thead><tbody>{br}</tbody></table></div></div><div class=grid><div class=panel><h2>EuroLeague CLV audit</h2><p>Status <strong>{escape(str(eu.get('status','WAITING')))}</strong> · settled {eu.get('settled_bets',0)} · ROI {pct(eu.get('roi_pct'))}</p><p>Mean A/B CLV {pct(eu.get('avg_ab_clv_pct'))} · median {pct(eu.get('median_ab_clv_pct'))} · trimmed {pct(eu.get('trimmed_ab_clv_pct'))} · consensus {pct(eu.get('avg_consensus_clv_pct'))}</p><p>|CLV| &gt;50%: {eu.get('abs_clv_gt_50_count',0)} · &gt;100%: {eu.get('abs_clv_gt_100_count',0)}</p></div><div class=panel><h2>PRED4 coverage audit</h2><p>Upcoming {cov.get('upcoming_events',0)} · predicted {cov.get('predicted_events',0)} · broadly eligible {cov.get('broadly_eligible_events',0)} · due eligible {cov.get('due_eligible_events',0)} · due/no prediction <strong>{cov.get('eligible_without_prediction',0)}</strong></p><p class=muted>{escape(json.dumps(cov.get('reason_counts',{}),sort_keys=True))}</p></div></div><p class=muted>Forward start: {escape(str(s.get('forward_start_at') or '—'))}. New research tables are included in Full and Weekly exports.</p></body></html>""")
    global _STARTED
    with _LOCK:
        if not _STARTED:
            _STARTED=True; threading.Thread(target=loop,args=(db,),daemon=True,name="research-extensions").start()
