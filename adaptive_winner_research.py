from __future__ import annotations

"""Adaptive winner research expansion.

Research-only additions:
- W1 Winner Discovery across the unified singles universe plus API-Sports lanes.
- TPRED4 tennis challenger using opponent-adjusted serve/return and scoreline dominance.
- Dense tennis market-timing snapshots derived from already-stored odds waves (zero new provider calls).
- Tennis Winner Discovery layer on top of TPRED4.

Nothing in this module can place, gate, size, alert, or alter an existing bet.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
import json
import math
import re
from statistics import mean
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from fastapi.responses import HTMLResponse

from db import utc_now_iso
import tennis_predictive3 as tpred3
from tennis_predictive3 import _extract_stats, _serve_summary, _return_summary
from tennis_rich_source import _norm_name
from tennis_shadow import _market_metrics

VERSION = "ADAPTIVE_WINNER_RESEARCH_V1"
W1_MODEL_VERSION = "W1_PREBET_HIERARCHICAL_V1"
TPRED4_MODEL_VERSION = "TPRED4_OPP_ADJ_DOM_V1"
TENNIS_W1_MODEL_VERSION = "TENNIS_W1_TPRED4_V1"
DASHBOARD_VERSION = "0.19.76"
EXPORT_TABLES = (
    "winner_discovery_state",
    "winner_discovery_samples",
    "tennis_predictive4_state",
    "tennis_predictive4_walkforward",
    "tennis_predictive4_player_state",
    "tennis_predictive4_predictions",
    "tennis_market_timing_snapshots",
    "tennis_winner_discovery_samples",
)
W1_SEGMENTS = (
    "source_lane", "sport", "market", "bookmaker", "odds_band", "hours_band",
    "edge_band", "sport_market", "sport_odds", "agreement_band",
)
TENNIS_SEGMENTS = (
    "tour", "surface", "prob_band", "serve_edge_band", "return_edge_band",
    "dominance_edge_band", "market_gap_band", "tour_surface", "surface_prob",
)
_MIN_SEGMENT = 8
_SHRINK_N = 40.0
_INSTALLED = False


def _dt(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if out.tzinfo is None:
            out = out.replace(tzinfo=timezone.utc)
        return out.astimezone(timezone.utc)
    except Exception:
        return None


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _clip(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, float(value)))


def _sigmoid(value: float) -> float:
    if value >= 0:
        e = math.exp(-value)
        return 1.0 / (1.0 + e)
    e = math.exp(value)
    return e / (1.0 + e)


def _logit(prob: float) -> float:
    p = _clip(prob, 1e-6, 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def _fair_odds(prob: float) -> float:
    return 1.0 / _clip(prob, 1e-6, 1.0 - 1e-6)


def _state(db, table: str, key: str) -> Optional[str]:
    row = db.fetchone(f"SELECT state_value FROM {table} WHERE state_key=?", (key,))
    return str(row.get("state_value")) if row and row.get("state_value") is not None else None


def _set_state(db, table: str, key: str, value: Any) -> None:
    db.execute(
        f"""INSERT INTO {table}(state_key,state_value,updated_at)
            VALUES(?,?,?)
            ON CONFLICT(state_key) DO UPDATE SET
              state_value=excluded.state_value,updated_at=excluded.updated_at""",
        (key, str(value), utc_now_iso()),
    )


def _ensure_forward_start(db, table: str, key: str) -> datetime:
    raw = _state(db, table, key)
    if not raw:
        raw = utc_now_iso()
        _set_state(db, table, key, raw)
    return _dt(raw) or datetime.now(timezone.utc)


def ensure_schema(db) -> Dict[str, Any]:
    ident = "BIGSERIAL PRIMARY KEY" if bool(getattr(db, "is_postgres", False)) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    for state_table in ("winner_discovery_state", "tennis_predictive4_state"):
        db.execute(
            f"""CREATE TABLE IF NOT EXISTS {state_table}(
                state_key TEXT PRIMARY KEY,
                state_value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
    db.execute(
        f"""CREATE TABLE IF NOT EXISTS winner_discovery_samples(
            id {ident}, candidate_key TEXT NOT NULL UNIQUE, evidence_mode TEXT NOT NULL,
            created_at TEXT NOT NULL, event_id TEXT, source_lane TEXT, source_lanes TEXT,
            agreement_count INTEGER NOT NULL DEFAULT 1, sport_key TEXT, league TEXT,
            market_key TEXT, selection TEXT, point REAL, bookmaker_key TEXT, entry_odds REAL,
            edge_pct REAL, hours_to_start REAL, predicted_win_prob REAL NOT NULL,
            confidence TEXT NOT NULL, model_version TEXT NOT NULL, features_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN', result TEXT, pnl_units REAL, settled_at TEXT,
            updated_at TEXT NOT NULL
        )"""
    )
    db.execute(
        f"""CREATE TABLE IF NOT EXISTS tennis_predictive4_walkforward(
            id {ident}, source_walkforward_id INTEGER NOT NULL UNIQUE, rich_match_id INTEGER,
            commence_time TEXT NOT NULL, tour TEXT NOT NULL, surface TEXT, player_one TEXT NOT NULL,
            player_two TEXT NOT NULL, predicted_p1 REAL NOT NULL, market_p1 REAL, outcome_p1 INTEGER NOT NULL,
            brier REAL NOT NULL, market_brier REAL, evidence_ready INTEGER NOT NULL DEFAULT 0,
            feature_json TEXT NOT NULL, model_version TEXT NOT NULL, created_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS tennis_predictive4_player_state(
            tour TEXT NOT NULL, player_norm TEXT NOT NULL, matches INTEGER NOT NULL DEFAULT 0,
            adj_serve REAL NOT NULL DEFAULT 0.5, adj_return REAL NOT NULL DEFAULT 0.5,
            dominance REAL NOT NULL DEFAULT 0.0, recent_dominance REAL NOT NULL DEFAULT 0.0,
            updated_at TEXT NOT NULL, PRIMARY KEY(tour,player_norm)
        )"""
    )
    db.execute(
        f"""CREATE TABLE IF NOT EXISTS tennis_predictive4_predictions(
            id {ident}, event_id TEXT NOT NULL, selection TEXT NOT NULL, opponent TEXT NOT NULL,
            generated_at TEXT NOT NULL, commence_time TEXT NOT NULL, tour TEXT NOT NULL, surface TEXT,
            probability REAL NOT NULL, fair_odds REAL NOT NULL, market_probability REAL,
            model_edge_vs_market_pct REAL, confidence TEXT NOT NULL, feature_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN', outcome INTEGER, brier REAL, settled_at TEXT,
            model_version TEXT NOT NULL, UNIQUE(model_version,event_id,selection)
        )"""
    )
    db.execute(
        f"""CREATE TABLE IF NOT EXISTS tennis_market_timing_snapshots(
            id {ident}, event_id TEXT NOT NULL, selection TEXT NOT NULL, captured_at TEXT NOT NULL,
            commence_time TEXT NOT NULL, hours_to_start REAL, fair_probability REAL NOT NULL,
            fair_odds REAL NOT NULL, num_books INTEGER, price_dispersion_pct REAL, mean_overround_pct REAL,
            capture_source TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(event_id,selection,captured_at,capture_source)
        )"""
    )
    db.execute(
        f"""CREATE TABLE IF NOT EXISTS tennis_winner_discovery_samples(
            id {ident}, sample_key TEXT NOT NULL UNIQUE, evidence_mode TEXT NOT NULL,
            event_id TEXT NOT NULL, selection TEXT NOT NULL, opponent TEXT NOT NULL,
            generated_at TEXT NOT NULL, commence_time TEXT NOT NULL, tour TEXT NOT NULL, surface TEXT,
            tpred4_probability REAL NOT NULL, winner_probability REAL NOT NULL,
            segment_support INTEGER NOT NULL DEFAULT 0, confidence TEXT NOT NULL,
            feature_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN', outcome INTEGER,
            brier REAL, settled_at TEXT, model_version TEXT NOT NULL
        )"""
    )
    _ensure_forward_start(db, "winner_discovery_state", "forward_start_at")
    _ensure_forward_start(db, "tennis_predictive4_state", "winner_forward_start_at")
    return {"installed": True, "version": VERSION, "betting_authority": False}


# W1 ------------------------------------------------------------------------

def _odds_band(v: Any) -> str:
    x = _f(v)
    if x is None: return "UNKNOWN"
    if x < 1.5: return "LT1.5"
    if x < 2.0: return "1.5-1.99"
    if x < 3.0: return "2-2.99"
    if x < 4.0: return "3-3.99"
    if x < 5.0: return "4-4.99"
    if x < 7.5: return "5-7.49"
    return "7.5+"


def _hours_band(v: Any) -> str:
    x = _f(v)
    if x is None: return "UNKNOWN"
    if x < 3: return "0-3H"
    if x < 12: return "3-12H"
    if x < 24: return "12-24H"
    if x < 48: return "24-48H"
    if x < 72: return "48-72H"
    return "72H+"


def _edge_band(v: Any) -> str:
    x = _f(v)
    if x is None: return "UNKNOWN"
    if x < 0: return "NEG"
    if x < 3: return "0-2.99"
    if x < 5: return "3-4.99"
    if x < 10: return "5-9.99"
    return "10+"


def _w1_feature_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    sport = str(row.get("sport_key") or "UNKNOWN").upper()
    market = str(row.get("market_key") or "h2h")
    odds = _f(row.get("entry_odds")); edge = _f(row.get("edge_pct")); hours = _f(row.get("hours_to_start"))
    source = str(row.get("source_lane") or ""); book = str(row.get("bookmaker_key") or "")
    agreement = int(row.get("agreement_count") or 1); ob = _odds_band(odds)
    return {
        "source_lane": source, "sport": sport, "market": market, "bookmaker": book,
        "odds_band": ob, "hours_band": _hours_band(hours), "edge_band": _edge_band(edge),
        "sport_market": f"{sport}|{market}", "sport_odds": f"{sport}|{ob}",
        "agreement_band": "3+" if agreement >= 3 else str(max(1, agreement)),
        "entry_odds": odds, "edge_pct": edge, "hours_to_start": hours,
    }


def _candidate_key(event_id: Any, market: Any, selection: Any, point: Any) -> str:
    p = "" if point is None else str(round(float(point), 8)) if _f(point) is not None else str(point)
    return "|".join((str(event_id or ""), str(market or "h2h"), str(selection or ""), p))


def _clv_candidates(db) -> list[Dict[str, Any]]:
    try: rows = db.fetchall("SELECT * FROM clv_first_samples ORDER BY created_at,id")
    except Exception: rows = []
    grouped: Dict[str, list] = defaultdict(list)
    for raw in rows:
        row = dict(raw); key = _candidate_key(row.get("event_id"), row.get("market_key"), row.get("selection"), row.get("point"))
        if key.strip("|"): grouped[key].append(row)
    out = []
    for key, items in grouped.items():
        items.sort(key=lambda r: str(r.get("created_at") or "")); first = dict(items[0]); first_at = _dt(first.get("created_at"))
        near = [r for r in items if not first_at or not _dt(r.get("created_at")) or _dt(r.get("created_at")) <= first_at + timedelta(minutes=5)]
        lanes = sorted({str(r.get("source_lane") or "") for r in near if r.get("source_lane")})
        result = next((str(r.get("result") or "").upper() for r in items if str(r.get("result") or "").upper() in {"WIN","LOSS"}), "")
        odds = _f(first.get("entry_odds")); pnl = (odds - 1.0) if result == "WIN" and odds else (-1.0 if result == "LOSS" else None)
        out.append({
            "candidate_key": key, "created_at": first.get("created_at"), "event_id": first.get("event_id"),
            "source_lane": str(first.get("source_lane") or ""), "source_lanes": ",".join(lanes),
            "agreement_count": max(1, len(lanes)), "sport_key": first.get("sport_key"), "league": first.get("league"),
            "market_key": first.get("market_key") or "h2h", "selection": first.get("selection"), "point": first.get("point"),
            "bookmaker_key": first.get("bookmaker_key"), "entry_odds": odds, "edge_pct": _f(first.get("edge_pct")),
            "hours_to_start": _f(first.get("hours_to_start")), "result": result or None, "pnl_units": pnl,
            "settled_at": next((r.get("settled_at") for r in items if r.get("settled_at")), None),
        })
    return out


def _api_sports_candidates(db) -> list[Dict[str, Any]]:
    try:
        rows = db.fetchall(
            """SELECT b.*,f.lane_key,f.generated_at,f.commence_time
               FROM api_sports_pred_shadow_bets b JOIN api_sports_pred_forecasts f ON f.id=b.forecast_id
               ORDER BY b.created_at,b.id"""
        )
    except Exception: return []
    out = []
    for r in rows:
        row = dict(r); created = _dt(row.get("created_at")); commence = _dt(row.get("commence_time"))
        hours = ((commence-created).total_seconds()/3600.0) if created and commence else None
        result = str(row.get("result") or "").upper(); result = result if result in {"WIN","LOSS"} else None
        out.append({
            "candidate_key": f"APISPORTS|{row.get('forecast_id')}|{row.get('selection')}",
            "created_at": row.get("created_at") or row.get("generated_at"), "event_id": row.get("odds_event_id") or f"provider:{row.get('forecast_id')}",
            "source_lane": f"APISPORTS_{str(row.get('lane_key') or '').upper()}", "source_lanes": f"APISPORTS_{str(row.get('lane_key') or '').upper()}",
            "agreement_count": 1, "sport_key": "AMERICAN_FOOTBALL" if str(row.get("lane_key")) in {"NFL","NCAAF"} else "BASKETBALL",
            "league": row.get("lane_key"), "market_key": "h2h", "selection": row.get("selection"), "point": None,
            "bookmaker_key": row.get("bookmaker_key"), "entry_odds": _f(row.get("offered_odds")), "edge_pct": _f(row.get("edge_pct")),
            "hours_to_start": hours, "result": result, "pnl_units": _f(row.get("pnl_units")), "settled_at": None,
        })
    return out


def _all_w1_candidates(db) -> list[Dict[str, Any]]:
    merged = {}
    for row in _clv_candidates(db) + _api_sports_candidates(db):
        key = str(row.get("candidate_key") or "")
        if key and (key not in merged or str(row.get("created_at") or "") < str(merged[key].get("created_at") or "")): merged[key] = row
    return sorted(merged.values(), key=lambda r: str(r.get("created_at") or ""))


def _blank_counts(segment_keys: Sequence[str]) -> Dict[str, Any]:
    return {"global": {"n": 0, "wins": 0}, "segments": {k: {} for k in segment_keys}}


def _score_counts(counts: Mapping[str, Any], features: Mapping[str, Any], segment_keys: Sequence[str]) -> Tuple[float, int, list]:
    g = counts.get("global") or {}; gn = int(g.get("n") or 0); gw = int(g.get("wins") or 0); global_p = (gw + 5.0) / (gn + 10.0)
    contributions = []
    for key in segment_keys:
        value = str(features.get(key) or ""); stat = ((counts.get("segments") or {}).get(key) or {}).get(value) if value else None
        if not stat: continue
        n = int(stat.get("n") or 0); wins = int(stat.get("wins") or 0)
        if n < _MIN_SEGMENT: continue
        seg_p = (wins + 5.0 * global_p) / (n + 5.0); w = n / (n + _SHRINK_N); contributions.append((w, seg_p, n, key, value))
    if contributions:
        denom = sum(x[0] for x in contributions); p = global_p + sum(w*(sp-global_p) for w,sp,_,_,_ in contributions)/max(1.0,denom); support=max(x[2] for x in contributions)
    else: p=global_p; support=gn
    return _clip(p,0.05,0.95), int(support), contributions


def _update_counts(counts: Dict[str, Any], features: Mapping[str, Any], y: int, segment_keys: Sequence[str]) -> None:
    counts["global"]["n"] += 1; counts["global"]["wins"] += int(y)
    for key in segment_keys:
        value = str(features.get(key) or "")
        if not value: continue
        bucket = counts["segments"][key].setdefault(value,{"n":0,"wins":0}); bucket["n"] += 1; bucket["wins"] += int(y)


def _top_discoveries(counts: Mapping[str, Any], segment_keys: Sequence[str], min_n: int = 20, limit: int = 20) -> list:
    g=counts.get("global") or {}; gp=(int(g.get("wins") or 0)+5.0)/(int(g.get("n") or 0)+10.0); rows=[]
    for key in segment_keys:
        for value,stat in ((counts.get("segments") or {}).get(key) or {}).items():
            n=int(stat.get("n") or 0)
            if n < min_n: continue
            p=(int(stat.get("wins") or 0)+5.0*gp)/(n+5.0); rows.append({"feature":key,"value":value,"n":n,"hit_rate_pct":p*100.0,"uplift_pp":(p-gp)*100.0})
    return sorted(rows,key=lambda x:(x["uplift_pp"],x["n"]),reverse=True)[:limit]


def _train_w1(db,start:datetime) -> Dict[str,Any]:
    counts=_blank_counts(W1_SEGMENTS); briers=[]; correct=scored=0
    training=[r for r in _all_w1_candidates(db) if (_dt(r.get("created_at")) or start) < start and r.get("result") in {"WIN","LOSS"}]
    for row in training:
        feat=_w1_feature_row(row); p,_,_=_score_counts(counts,feat,W1_SEGMENTS); y=1 if row["result"]=="WIN" else 0
        if int(counts["global"]["n"])>=25: briers.append((p-y)**2); correct += int((p>=0.5)==bool(y)); scored += 1
        _update_counts(counts,feat,y,W1_SEGMENTS)
    model={"model_version":W1_MODEL_VERSION,"trained_at":utc_now_iso(),"training_cutoff":start.isoformat(),"training_samples":len(training),"counts":counts,
           "prequential":{"scored":scored,"brier":sum(briers)/len(briers) if briers else None,"accuracy_pct":correct/scored*100.0 if scored else None},
           "discoveries":_top_discoveries(counts,W1_SEGMENTS),"policy":{"research_only":True,"no_price_gate":True,"no_execution_authority":True}}
    _set_state(db,"winner_discovery_state","model_json",json.dumps(model,sort_keys=True,default=str)); return model


def _w1_model(db,start):
    raw=_state(db,"winner_discovery_state","model_json")
    if raw:
        try:
            model=json.loads(raw)
            if model.get("model_version")==W1_MODEL_VERSION and model.get("training_cutoff")==start.isoformat(): return model
        except Exception: pass
    return _train_w1(db,start)


def run_w1(db) -> Dict[str,Any]:
    start=_ensure_forward_start(db,"winner_discovery_state","forward_start_at"); model=_w1_model(db,start); counts=model.get("counts") or _blank_counts(W1_SEGMENTS); inserted=updated=0
    for row in _all_w1_candidates(db):
        created=_dt(row.get("created_at"))
        if not created or created < start: continue
        key=str(row["candidate_key"]); existing=db.fetchone("SELECT id,status FROM winner_discovery_samples WHERE candidate_key=?",(key,))
        if not existing:
            feat=_w1_feature_row(row); p,support,contrib=_score_counts(counts,feat,W1_SEGMENTS); confidence="HIGH" if support>=100 else "MEDIUM" if support>=30 else "LOW"; status="SETTLED" if row.get("result") in {"WIN","LOSS"} else "OPEN"
            db.execute("""INSERT INTO winner_discovery_samples(candidate_key,evidence_mode,created_at,event_id,source_lane,source_lanes,agreement_count,sport_key,league,market_key,selection,point,bookmaker_key,entry_odds,edge_pct,hours_to_start,predicted_win_prob,confidence,model_version,features_json,status,result,pnl_units,settled_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (key,"FORWARD",row.get("created_at"),row.get("event_id"),row.get("source_lane"),row.get("source_lanes"),int(row.get("agreement_count") or 1),row.get("sport_key"),row.get("league"),row.get("market_key"),row.get("selection"),row.get("point"),row.get("bookmaker_key"),row.get("entry_odds"),row.get("edge_pct"),row.get("hours_to_start"),p,confidence,W1_MODEL_VERSION,json.dumps({"features":feat,"segments":contrib},sort_keys=True,default=str),status,row.get("result"),row.get("pnl_units"),row.get("settled_at"),utc_now_iso()))
            inserted += 1
        elif row.get("result") in {"WIN","LOSS"} and str(existing.get("status") or "") != "SETTLED":
            db.execute("UPDATE winner_discovery_samples SET status='SETTLED',result=?,pnl_units=?,settled_at=?,updated_at=? WHERE id=?",(row.get("result"),row.get("pnl_units"),row.get("settled_at") or utc_now_iso(),utc_now_iso(),existing["id"])); updated += 1
    return {"ok":True,"inserted":inserted,"updated":updated,"training_samples":model.get("training_samples"),"betting_authority":False}


def _winner_metrics(rows,threshold=None):
    selected=[r for r in rows if threshold is None or float(r.get("predicted_win_prob") or 0)>=threshold]; settled=[r for r in selected if str(r.get("result") or "") in {"WIN","LOSS"}]; wins=sum(1 for r in settled if str(r.get("result"))=="WIN")
    brier=mean([(float(r["predicted_win_prob"])-(1 if r["result"]=="WIN" else 0))**2 for r in settled]) if settled else None; pnl_rows=[r for r in settled if _f(r.get("pnl_units")) is not None]; pnl=sum(float(r["pnl_units"]) for r in pnl_rows) if pnl_rows else 0.0
    return {"samples":len(selected),"settled":len(settled),"wins":wins,"hit_rate_pct":wins/len(settled)*100.0 if settled else None,"brier":brier,"pnl_units":pnl,"roi_pct":pnl/len(pnl_rows)*100.0 if pnl_rows else None}


def winner_scoreboard(db):
    start=_ensure_forward_start(db,"winner_discovery_state","forward_start_at"); model=_w1_model(db,start); rows=db.fetchall("SELECT * FROM winner_discovery_samples WHERE model_version=? ORDER BY id",(W1_MODEL_VERSION,))
    return {"version":VERSION,"model_version":W1_MODEL_VERSION,"forward_start_at":start.isoformat(),"training_samples":model.get("training_samples",0),"prequential":model.get("prequential") or {},"forward":_winner_metrics(rows),"thresholds":{str(t):_winner_metrics(rows,t) for t in (0.55,0.60,0.65,0.70,0.75)},"historical_discoveries":model.get("discoveries") or [],"sources":["CLV_FIRST_UNIFIED_SINGLES","API_SPORTS_NFL","API_SPORTS_NCAAF","API_SPORTS_EUROLEAGUE"],"authority":"RESEARCH_ONLY_NO_EXECUTION"}


# TPRED4 --------------------------------------------------------------------

def _player(): return {"matches":0,"serve":0.5,"return":0.5,"dom":0.0,"recent_dom":0.0}


def _load_players(db):
    out=defaultdict(_player)
    try: rows=db.fetchall("SELECT * FROM tennis_predictive4_player_state")
    except Exception: rows=[]
    for r in rows: out[(str(r.get("tour") or "").lower(),str(r.get("player_norm") or ""))]={"matches":int(r.get("matches") or 0),"serve":_f(r.get("adj_serve")) or 0.5,"return":_f(r.get("adj_return")) or 0.5,"dom":_f(r.get("dominance")) or 0.0,"recent_dom":_f(r.get("recent_dominance")) or 0.0}
    return out


def _save_players(db,players):
    now=utc_now_iso(); rows=[(tour,norm,int(p.get("matches") or 0),float(p.get("serve") or 0.5),float(p.get("return") or 0.5),float(p.get("dom") or 0.0),float(p.get("recent_dom") or 0.0),now) for (tour,norm),p in players.items() if norm]
    if rows: db.executemany("""INSERT INTO tennis_predictive4_player_state(tour,player_norm,matches,adj_serve,adj_return,dominance,recent_dominance,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(tour,player_norm) DO UPDATE SET matches=excluded.matches,adj_serve=excluded.adj_serve,adj_return=excluded.adj_return,dominance=excluded.dominance,recent_dominance=excluded.recent_dominance,updated_at=excluded.updated_at""",rows)


def _dominance_magnitude(result):
    sets=[(int(a),int(b)) for a,b in re.findall(r"(\d{1,2})\s*[-:]\s*(\d{1,2})",str(result or ""))]; sets=[(a,b) for a,b in sets if a<=20 and b<=20]
    if not sets: return 0.0
    total=sum(a+b for a,b in sets); margin=abs(sum(a-b for a,b in sets)); return _clip((margin/max(1,total))*2.0,0.0,1.0)


def _profile_features(players,tour,p1,p2):
    a=players[(tour.lower(),_norm_name(p1))]; b=players[(tour.lower(),_norm_name(p2))]
    return {"opp_adj_serve_diff":_clip((float(a["serve"])-float(b["serve"]))*2.0,-1,1),"opp_adj_return_diff":_clip((float(a["return"])-float(b["return"]))*2.0,-1,1),"dominance_diff":_clip(float(a["dom"])-float(b["dom"]),-1,1),"recent_dominance_diff":_clip(float(a["recent_dom"])-float(b["recent_dom"]),-1,1),"p1_profile_matches":int(a["matches"]),"p2_profile_matches":int(b["matches"])}


def _tpred4_predict(base_p,weights,feat):
    z=_logit(base_p)
    for key in ("opp_adj_serve_diff","opp_adj_return_diff","dominance_diff","recent_dominance_diff"): z += float(weights.get(key) or 0.0)*float(feat.get(key) or 0.0)
    return _clip(_sigmoid(z),0.03,0.97)


def _tpred4_learn(weights,feat,p,y,updates):
    eta=0.025/math.sqrt(1.0+updates/500.0); err=int(y)-float(p)
    for key in ("opp_adj_serve_diff","opp_adj_return_diff","dominance_diff","recent_dominance_diff"):
        old=float(weights.get(key) or 0.0); x=float(feat.get(key) or 0.0); weights[key]=old+eta*(err*x-0.002*old)


def _update_profiles(players,tour,p1,p2,outcome_p1,stats1,stats2,dom_mag):
    a=players[(tour.lower(),_norm_name(p1))]; b=players[(tour.lower(),_norm_name(p2))]; sa=_serve_summary(stats1); sb=_serve_summary(stats2); ra=_return_summary(stats1); rb=_return_summary(stats2); aa=0.15 if int(a["matches"])<10 else 0.08; ab=0.15 if int(b["matches"])<10 else 0.08
    if sa is not None: a["serve"]=(1-aa)*float(a["serve"])+aa*_clip(float(sa)-(float(b["return"])-0.5),0.20,0.80)
    if sb is not None: b["serve"]=(1-ab)*float(b["serve"])+ab*_clip(float(sb)-(float(a["return"])-0.5),0.20,0.80)
    if ra is not None: a["return"]=(1-aa)*float(a["return"])+aa*_clip(float(ra)-(float(b["serve"])-0.5),0.20,0.80)
    if rb is not None: b["return"]=(1-ab)*float(b["return"])+ab*_clip(float(rb)-(float(a["serve"])-0.5),0.20,0.80)
    ad=float(dom_mag) if int(outcome_p1)==1 else -float(dom_mag); bd=-ad; a["dom"]=0.92*float(a["dom"])+0.08*ad; b["dom"]=0.92*float(b["dom"])+0.08*bd; a["recent_dom"]=0.75*float(a["recent_dom"])+0.25*ad; b["recent_dom"]=0.75*float(b["recent_dom"])+0.25*bd; a["matches"]=int(a["matches"])+1; b["matches"]=int(b["matches"])+1


def _tpred4_weights(db):
    raw=_state(db,"tennis_predictive4_state","weights_json")
    try: data=json.loads(raw) if raw else {}
    except Exception: data={}
    return {k:float(data.get(k) or 0.0) for k in ("opp_adj_serve_diff","opp_adj_return_diff","dominance_diff","recent_dominance_diff")}


def _process_tpred4_history(db):
    last_id=int(_state(db,"tennis_predictive4_state","last_source_walkforward_id") or 0); weights=_tpred4_weights(db); players=_load_players(db)
    try:
        rows=db.fetchall("""SELECT w.id AS source_walkforward_id,w.rich_match_id,w.commence_time,w.tour,w.surface,w.player_one,w.player_two,w.predicted_p1,w.market_p1,w.outcome_p1,w.evidence_ready,w.feature_json,r.player1_name AS rich_p1,r.player2_name AS rich_p2,r.result AS rich_result,r.stats_json FROM tennis_predictive3_walkforward w LEFT JOIN tennis_rich_matches r ON r.id=w.rich_match_id WHERE w.model_version=? AND w.id>? ORDER BY w.commence_time,w.id""",(tpred3.MODEL_VERSION,last_id))
    except Exception as exc: return {"ok":False,"error":f"{type(exc).__name__}: {exc}","processed":0}
    inserts=[]; max_id=last_id; updates=int(_state(db,"tennis_predictive4_state","learning_updates") or 0)
    for row in rows:
        source_id=int(row.get("source_walkforward_id") or 0); max_id=max(max_id,source_id); tour=str(row.get("tour") or ""); p1=str(row.get("player_one") or ""); p2=str(row.get("player_two") or ""); base=float(row.get("predicted_p1")); y=int(row.get("outcome_p1") or 0); feat=_profile_features(players,tour,p1,p2); p=_tpred4_predict(base,weights,feat); market=_f(row.get("market_p1")); brier=(p-y)**2; market_brier=((market-y)**2) if market is not None else None
        try: t3feat=json.loads(str(row.get("feature_json") or "{}"))
        except Exception: t3feat={}
        payload={"tpred3_probability":base,**feat,"tpred3_features":t3feat}; inserts.append((source_id,row.get("rich_match_id"),row.get("commence_time"),tour,row.get("surface"),p1,p2,p,market,y,brier,market_brier,int(row.get("evidence_ready") or 0),json.dumps(payload,sort_keys=True,default=str),TPRED4_MODEL_VERSION,utc_now_iso())); updates += 1; _tpred4_learn(weights,feat,p,y,updates)
        stats1,stats2=_extract_stats(row.get("stats_json")); rp1=_norm_name(row.get("rich_p1"))
        if rp1 and rp1 != _norm_name(p1) and rp1 == _norm_name(p2): stats1,stats2=stats2,stats1
        _update_profiles(players,tour,p1,p2,y,stats1,stats2,_dominance_magnitude(row.get("rich_result")))
        if len(inserts)>=1000:
            db.executemany("""INSERT INTO tennis_predictive4_walkforward(source_walkforward_id,rich_match_id,commence_time,tour,surface,player_one,player_two,predicted_p1,market_p1,outcome_p1,brier,market_brier,evidence_ready,feature_json,model_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_walkforward_id) DO NOTHING""",inserts); inserts=[]
    if inserts: db.executemany("""INSERT INTO tennis_predictive4_walkforward(source_walkforward_id,rich_match_id,commence_time,tour,surface,player_one,player_two,predicted_p1,market_p1,outcome_p1,brier,market_brier,evidence_ready,feature_json,model_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_walkforward_id) DO NOTHING""",inserts)
    if rows:
        _save_players(db,players); _set_state(db,"tennis_predictive4_state","last_source_walkforward_id",max_id); _set_state(db,"tennis_predictive4_state","weights_json",json.dumps(weights,sort_keys=True)); _set_state(db,"tennis_predictive4_state","learning_updates",updates)
    return {"ok":True,"processed":len(rows),"last_source_walkforward_id":max_id,"weights":weights}


def _tpred4_current_predictions(db):
    players=_load_players(db); weights=_tpred4_weights(db)
    try: rows=db.fetchall("SELECT * FROM tennis_predictive3_predictions WHERE model_version=? ORDER BY generated_at,event_id,selection",(tpred3.MODEL_VERSION,))
    except Exception as exc: return {"ok":False,"error":f"{type(exc).__name__}: {exc}","upserted":0}
    upserted=0
    for r in rows:
        selection=str(r.get("selection") or ""); opponent=str(r.get("opponent") or ""); tour=str(r.get("tour") or ""); base=float(r.get("probability") or 0.5); feat=_profile_features(players,tour,selection,opponent); p=_tpred4_predict(base,weights,feat); market=_f(r.get("market_probability")); edge=((p-market)*100.0) if market is not None else None; support=min(int(feat.get("p1_profile_matches") or 0),int(feat.get("p2_profile_matches") or 0)); confidence="A" if support>=25 and abs(p-0.5)>=0.12 else "B" if support>=15 else "C"; outcome=r.get("outcome"); brier=((p-int(outcome))**2) if outcome is not None else None; payload={"tpred3_probability":base,**feat}
        db.execute("""INSERT INTO tennis_predictive4_predictions(event_id,selection,opponent,generated_at,commence_time,tour,surface,probability,fair_odds,market_probability,model_edge_vs_market_pct,confidence,feature_json,status,outcome,brier,settled_at,model_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(model_version,event_id,selection) DO UPDATE SET opponent=excluded.opponent,commence_time=excluded.commence_time,tour=excluded.tour,surface=excluded.surface,probability=excluded.probability,fair_odds=excluded.fair_odds,market_probability=excluded.market_probability,model_edge_vs_market_pct=excluded.model_edge_vs_market_pct,confidence=excluded.confidence,feature_json=excluded.feature_json,status=excluded.status,outcome=excluded.outcome,brier=excluded.brier,settled_at=excluded.settled_at""",(r.get("event_id"),selection,opponent,r.get("generated_at"),r.get("commence_time"),tour,r.get("surface"),p,_fair_odds(p),market,edge,confidence,json.dumps(payload,sort_keys=True,default=str),r.get("status") or "OPEN",outcome,brier,r.get("settled_at"),TPRED4_MODEL_VERSION)); upserted += 1
    return {"ok":True,"upserted":upserted}


def tpred4_scoreboard(db):
    hist=db.fetchall("SELECT * FROM tennis_predictive4_walkforward WHERE model_version=? AND evidence_ready=1",(TPRED4_MODEL_VERSION,)); market=[r for r in hist if _f(r.get("market_brier")) is not None]; current=db.fetchall("SELECT * FROM tennis_predictive4_predictions WHERE model_version=? ORDER BY commence_time",(TPRED4_MODEL_VERSION,))
    def avg(rows,key):
        vals=[float(r[key]) for r in rows if _f(r.get(key)) is not None]; return sum(vals)/len(vals) if vals else None
    mb=avg(market,"market_brier"); pb=avg(market,"brier")
    return {"version":VERSION,"model_version":TPRED4_MODEL_VERSION,"historical_evidence_ready":len(hist),"avg_brier":avg(hist,"brier"),"market_comparison_samples":len(market),"avg_market_brier":mb,"brier_advantage_vs_market":(mb-pb) if mb is not None and pb is not None else None,"open_predictions":sum(1 for r in current if str(r.get("status") or "")=="OPEN"),"settled_predictions":sum(1 for r in current if r.get("outcome") is not None),"weights":_tpred4_weights(db),"authority":"RESEARCH_ONLY_NO_EXECUTION"}


# Market timing -------------------------------------------------------------

def run_tennis_timing(db):
    last_id=int(_state(db,"tennis_predictive4_state","timing_last_odds_id") or 0)
    try: rows=db.fetchall("""SELECT o.*,e.commence_time,e.player_one,e.player_two FROM tennis_odds_snapshots o JOIN tennis_events e ON e.event_id=o.event_id WHERE o.id>? AND o.market_key='h2h' ORDER BY o.id""",(last_id,))
    except Exception as exc: return {"ok":False,"error":f"{type(exc).__name__}: {exc}","snapshots":0}
    groups=defaultdict(list); max_id=last_id
    for r in rows: max_id=max(max_id,int(r.get("id") or 0)); groups[(str(r.get("event_id")),str(r.get("captured_at")),str(r.get("capture_mode") or "UNKNOWN"))].append(dict(r))
    written=0
    for (event_id,captured_at,mode),items in groups.items():
        first=items[0]; players=(str(first.get("player_one") or ""),str(first.get("player_two") or "")); by_book=defaultdict(dict)
        for r in items:
            book=str(r.get("bookmaker_key") or "")
            if not book or "_ex_" in book: continue
            try: by_book[book][str(r.get("selection") or "")]=float(r.get("price"))
            except Exception: pass
        metrics=_market_metrics(by_book,players)
        if not metrics: continue
        cap_dt,start_dt=_dt(captured_at),_dt(first.get("commence_time")); hours=((start_dt-cap_dt).total_seconds()/3600.0) if cap_dt and start_dt else None
        for selection in players:
            m=metrics["players"].get(selection) or {}; prob=_f(m.get("fair_probability"))
            if prob is None: continue
            db.execute("""INSERT INTO tennis_market_timing_snapshots(event_id,selection,captured_at,commence_time,hours_to_start,fair_probability,fair_odds,num_books,price_dispersion_pct,mean_overround_pct,capture_source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id,selection,captured_at,capture_source) DO NOTHING""",(event_id,selection,captured_at,first.get("commence_time"),hours,prob,_fair_odds(prob),int(metrics.get("num_books") or 0),m.get("price_dispersion_pct"),metrics.get("mean_overround_pct"),mode,utc_now_iso())); written += 1
    if rows: _set_state(db,"tennis_predictive4_state","timing_last_odds_id",max_id)
    return {"ok":True,"odds_rows_seen":len(rows),"capture_waves":len(groups),"snapshots_attempted":written,"provider_calls":0}


def _timing_bucket(hours):
    h=_f(hours)
    if h is None: return "UNKNOWN"
    if h<=1:return "T-1H"
    if h<=6:return "T-6H"
    if h<=12:return "T-12H"
    if h<=24:return "T-24H"
    if h<=48:return "T-48H"
    if h<=72:return "T-72H"
    return "EARLIER"


def timing_scoreboard(db):
    rows=db.fetchall("SELECT * FROM tennis_market_timing_snapshots ORDER BY event_id,selection,captured_at"); grouped=defaultdict(list)
    for r in rows: grouped[(str(r.get("event_id")),str(r.get("selection")))].append(r)
    buckets=defaultdict(list)
    for items in grouped.values():
        valid=[r for r in items if _f(r.get("hours_to_start")) is not None and float(r.get("hours_to_start"))>=0]
        if not valid: continue
        close=min(valid,key=lambda r:float(r.get("hours_to_start"))); close_odds=_f(close.get("fair_odds"))
        if close_odds is None: continue
        for r in valid:
            odds=_f(r.get("fair_odds"))
            if odds is not None: buckets[_timing_bucket(r.get("hours_to_start"))].append((odds/close_odds-1.0)*100.0)
    summary={k:{"snapshots":len(v),"avg_move_to_final_pct":sum(v)/len(v) if v else None,"positive_move_pct":sum(1 for x in v if x>0)/len(v)*100.0 if v else None} for k,v in buckets.items()}
    return {"snapshots":len(rows),"selection_series":len(grouped),"horizons":summary,"provider_calls":0,"authority":"RESEARCH_ONLY_NO_EXECUTION"}


# Tennis Winner Discovery ---------------------------------------------------

def _num_band(v,cuts,default):
    for bound,label in cuts:
        if v < bound: return label
    return default


def _tennis_features(row,orientation=1):
    try: payload=json.loads(str(row.get("feature_json") or "{}"))
    except Exception: payload={}
    p=float(row.get("predicted_p1") if "predicted_p1" in row else row.get("probability") or 0.5); p=p if orientation>0 else 1.0-p; serve=float(payload.get("opp_adj_serve_diff") or 0.0)*orientation; ret=float(payload.get("opp_adj_return_diff") or 0.0)*orientation; dom=float(payload.get("dominance_diff") or 0.0)*orientation; market=_f(row.get("market_p1") if "market_p1" in row else row.get("market_probability")); market=(1.0-market) if market is not None and orientation<0 else market; gap=(p-market)*100.0 if market is not None else 0.0
    pb=_num_band(p,((.55,"50-54"),(.60,"55-59"),(.65,"60-64"),(.70,"65-69"),(.75,"70-74"),(.80,"75-79")),"80+")
    def dband(x): return _num_band(x,((-.15,"<-15"),(-.05,"-15--5"),(.05,"-5-5"),(.15,"5-15")),"15+")
    def gband(x): return _num_band(x,((-5,"<-5"),(0,"-5-0"),(3,"0-3"),(7,"3-7")),"7+")
    tour=str(row.get("tour") or "").upper(); surface=str(row.get("surface") or "UNKNOWN").upper()
    return {"tour":tour,"surface":surface,"prob_band":pb,"serve_edge_band":dband(serve),"return_edge_band":dband(ret),"dominance_edge_band":dband(dom),"market_gap_band":gband(gap),"tour_surface":f"{tour}|{surface}","surface_prob":f"{surface}|{pb}","serve_edge":serve,"return_edge":ret,"dominance_edge":dom,"market_gap_pct":gap}


def _train_tennis_w1(db,start):
    rows=db.fetchall("SELECT * FROM tennis_predictive4_walkforward WHERE model_version=? AND evidence_ready=1 AND commence_time<? ORDER BY commence_time,id",(TPRED4_MODEL_VERSION,start.isoformat())); counts=_blank_counts(TENNIS_SEGMENTS); briers=[]; correct=scored=0
    for r in rows:
        p1=float(r.get("predicted_p1")); orientation=1 if p1>=0.5 else -1; y=int(r.get("outcome_p1")); target=y if orientation==1 else 1-y; feat=_tennis_features(r,orientation); p,_,_=_score_counts(counts,feat,TENNIS_SEGMENTS)
        if int(counts["global"]["n"])>=25: briers.append((p-target)**2); correct += int((p>=.5)==bool(target)); scored += 1
        _update_counts(counts,feat,target,TENNIS_SEGMENTS)
    model={"model_version":TENNIS_W1_MODEL_VERSION,"trained_at":utc_now_iso(),"training_cutoff":start.isoformat(),"training_samples":len(rows),"counts":counts,"prequential":{"scored":scored,"brier":sum(briers)/len(briers) if briers else None,"accuracy_pct":correct/scored*100.0 if scored else None},"discoveries":_top_discoveries(counts,TENNIS_SEGMENTS)}; _set_state(db,"tennis_predictive4_state","winner_model_json",json.dumps(model,sort_keys=True,default=str)); return model


def _tennis_w1_model(db,start):
    raw=_state(db,"tennis_predictive4_state","winner_model_json")
    if raw:
        try:
            model=json.loads(raw)
            if model.get("model_version")==TENNIS_W1_MODEL_VERSION and model.get("training_cutoff")==start.isoformat(): return model
        except Exception: pass
    return _train_tennis_w1(db,start)


def run_tennis_w1(db):
    start=_ensure_forward_start(db,"tennis_predictive4_state","winner_forward_start_at"); model=_tennis_w1_model(db,start); counts=model.get("counts") or _blank_counts(TENNIS_SEGMENTS); rows=db.fetchall("SELECT * FROM tennis_predictive4_predictions WHERE model_version=? ORDER BY generated_at,id",(TPRED4_MODEL_VERSION,)); inserted=updated=0
    for r in rows:
        generated=_dt(r.get("generated_at"))
        if not generated or generated<start: continue
        key=f"{TPRED4_MODEL_VERSION}|{r.get('event_id')}|{r.get('selection')}"; existing=db.fetchone("SELECT id,status FROM tennis_winner_discovery_samples WHERE sample_key=?",(key,)); feat=_tennis_features(r,1)
        if not existing:
            p,support,contrib=_score_counts(counts,feat,TENNIS_SEGMENTS); confidence="HIGH" if support>=100 else "MEDIUM" if support>=30 else "LOW"; outcome=r.get("outcome"); brier=(p-int(outcome))**2 if outcome is not None else None
            db.execute("""INSERT INTO tennis_winner_discovery_samples(sample_key,evidence_mode,event_id,selection,opponent,generated_at,commence_time,tour,surface,tpred4_probability,winner_probability,segment_support,confidence,feature_json,status,outcome,brier,settled_at,model_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(key,"FORWARD",r.get("event_id"),r.get("selection"),r.get("opponent"),r.get("generated_at"),r.get("commence_time"),r.get("tour"),r.get("surface"),r.get("probability"),p,support,confidence,json.dumps({"features":feat,"segments":contrib},sort_keys=True,default=str),r.get("status") or "OPEN",outcome,brier,r.get("settled_at"),TENNIS_W1_MODEL_VERSION)); inserted += 1
        elif r.get("outcome") is not None and str(existing.get("status") or "")!="SETTLED":
            sample=db.fetchone("SELECT winner_probability FROM tennis_winner_discovery_samples WHERE id=?",(existing["id"],)) or {}; p=float(sample.get("winner_probability") or .5); db.execute("UPDATE tennis_winner_discovery_samples SET status='SETTLED',outcome=?,brier=?,settled_at=? WHERE id=?",(int(r.get("outcome")),(p-int(r.get("outcome")))**2,r.get("settled_at") or utc_now_iso(),existing["id"])); updated += 1
    return {"ok":True,"inserted":inserted,"updated":updated,"training_samples":model.get("training_samples"),"betting_authority":False}


def tennis_winner_scoreboard(db):
    start=_ensure_forward_start(db,"tennis_predictive4_state","winner_forward_start_at"); model=_tennis_w1_model(db,start); rows=db.fetchall("SELECT * FROM tennis_winner_discovery_samples WHERE model_version=? ORDER BY id",(TENNIS_W1_MODEL_VERSION,))
    def metrics(th=None):
        subset=[r for r in rows if th is None or float(r.get("winner_probability") or 0)>=th]; done=[r for r in subset if r.get("outcome") is not None]; wins=sum(int(r.get("outcome") or 0) for r in done); vals=[float(r.get("brier")) for r in done if _f(r.get("brier")) is not None]
        return {"samples":len(subset),"settled":len(done),"wins":wins,"hit_rate_pct":wins/len(done)*100.0 if done else None,"brier":mean(vals) if vals else None}
    return {"model_version":TENNIS_W1_MODEL_VERSION,"forward_start_at":start.isoformat(),"training_samples":model.get("training_samples",0),"prequential":model.get("prequential") or {},"forward":metrics(),"thresholds":{str(t):metrics(t) for t in (.60,.65,.70,.75,.80)},"historical_discoveries":model.get("discoveries") or [],"authority":"RESEARCH_ONLY_NO_EXECUTION"}


def run_all(db):
    summary={"version":VERSION,"ok":False}
    try:
        ensure_schema(db); summary["w1"]=run_w1(db); summary["tpred4_history"]=_process_tpred4_history(db); summary["tpred4_forward"]=_tpred4_current_predictions(db); summary["timing"]=run_tennis_timing(db); summary["tennis_w1"]=run_tennis_w1(db); summary["ok"]=True
        try: db.record_collector_run("ADAPTIVE_WINNER_RESEARCH_MAINT",True,detail=json.dumps(summary,sort_keys=True,default=str))
        except Exception: pass
    except Exception as exc:
        summary["error"]=f"{type(exc).__name__}: {exc}"
        try: db.record_collector_run("ADAPTIVE_WINNER_RESEARCH_MAINT",False,detail=summary["error"])
        except Exception: pass
    print("ADAPTIVE_WINNER_RESEARCH "+json.dumps(summary,sort_keys=True,default=str),flush=True); return summary


def patch_exports():
    try:
        import exporter; exporter.EXPORT_TABLES=tuple(dict.fromkeys(tuple(exporter.EXPORT_TABLES)+EXPORT_TABLES))
    except Exception: pass
    try:
        import split_exporter; split_exporter.WEEKLY_TABLES=tuple(dict.fromkeys(tuple(split_exporter.WEEKLY_TABLES)+EXPORT_TABLES))
    except Exception: pass


def _fmt(v,d=2,suffix=""):
    try: return f"{float(v):.{d}f}{suffix}"
    except Exception: return "—"


def _tone(v):
    try: return "good" if float(v)>0 else "badx" if float(v)<0 else ""
    except Exception: return ""


def _dashboard_html(db,base_style):
    w=winner_scoreboard(db); t=tpred4_scoreboard(db); tm=timing_scoreboard(db); tw=tennis_winner_scoreboard(db); wf=w["forward"]; twf=tw["forward"]
    discoveries="".join(f"<tr><td>{escape(str(x['feature']))}</td><td>{escape(str(x['value']))}</td><td>{x['n']}</td><td>{_fmt(x['hit_rate_pct'],1,'%')}</td><td>{_fmt(x['uplift_pp'],1,'pp')}</td></tr>" for x in (w.get("historical_discoveries") or [])[:10]) or "<tr><td colspan=5>No discovery segments yet.</td></tr>"
    tdisc="".join(f"<tr><td>{escape(str(x['feature']))}</td><td>{escape(str(x['value']))}</td><td>{x['n']}</td><td>{_fmt(x['hit_rate_pct'],1,'%')}</td><td>{_fmt(x['uplift_pp'],1,'pp')}</td></tr>" for x in (tw.get("historical_discoveries") or [])[:10]) or "<tr><td colspan=5>No tennis discovery segments yet.</td></tr>"
    horizons="".join(f"<tr><td>{escape(k)}</td><td>{v.get('snapshots',0)}</td><td>{_fmt(v.get('avg_move_to_final_pct'),2,'%')}</td><td>{_fmt(v.get('positive_move_pct'),1,'%')}</td></tr>" for k,v in (tm.get("horizons") or {}).items()) or "<tr><td colspan=4>No timing snapshots yet.</td></tr>"
    return f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Adaptive Winner Research</title><style>{base_style}.table-wrap{{overflow-x:auto}}body{{max-width:1500px;margin:auto}}</style></head><body><a href='/'>← Betting Lab</a><h1>Adaptive Winner Research <span class='pill'>SHADOW · NO EXECUTION AUTHORITY</span></h1><p class='muted'>Winner identification is separated from price/staking. Historical discovery is never counted as forward validation.</p><div class='grid compact'><div class='card metric-card'><div class='label'>W1 forward candidates</div><div class='value'>{wf.get('samples',0)}</div><div class='muted'>settled {wf.get('settled',0)} · hit {_fmt(wf.get('hit_rate_pct'),1,'%')}</div></div><div class='card metric-card'><div class='label'>W1 prequential Brier</div><div class='value'>{_fmt((w.get('prequential') or {}).get('brier'),4)}</div><div class='muted'>history scored before each update</div></div><div class='card metric-card'><div class='label'>TPRED4 evidence-ready</div><div class='value'>{t.get('historical_evidence_ready',0)}</div><div class='muted'>Brier {_fmt(t.get('avg_brier'),4)} · market {_fmt(t.get('avg_market_brier'),4)}</div></div><div class='card metric-card'><div class='label'>Tennis W1 forward</div><div class='value'>{twf.get('samples',0)}</div><div class='muted'>settled {twf.get('settled',0)} · hit {_fmt(twf.get('hit_rate_pct'),1,'%')}</div></div></div><div class='panel'><h2>Cross-sport winner fingerprints — discovery only</h2><div class='table-wrap'><table><thead><tr><th>Feature</th><th>Value</th><th>N</th><th>Shrunk hit rate</th><th>Uplift</th></tr></thead><tbody>{discoveries}</tbody></table></div></div><div class='panel'><h2>TPRED4</h2><p>Opponent-adjusted serve/return + dominance challenger. TPRED3 remains untouched.</p><p>Market comparisons <strong>{t.get('market_comparison_samples',0)}</strong> · Brier advantage vs market <strong class='{_tone(t.get('brier_advantage_vs_market'))}'>{_fmt(t.get('brier_advantage_vs_market'),4)}</strong> · open forecasts {t.get('open_predictions',0)}</p></div><div class='panel'><h2>Tennis Winner Discovery — discovery only</h2><div class='table-wrap'><table><thead><tr><th>Feature</th><th>Value</th><th>N</th><th>Shrunk hit rate</th><th>Uplift</th></tr></thead><tbody>{tdisc}</tbody></table></div></div><div class='panel'><h2>Tennis market timing</h2><p>{tm.get('snapshots',0)} normalized snapshots from stored breadth + convergence waves. Zero additional provider calls.</p><div class='table-wrap'><table><thead><tr><th>Horizon</th><th>Snapshots</th><th>Avg move to final</th><th>Positive move</th></tr></thead><tbody>{horizons}</tbody></table></div></div><p class='muted'>Models: {escape(W1_MODEL_VERSION)} · {escape(TPRED4_MODEL_VERSION)} · {escape(TENNIS_W1_MODEL_VERSION)}.</p></body></html>"""


def _surface_home_link(app):
    route=next((r for r in list(app.router.routes) if getattr(r,"path",None)=="/" and "GET" in (getattr(r,"methods",set()) or set())),None)
    if route is None: return False
    original=route.endpoint; name=route.name; include=getattr(route,"include_in_schema",True); app.router.routes.remove(route)
    async def wrapped():
        response=original(); import inspect
        if inspect.isawaitable(response): response=await response
        if not isinstance(response,HTMLResponse): return response
        html=response.body.decode("utf-8")
        if "href='/adaptive-research'" not in html:
            link="<a href='/adaptive-research'>Adaptive Winner Research</a>"; marker="<a href='/tennis-predictive'>Tennis Predictive</a>"; html=html.replace(marker,marker+link,1) if marker in html else html.replace("<div class='nav'>","<div class='nav'>"+link,1)
        headers={k:v for k,v in response.headers.items() if k.lower()!="content-length"}; return HTMLResponse(content=html,status_code=response.status_code,headers=headers)
    app.add_api_route("/",wrapped,methods=["GET"],response_class=HTMLResponse,name=name,include_in_schema=include); return True


def install(app,db,base_style,tennis_engine=None):
    global _INSTALLED
    if _INSTALLED: return {"installed":True,"already_installed":True,"version":VERSION}
    ensure_schema(db); patch_exports()
    @app.get("/api/winner-discovery/status")
    def api_winner_discovery(): return winner_scoreboard(db)
    @app.get("/api/tennis-pred4/status")
    def api_tpred4(): return tpred4_scoreboard(db)
    @app.get("/api/tennis-market-timing/status")
    def api_timing(): return timing_scoreboard(db)
    @app.get("/api/tennis-winner-discovery/status")
    def api_tennis_winner(): return tennis_winner_scoreboard(db)
    @app.post("/admin/adaptive-research/run")
    def admin_run(): return run_all(db)
    @app.get("/adaptive-research")
    def dashboard(): return HTMLResponse(_dashboard_html(db,base_style))
    wrapped_runtime=False
    if tennis_engine is not None and not getattr(tennis_engine,"_adaptive_winner_research_installed",False):
        original=tennis_engine.one_cycle
        def wrapped_one_cycle(*args,**kwargs):
            result=original(*args,**kwargs); adaptive=run_all(db)
            if isinstance(result,dict): result=dict(result); result["adaptive_winner_research"]=adaptive
            return result
        tennis_engine.one_cycle=wrapped_one_cycle; tennis_engine._adaptive_winner_research_installed=True; wrapped_runtime=True
    home_link=_surface_home_link(app); _INSTALLED=True
    return {"installed":True,"already_installed":False,"version":VERSION,"dashboard_version":DASHBOARD_VERSION,"runtime_wrapped":wrapped_runtime,"home_link":home_link,"exports":list(EXPORT_TABLES),"provider_calls_added":0,"betting_authority":False}
