from __future__ import annotations

from collections import defaultdict
import json, math, os
from typing import Dict, List, Tuple

from db import Database, utc_now_iso
import api_sports_predictive as core

VERSION = "APISPORTS_PRED1_WALKFORWARD_V1"
MIN_HISTORY = 3


def ensure_schema(db):
    id_col = "BIGSERIAL PRIMARY KEY" if db.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db.execute(f"""CREATE TABLE IF NOT EXISTS api_sports_pred_walkforward(
        id {id_col}, model_name TEXT NOT NULL, lane_key TEXT NOT NULL,
        provider_game_id TEXT NOT NULL, commence_time TEXT NOT NULL,
        home_probability REAL NOT NULL, outcome_home REAL NOT NULL,
        brier_score REAL NOT NULL, baseline_brier_score REAL NOT NULL,
        correct INTEGER, home_sample INTEGER NOT NULL, away_sample INTEGER NOT NULL,
        UNIQUE(model_name,provider_game_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS api_sports_pred_walkforward_summary(
        lane_key TEXT PRIMARY KEY, model_name TEXT NOT NULL, model_version TEXT NOT NULL,
        generated_at TEXT NOT NULL, samples INTEGER NOT NULL, avg_brier REAL,
        baseline_brier REAL, brier_improvement_pct REAL, accuracy_pct REAL,
        first_forecast_at TEXT, last_forecast_at TEXT)""")


def _stats(perf, team):
    rows=list(perf.get(team) or [])[-10:]
    if not rows: return None,None,0
    weights=list(range(1,len(rows)+1)); den=float(sum(weights))
    pf=sum(w*r[0] for w,r in zip(weights,rows))/den
    pa=sum(w*r[1] for w,r in zip(weights,rows))/den
    return pf,pa,len(rows)


def _predict(elo, perf, home, away):
    he=float(elo.get(home,1500.0)); ae=float(elo.get(away,1500.0))
    hpf,hpa,hn=_stats(perf,home); apf,apa,an=_stats(perf,away)
    if hn<MIN_HISTORY or an<MIN_HISTORY: return None
    base=core._logistic_elo((he+core.HOME_ADVANTAGE_ELO)-ae)
    home_exp=((hpf or 0)+(apa or 0))/2.0
    away_exp=((apf or 0)+(hpa or 0))/2.0
    adj=max(-0.08,min(0.08,(home_exp-away_exp)/100.0))
    hp=max(0.04,min(0.96,base+adj))
    return hp,hn,an


def _update(elo, perf, game):
    home=str(game['home_team']); away=str(game['away_team'])
    hs=float(game['home_score']); aas=float(game['away_score'])
    he=float(elo.get(home,1500.0)); ae=float(elo.get(away,1500.0))
    expected=core._logistic_elo((he+core.HOME_ADVANTAGE_ELO)-ae)
    actual=1.0 if hs>aas else 0.0 if hs<aas else 0.5
    delta=core.ELO_K*max(1.0,math.log(abs(hs-aas)+1.0))*(actual-expected)
    elo[home]=he+delta; elo[away]=ae-delta
    perf.setdefault(home,[]).append((hs,aas)); perf.setdefault(away,[]).append((aas,hs))


def run_lane(db, lane):
    rows=db.fetchall("""SELECT * FROM api_sports_pred_games
        WHERE lane_key=? AND status='FINISHED' AND home_score IS NOT NULL AND away_score IS NOT NULL
        ORDER BY commence_time,provider_game_id""",(lane.key,))
    groups=defaultdict(list)
    for r in rows: groups[str(r.get('commence_time') or '')].append(dict(r))
    elo={}; perf={}; home_sum=0.0; prior_n=0; out=[]
    for kickoff in sorted(groups):
        pending=[]
        for g in groups[kickoff]:
            hs=float(g['home_score']); aas=float(g['away_score'])
            y=1.0 if hs>aas else 0.0 if hs<aas else 0.5
            pred=_predict(elo,perf,str(g['home_team']),str(g['away_team']))
            if pred:
                hp,hn,an=pred; bp=(home_sum/prior_n) if prior_n>=10 else 0.5
                correct=None if y==0.5 else int((hp>=0.5)==(y==1.0))
                out.append((lane.model_name,lane.key,str(g['provider_game_id']),kickoff,hp,y,(hp-y)**2,(bp-y)**2,correct,hn,an))
            pending.append((g,y))
        for g,y in pending:
            _update(elo,perf,g); home_sum+=y; prior_n+=1
    db.execute("DELETE FROM api_sports_pred_walkforward WHERE model_name=?",(lane.model_name,))
    for row in out:
        db.execute("""INSERT INTO api_sports_pred_walkforward(
            model_name,lane_key,provider_game_id,commence_time,home_probability,outcome_home,
            brier_score,baseline_brier_score,correct,home_sample,away_sample)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",row)
    n=len(out); b=sum(x[6] for x in out)/n if n else None; bb=sum(x[7] for x in out)/n if n else None
    imp=((bb-b)/bb*100.0) if n and bb else None
    resolved=[x for x in out if x[8] is not None]
    acc=(sum(x[8] for x in resolved)/len(resolved)*100.0) if resolved else None
    first=out[0][3] if out else None; last=out[-1][3] if out else None
    db.execute("""INSERT INTO api_sports_pred_walkforward_summary(
        lane_key,model_name,model_version,generated_at,samples,avg_brier,baseline_brier,
        brier_improvement_pct,accuracy_pct,first_forecast_at,last_forecast_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(lane_key) DO UPDATE SET
        model_name=excluded.model_name,model_version=excluded.model_version,generated_at=excluded.generated_at,
        samples=excluded.samples,avg_brier=excluded.avg_brier,baseline_brier=excluded.baseline_brier,
        brier_improvement_pct=excluded.brier_improvement_pct,accuracy_pct=excluded.accuracy_pct,
        first_forecast_at=excluded.first_forecast_at,last_forecast_at=excluded.last_forecast_at""",
        (lane.key,lane.model_name,VERSION,utc_now_iso(),n,b,bb,imp,acc,first,last))
    return {'samples':n,'avg_brier':b,'baseline_brier':bb,'brier_improvement_pct':imp,'accuracy_pct':acc}


def run():
    db=Database(os.getenv('DATABASE_URL','').strip(),os.getenv('DB_PATH','./betting_lab.sqlite'))
    ensure_schema(db); result={'version':VERSION,'lanes':{}}
    for lane in core.LANES:
        try: result['lanes'][lane.key]={'ok':True,**run_lane(db,lane)}
        except Exception as exc: result['lanes'][lane.key]={'ok':False,'error':f'{type(exc).__name__}: {exc}'}
    print('API_SPORTS_WALKFORWARD '+json.dumps(result,sort_keys=True),flush=True)
    return result
