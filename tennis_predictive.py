from __future__ import annotations

"""TPRED1/TPRED2 research-only tennis prediction lab.

TPRED1 is a pure pre-market model built only from previously completed matches.
TPRED2 is a market-aware challenger that blends the frozen TPRED1 probability
with the latest pre-match de-vigged consensus. Neither model can place bets.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

MODEL1 = "TPRED1"
MODEL1_VERSION = "TPRED1_ELO_FORM_V1"
MODEL2 = "TPRED2"
MODEL2_VERSION = "TPRED2_MARKET_BLEND_V1"
FORWARD_HOURS = 168.0
MARKET_WEIGHT = 0.65
PURE_WEIGHT = 0.35


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(value: Any) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clip(p: float, lo: float = 0.02, hi: float = 0.98) -> float:
    return min(hi, max(lo, float(p)))


def _sigmoid(z: float) -> float:
    if z >= 0:
        e = math.exp(-z)
        return 1.0 / (1.0 + e)
    e = math.exp(z)
    return e / (1.0 + e)


def _logit(p: float) -> float:
    p = _clip(p, 1e-6, 1 - 1e-6)
    return math.log(p / (1.0 - p))


def _elo_probability(r1: float, r2: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((r2 - r1) / 400.0))


def _brier(p: float, y: int) -> float:
    return (float(p) - int(y)) ** 2


def _log_loss(p: float, y: int) -> float:
    p = _clip(p, 1e-9, 1 - 1e-9)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def _fair_odds(p: float) -> float:
    return 1.0 / _clip(p, 1e-6, 1 - 1e-6)


def install_schema(db) -> Dict[str, Any]:
    id_col = "BIGSERIAL PRIMARY KEY" if getattr(db, "is_postgres", False) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ddl = (
        """
        CREATE TABLE IF NOT EXISTS tennis_predictive_state (
            singleton_id INTEGER PRIMARY KEY,
            model1_version TEXT NOT NULL,
            model2_version TEXT NOT NULL,
            last_result_count INTEGER NOT NULL DEFAULT 0,
            last_result_at TEXT,
            last_walkforward_at TEXT,
            last_maintenance_at TEXT
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS tennis_predictive_feature_snapshots (
            id {id_col},
            captured_at TEXT NOT NULL,
            player TEXT NOT NULL,
            tour TEXT,
            surface TEXT,
            ranking INTEGER,
            ranking_points REAL,
            serve_points_won_pct REAL,
            return_points_won_pct REAL,
            first_serve_win_pct REAL,
            second_serve_win_pct REAL,
            ace_rate REAL,
            double_fault_rate REAL,
            break_points_saved_pct REAL,
            source TEXT NOT NULL,
            metadata_json TEXT
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS tennis_predictive_walkforward (
            id {id_col},
            model_name TEXT NOT NULL,
            model_version TEXT NOT NULL,
            event_id TEXT NOT NULL,
            commence_time TEXT NOT NULL,
            tour TEXT NOT NULL,
            tournament_title TEXT,
            tournament_level TEXT,
            player_one TEXT NOT NULL,
            player_two TEXT NOT NULL,
            predicted_p1 REAL NOT NULL,
            market_p1 REAL,
            outcome_p1 INTEGER NOT NULL,
            brier REAL NOT NULL,
            log_loss REAL NOT NULL,
            market_brier REAL,
            model_edge_vs_market_pct REAL,
            evidence_ready INTEGER NOT NULL DEFAULT 0,
            p1_elo REAL,
            p2_elo REAL,
            p1_form10 REAL,
            p2_form10 REAL,
            p1_matches_7d INTEGER,
            p2_matches_7d INTEGER,
            p1_prior_matches INTEGER,
            p2_prior_matches INTEGER,
            feature_json TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(model_name,event_id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS tennis_predictive_predictions (
            id {id_col},
            model_name TEXT NOT NULL,
            model_version TEXT NOT NULL,
            event_id TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            commence_time TEXT NOT NULL,
            tour TEXT NOT NULL,
            tournament_title TEXT,
            tournament_level TEXT,
            selection TEXT NOT NULL,
            opponent TEXT NOT NULL,
            probability REAL NOT NULL,
            fair_odds REAL NOT NULL,
            market_probability REAL,
            market_fair_odds REAL,
            model_edge_vs_market_pct REAL,
            confidence_tier TEXT NOT NULL,
            feature_json TEXT,
            status TEXT NOT NULL DEFAULT 'OPEN',
            outcome INTEGER,
            brier REAL,
            log_loss REAL,
            settled_at TEXT,
            UNIQUE(model_name,event_id,selection)
        )
        """,
    )
    for sql in ddl:
        db.execute(sql)
    db.execute(
        """
        INSERT INTO tennis_predictive_state(
            singleton_id,model1_version,model2_version,last_result_count
        ) VALUES(1,?,?,0)
        ON CONFLICT(singleton_id) DO UPDATE SET
            model1_version=excluded.model1_version,
            model2_version=excluded.model2_version
        """,
        (MODEL1_VERSION, MODEL2_VERSION),
    )
    return {"installed": True, "model1": MODEL1_VERSION, "model2": MODEL2_VERSION, "betting_authority": False}


def _history(db, before: Optional[datetime] = None) -> List[Dict[str, Any]]:
    params: Tuple[Any, ...] = ()
    where = ""
    if before is not None:
        where = " AND e.commence_time<?"
        params = (_iso(before),)
    rows = db.fetchall(
        f"""
        SELECT e.event_id,e.sport_key,e.tournament_title,e.tour,e.tournament_level,
               e.commence_time,e.player_one,e.player_two,r.winner,r.completed_at
        FROM tennis_results r
        JOIN tennis_events e ON e.event_id=r.event_id
        WHERE r.winner IS NOT NULL {where}
        ORDER BY e.commence_time ASC,e.event_id ASC
        """,
        params,
    )
    out = []
    for r in rows:
        if str(r.get("winner") or "") in {str(r.get("player_one") or ""), str(r.get("player_two") or "")}:
            out.append(dict(r))
    return out


def _market_probability(db, event_id: str, selection: str, at: datetime) -> Optional[Dict[str, Any]]:
    try:
        return db.fetchone(
            """
            SELECT fair_probability,fair_odds,num_books,price_dispersion_pct,
                   mean_overround_pct,captured_at
            FROM tennis_consensus_snapshots
            WHERE event_id=? AND selection=? AND captured_at<=?
            ORDER BY captured_at DESC,id DESC LIMIT 1
            """,
            (event_id, selection, _iso(at)),
        )
    except Exception:
        return None


def _new_state() -> Dict[str, Any]:
    return {"players": {}, "h2h": defaultdict(lambda: [0, 0])}


def _player(state: Dict[str, Any], name: str) -> Dict[str, Any]:
    players = state["players"]
    if name not in players:
        players[name] = {"elo": 1500.0, "matches": [], "last": None}
    return players[name]


def _recent_form(player: Mapping[str, Any], n: int = 10) -> float:
    matches = list(player.get("matches") or [])[-n:]
    wins = sum(int(x[1]) for x in matches)
    return (wins + 2.0) / (len(matches) + 4.0)


def _matches_7d(player: Mapping[str, Any], at: datetime) -> int:
    cutoff = at - timedelta(days=7)
    return sum(1 for dt, _ in player.get("matches") or [] if cutoff <= dt < at)


def _rest_days(player: Mapping[str, Any], at: datetime) -> Optional[float]:
    last = player.get("last")
    if not last:
        return None
    return max(0.0, (at - last).total_seconds() / 86400.0)


def _h2h_prob(state: Dict[str, Any], p1: str, p2: str) -> Tuple[float, int]:
    if p1 <= p2:
        key, idx = (p1, p2), 0
    else:
        key, idx = (p2, p1), 1
    wins = state["h2h"][key]
    meetings = wins[0] + wins[1]
    p1wins = wins[idx]
    return (p1wins + 1.0) / (meetings + 2.0), meetings


def _features(state: Dict[str, Any], p1: str, p2: str, at: datetime) -> Dict[str, Any]:
    a, b = _player(state, p1), _player(state, p2)
    h2h, h2h_n = _h2h_prob(state, p1, p2)
    return {
        "p1_elo": float(a["elo"]),
        "p2_elo": float(b["elo"]),
        "p1_form10": _recent_form(a),
        "p2_form10": _recent_form(b),
        "p1_matches_7d": _matches_7d(a, at),
        "p2_matches_7d": _matches_7d(b, at),
        "p1_rest_days": _rest_days(a, at),
        "p2_rest_days": _rest_days(b, at),
        "p1_prior_matches": len(a["matches"]),
        "p2_prior_matches": len(b["matches"]),
        "h2h_p1": h2h,
        "h2h_meetings": h2h_n,
    }


def _predict_p1(features: Mapping[str, Any]) -> float:
    base = _elo_probability(float(features["p1_elo"]), float(features["p2_elo"]))
    z = _logit(base)
    z += 0.90 * (float(features["p1_form10"]) - float(features["p2_form10"]))
    z -= 0.08 * (int(features["p1_matches_7d"]) - int(features["p2_matches_7d"]))
    r1, r2 = features.get("p1_rest_days"), features.get("p2_rest_days")
    if r1 is not None and float(r1) < 1.5:
        z -= 0.08
    if r2 is not None and float(r2) < 1.5:
        z += 0.08
    z += 0.35 * ((float(features["h2h_p1"]) - 0.5) * 2.0)
    return _clip(_sigmoid(z), 0.05, 0.95)


def _update_state(state: Dict[str, Any], row: Mapping[str, Any]) -> None:
    p1, p2 = str(row["player_one"]), str(row["player_two"])
    at = _parse(row["commence_time"])
    winner = str(row["winner"])
    a, b = _player(state, p1), _player(state, p2)
    outcome = 1 if winner == p1 else 0
    expected = _elo_probability(float(a["elo"]), float(b["elo"]))
    k = 32.0 if min(len(a["matches"]), len(b["matches"])) < 20 else 24.0
    delta = k * (outcome - expected)
    a["elo"] = float(a["elo"]) + delta
    b["elo"] = float(b["elo"]) - delta
    a["matches"].append((at, outcome))
    b["matches"].append((at, 1 - outcome))
    a["last"] = at
    b["last"] = at
    if p1 <= p2:
        key, idx = (p1, p2), 0
    else:
        key, idx = (p2, p1), 1
    state["h2h"][key][idx if outcome else 1 - idx] += 1


def _confidence(features: Mapping[str, Any]) -> str:
    n = min(int(features["p1_prior_matches"]), int(features["p2_prior_matches"]))
    if n >= 20:
        return "A"
    if n >= 10:
        return "B"
    if n >= 5:
        return "C"
    return "WATCH"


def _market_blend(pure_p: float, market_p: float) -> float:
    z = PURE_WEIGHT * _logit(pure_p) + MARKET_WEIGHT * _logit(market_p)
    return _clip(_sigmoid(z), 0.03, 0.97)


def rebuild_walkforward(db) -> Dict[str, Any]:
    db.execute("DELETE FROM tennis_predictive_walkforward")
    rows = _history(db)
    state = _new_state()
    created = {MODEL1: 0, MODEL2: 0}
    now_iso = _iso(_now())
    for row in rows:
        at = _parse(row["commence_time"])
        p1, p2 = str(row["player_one"]), str(row["player_two"])
        y = 1 if str(row["winner"]) == p1 else 0
        feat = _features(state, p1, p2, at)
        pure = _predict_p1(feat)
        ready = int(min(int(feat["p1_prior_matches"]), int(feat["p2_prior_matches"])) >= 3)
        market = _market_probability(db, row["event_id"], p1, at)
        market_p = float(market["fair_probability"]) if market and market.get("fair_probability") is not None else None

        def insert(model: str, version: str, p: float, market_value: Optional[float]) -> None:
            db.execute(
                """
                INSERT INTO tennis_predictive_walkforward(
                    model_name,model_version,event_id,commence_time,tour,tournament_title,
                    tournament_level,player_one,player_two,predicted_p1,market_p1,outcome_p1,
                    brier,log_loss,market_brier,model_edge_vs_market_pct,evidence_ready,
                    p1_elo,p2_elo,p1_form10,p2_form10,p1_matches_7d,p2_matches_7d,
                    p1_prior_matches,p2_prior_matches,feature_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    model,version,row["event_id"],row["commence_time"],row.get("tour") or "TENNIS",
                    row.get("tournament_title"),row.get("tournament_level"),p1,p2,p,market_value,y,
                    _brier(p,y),_log_loss(p,y),
                    _brier(market_value,y) if market_value is not None else None,
                    ((p-market_value)*100.0) if market_value is not None else None,
                    ready,feat["p1_elo"],feat["p2_elo"],feat["p1_form10"],feat["p2_form10"],
                    feat["p1_matches_7d"],feat["p2_matches_7d"],feat["p1_prior_matches"],
                    feat["p2_prior_matches"],json.dumps(feat,separators=(",",":")),now_iso,
                ),
            )
            created[model] += 1

        insert(MODEL1, MODEL1_VERSION, pure, market_p)
        if market_p is not None:
            insert(MODEL2, MODEL2_VERSION, _market_blend(pure, market_p), market_p)
        _update_state(state, row)

    last = rows[-1]["commence_time"] if rows else None
    db.execute(
        """
        UPDATE tennis_predictive_state
        SET last_result_count=?,last_result_at=?,last_walkforward_at=?
        WHERE singleton_id=1
        """,
        (len(rows), last, now_iso),
    )
    return {"results": len(rows), "created": created}


def _state_from_history(db, at: datetime) -> Dict[str, Any]:
    state = _new_state()
    for row in _history(db, before=at):
        _update_state(state, row)
    return state


def create_forward_predictions(db, now: Optional[datetime] = None) -> Dict[str, int]:
    now = now or _now()
    state = _state_from_history(db, now)
    cutoff = now + timedelta(hours=FORWARD_HOURS)
    events = db.fetchall(
        """
        SELECT * FROM tennis_events
        WHERE status='UPCOMING' AND commence_time>? AND commence_time<=?
        ORDER BY commence_time,event_id
        """,
        (_iso(now), _iso(cutoff)),
    )
    created = {MODEL1: 0, MODEL2: 0}
    for event in events:
        at = _parse(event["commence_time"])
        p1, p2 = str(event["player_one"]), str(event["player_two"])
        feat = _features(state, p1, p2, at)
        pure = _predict_p1(feat)
        tier = _confidence(feat)
        market = _market_probability(db, event["event_id"], p1, now)
        market_p = float(market["fair_probability"]) if market and market.get("fair_probability") is not None else None

        def insert(model: str, version: str, sel: str, opp: str, p: float, mp: Optional[float]) -> None:
            changed = db.execute(
                """
                INSERT INTO tennis_predictive_predictions(
                    model_name,model_version,event_id,generated_at,commence_time,tour,
                    tournament_title,tournament_level,selection,opponent,probability,fair_odds,
                    market_probability,market_fair_odds,model_edge_vs_market_pct,confidence_tier,
                    feature_json,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN')
                ON CONFLICT(model_name,event_id,selection) DO NOTHING
                """,
                (
                    model,version,event["event_id"],_iso(now),event["commence_time"],
                    event.get("tour") or "TENNIS",event.get("tournament_title"),
                    event.get("tournament_level"),sel,opp,p,_fair_odds(p),mp,
                    _fair_odds(mp) if mp is not None else None,
                    ((p-mp)*100.0) if mp is not None else None,tier,
                    json.dumps(feat,separators=(",",":")),
                ),
            )
            if changed:
                created[model] += 1

        insert(MODEL1, MODEL1_VERSION, p1, p2, pure, market_p)
        insert(MODEL1, MODEL1_VERSION, p2, p1, 1.0-pure, (1.0-market_p) if market_p is not None else None)
        if market_p is not None:
            blend = _market_blend(pure, market_p)
            insert(MODEL2, MODEL2_VERSION, p1, p2, blend, market_p)
            insert(MODEL2, MODEL2_VERSION, p2, p1, 1.0-blend, 1.0-market_p)
    return created


def settle_forward_predictions(db) -> int:
    rows = db.fetchall(
        """
        SELECT p.id,p.selection,p.probability,r.winner,r.completed_at,r.fetched_at
        FROM tennis_predictive_predictions p
        JOIN tennis_results r ON r.event_id=p.event_id
        WHERE p.status='OPEN'
        ORDER BY p.id
        """
    )
    updated = 0
    for row in rows:
        y = 1 if str(row["selection"]) == str(row["winner"]) else 0
        db.execute(
            """
            UPDATE tennis_predictive_predictions
            SET status='SETTLED',outcome=?,brier=?,log_loss=?,settled_at=?
            WHERE id=?
            """,
            (y,_brier(float(row["probability"]),y),_log_loss(float(row["probability"]),y),
             row.get("completed_at") or row.get("fetched_at") or _iso(_now()),row["id"]),
        )
        updated += 1
    return updated


def run_maintenance(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    install_schema(db)
    now = now or _now()
    count_row = db.fetchone("SELECT COUNT(*) AS n,MAX(e.commence_time) AS last_at FROM tennis_results r JOIN tennis_events e ON e.event_id=r.event_id") or {}
    result_count = int(count_row.get("n") or 0)
    state = db.fetchone("SELECT * FROM tennis_predictive_state WHERE singleton_id=1") or {}
    walkforward = {"rebuilt": False}
    if result_count != int(state.get("last_result_count") or 0):
        walkforward = rebuild_walkforward(db)
        walkforward["rebuilt"] = True
    created = create_forward_predictions(db, now)
    settled = settle_forward_predictions(db)
    db.execute("UPDATE tennis_predictive_state SET last_maintenance_at=? WHERE singleton_id=1", (_iso(now),))
    return {
        "ok": True,
        "walkforward": walkforward,
        "forward_created": created,
        "forward_settled": settled,
        "provider_credits": 0,
        "betting_authority": False,
    }


def _metrics(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    rows = list(rows)
    if not rows:
        return {"samples": 0, "accuracy_pct": None, "brier": None, "log_loss": None}
    acc = sum(1 for r in rows if (float(r["predicted_p1"]) >= 0.5) == bool(int(r["outcome_p1"])))
    brier = sum(float(r["brier"]) for r in rows) / len(rows)
    ll = sum(float(r["log_loss"]) for r in rows) / len(rows)
    mkt = [r for r in rows if r.get("market_brier") is not None]
    market_brier = sum(float(r["market_brier"]) for r in mkt) / len(mkt) if mkt else None
    return {
        "samples": len(rows),
        "accuracy_pct": acc / len(rows) * 100.0,
        "brier": brier,
        "log_loss": ll,
        "market_samples": len(mkt),
        "market_brier": market_brier,
        "brier_improvement_vs_market_pct": (
            (market_brier - brier) / market_brier * 100.0
            if market_brier and market_brier > 0 and len(mkt) == len(rows) else None
        ),
    }


def scoreboard(db) -> Dict[str, Any]:
    install_schema(db)
    wf = db.fetchall("SELECT * FROM tennis_predictive_walkforward ORDER BY commence_time,id")
    pred = db.fetchall("SELECT * FROM tennis_predictive_predictions ORDER BY generated_at,id")
    state = db.fetchone("SELECT * FROM tennis_predictive_state WHERE singleton_id=1") or {}
    models: Dict[str, Any] = {}
    for model in (MODEL1, MODEL2):
        rows = [r for r in wf if str(r.get("model_name")) == model]
        ready = [r for r in rows if int(r.get("evidence_ready") or 0) == 1]
        by_tour = {}
        for tour in ("ATP","WTA","TENNIS"):
            tour_rows = [r for r in ready if str(r.get("tour") or "") == tour]
            if tour_rows:
                by_tour[tour] = _metrics(tour_rows)
        models[model] = {
            "all_walkforward": _metrics(rows),
            "evidence_ready_walkforward": _metrics(ready),
            "by_tour": by_tour,
            "forward_open": sum(1 for r in pred if r.get("model_name")==model and r.get("status")=="OPEN"),
            "forward_settled": sum(1 for r in pred if r.get("model_name")==model and r.get("status")=="SETTLED"),
        }
    feature_count = int((db.fetchone("SELECT COUNT(*) AS n FROM tennis_predictive_feature_snapshots") or {}).get("n") or 0)
    disagreements = []
    for r in pred:
        if r.get("status") != "OPEN" or r.get("model_edge_vs_market_pct") is None:
            continue
        disagreements.append(dict(r))
    disagreements.sort(key=lambda r: abs(float(r.get("model_edge_vs_market_pct") or 0)), reverse=True)
    return {
        "model1_version": MODEL1_VERSION,
        "model2_version": MODEL2_VERSION,
        "models": models,
        "feature_snapshot_rows": feature_count,
        "rich_feature_status": "WAITING_FOR_SOURCE" if feature_count == 0 else "COLLECTING",
        "last_maintenance_at": state.get("last_maintenance_at"),
        "last_walkforward_at": state.get("last_walkforward_at"),
        "historical_results": int(state.get("last_result_count") or 0),
        "top_forward_disagreements": disagreements[:12],
        "betting_authority": False,
        "provider_credits": 0,
    }


def _band(value: float, cuts: List[Tuple[float,float,str]]) -> Optional[str]:
    for lo, hi, label in cuts:
        if lo <= value < hi:
            return label
    return None


def pocket_report(db) -> Dict[str, Any]:
    rows = db.fetchall("SELECT * FROM tennis_execution_bets WHERE status='SETTLED' ORDER BY created_at,id")
    def aggregate(items: List[Mapping[str, Any]]) -> Dict[str, Any]:
        settled = [r for r in items if r.get("pnl_units") is not None or r.get("net_pnl_units") is not None]
        pnl = sum(float(r.get("net_pnl_units") if r.get("net_pnl_units") is not None else r.get("pnl_units") or 0) for r in settled)
        clv = [float(r["clv_pct"]) for r in settled if r.get("clv_pct") is not None and str(r.get("clv_quality") or "") in {"A","B"}]
        return {
            "settled": len(settled),
            "net_pnl_units": pnl,
            "net_roi_pct": pnl / len(settled) * 100.0 if settled else None,
            "ab_clv_samples": len(clv),
            "avg_ab_clv_pct": sum(clv)/len(clv) if clv else None,
        }
    odds_cuts = [(1.5,2.0,"1.50–1.99"),(2.0,2.5,"2.00–2.49"),(2.5,3.0,"2.50–2.99"),(3.0,4.0,"3.00–3.99"),(4.0,5.0,"4.00–4.99"),(5.0,7.5,"5.00–7.49"),(7.5,1e9,"7.50+")]
    edge_cuts = [(-1e9,7.5,"<7.5%"),(7.5,15.0,"7.5–14.99%"),(15.0,25.0,"15–24.99%"),(25.0,1e9,"25%+")]
    out: Dict[str,Any] = {"overall": aggregate(rows), "odds_bands": {}, "edge_bands": {}, "tours": {}, "reference_books": {}}
    for label in [x[2] for x in odds_cuts]:
        out["odds_bands"][label] = aggregate([r for r in rows if _band(float(r.get("offered_odds") or 0), odds_cuts)==label])
    for label in [x[2] for x in edge_cuts]:
        out["edge_bands"][label] = aggregate([r for r in rows if _band(float(r.get("edge_pct") or 0), edge_cuts)==label])
    try:
        enriched = db.fetchall(
            """
            SELECT b.*,e.tour FROM tennis_execution_bets b
            JOIN tennis_events e ON e.event_id=b.event_id
            WHERE b.status='SETTLED' ORDER BY b.created_at,b.id
            """
        )
    except Exception:
        enriched = rows
    for tour in ("ATP","WTA"):
        out["tours"][tour] = aggregate([r for r in enriched if str(r.get("tour") or "")==tour])
    out["reference_books"]["<16"] = aggregate([r for r in rows if int(r.get("reference_book_count") or 0) < 16])
    out["reference_books"]["16+"] = aggregate([r for r in rows if int(r.get("reference_book_count") or 0) >= 16])
    return out
