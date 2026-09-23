from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from statistics import mean
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

META_MODEL_VERSION = "META2_CLV_FROZEN_V1"
DEFAULT_HOLDOUT_FRACTION = 0.20
MIN_HOLDOUT = 40
L2 = 0.20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def _sigmoid(z: float) -> float:
    z = _clip(z, -35.0, 35.0)
    return 1.0 / (1.0 + math.exp(-z))


def _feature_dict(row: Mapping[str, Any]) -> Dict[str, float]:
    odds = _clip(_safe_float(row.get("offered_odds"), 2.0), 1.01, 25.0)
    hours = _clip(_safe_float(row.get("hours_to_kickoff"), 0.0), 0.0, 336.0)
    pair_gap_raw = row.get("paired_probability_gap_pp")
    pair_gap_missing = 1.0 if pair_gap_raw is None else 0.0
    chosen_raw = row.get("chosen_vs_median_pct")
    dispersion_raw = row.get("price_dispersion_pct")
    overround_raw = row.get("mean_overround_pct")
    same_top_raw = row.get("same_top_outcome")
    market = str(row.get("market_key") or "")
    odds_band = str(row.get("odds_band") or "")
    agreement = str(row.get("agreement_band") or "")
    return {
        "log_odds": math.log(odds),
        "model_probability": _clip(_safe_float(row.get("model_probability"), 0.0), 0.0, 1.0),
        "edge_pct": _clip(_safe_float(row.get("edge_pct"), 0.0), -20.0, 100.0),
        "log_hours_to_kickoff": math.log1p(hours),
        "bookmaker_count": _clip(_safe_float(row.get("bookmaker_count"), 0.0), 0.0, 40.0),
        "chosen_vs_median_pct": _clip(_safe_float(chosen_raw, 0.0), -50.0, 50.0),
        "chosen_vs_median_missing": 1.0 if chosen_raw is None else 0.0,
        "price_dispersion_pct": _clip(_safe_float(dispersion_raw, 0.0), 0.0, 100.0),
        "price_dispersion_missing": 1.0 if dispersion_raw is None else 0.0,
        "mean_overround_pct": _clip(_safe_float(overround_raw, 0.0), -20.0, 60.0),
        "overround_missing": 1.0 if overround_raw is None else 0.0,
        "pair_gap_abs_pp": _clip(abs(_safe_float(pair_gap_raw, 0.0)), 0.0, 50.0),
        "pair_gap_missing": pair_gap_missing,
        "paired_bet_exists": 1.0 if int(row.get("paired_bet_exists") or 0) else 0.0,
        "same_top_outcome": _safe_float(same_top_raw, 0.0),
        "same_top_missing": 1.0 if same_top_raw is None else 0.0,
        "uncertainty_score": _clip(_safe_float(row.get("uncertainty_score"), 0.5), 0.0, 1.0),
        "expected_total_goals": _clip(_safe_float(row.get("expected_total_goals"), 2.5), 0.0, 8.0),
        "log_training_support": math.log1p(_clip(_safe_float(row.get("training_support_matches"), 0.0), 0.0, 200.0)),
        "strong_candidate": 1.0 if int(row.get("strong_candidate") or 0) else 0.0,
        "source_pred2": 1.0 if str(row.get("source_model")) == "PRED2" else 0.0,
        "market_h2h": 1.0 if market == "h2h" else 0.0,
        "market_btts": 1.0 if market == "btts" else 0.0,
        "market_totals": 1.0 if market == "totals" else 0.0,
        "odds_lt2": 1.0 if odds_band == "<2" else 0.0,
        "odds_2_3": 1.0 if odds_band == "2-3" else 0.0,
        "odds_3_5": 1.0 if odds_band == "3-5" else 0.0,
        "odds_5_8": 1.0 if odds_band == "5-8" else 0.0,
        "odds_8plus": 1.0 if odds_band == "8+" else 0.0,
        "agree_tight": 1.0 if agreement == "TIGHT_<=1PP" else 0.0,
        "agree_close": 1.0 if agreement == "CLOSE_1-3PP" else 0.0,
        "agree_mixed": 1.0 if agreement == "MIXED_3-7.5PP" else 0.0,
        "agree_divergent": 1.0 if agreement == "DIVERGENT_>7.5PP" else 0.0,
        "agree_unpaired": 1.0 if agreement == "UNPAIRED" else 0.0,
    }


def _matrix(rows: Sequence[Mapping[str, Any]], names: Optional[Sequence[str]] = None) -> Tuple[List[str], List[List[float]]]:
    feature_rows = [_feature_dict(r) for r in rows]
    if names is None:
        names = list(feature_rows[0].keys()) if feature_rows else []
    return list(names), [[float(fr.get(n, 0.0)) for n in names] for fr in feature_rows]


def _fit_scaler(x: Sequence[Sequence[float]]) -> Tuple[List[float], List[float]]:
    if not x:
        return [], []
    p = len(x[0])
    mu, sd = [], []
    for j in range(p):
        vals = [float(r[j]) for r in x]
        m = sum(vals) / len(vals)
        var = sum((v - m) ** 2 for v in vals) / max(1, len(vals))
        s = math.sqrt(var)
        mu.append(m)
        sd.append(s if s > 1e-9 else 1.0)
    return mu, sd


def _scale(x: Sequence[Sequence[float]], mu: Sequence[float], sd: Sequence[float]) -> List[List[float]]:
    return [[(float(v) - mu[j]) / sd[j] for j, v in enumerate(row)] for row in x]


def _dot(w: Sequence[float], x: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(w, x))


def _fit_logistic(x: Sequence[Sequence[float]], y: Sequence[float], l2: float = L2) -> Dict[str, Any]:
    p = len(x[0]) if x else 0
    w = [0.0] * p
    base = _clip(sum(y) / max(1, len(y)), 1e-4, 1.0 - 1e-4)
    b = math.log(base / (1.0 - base))
    n = max(1, len(y))
    for t in range(1400):
        gw = [0.0] * p
        gb = 0.0
        for row, target in zip(x, y):
            pred = _sigmoid(b + _dot(w, row))
            err = pred - float(target)
            gb += err
            for j in range(p):
                gw[j] += err * row[j]
        lr = 0.08 / (1.0 + 0.002 * t)
        b -= lr * gb / n
        for j in range(p):
            grad = gw[j] / n + l2 * w[j] / n
            w[j] -= lr * grad
    return {"intercept": b, "weights": w, "base_rate": base}


def _fit_linear(x: Sequence[Sequence[float]], y: Sequence[float], l2: float = L2) -> Dict[str, Any]:
    p = len(x[0]) if x else 0
    y_mean = sum(y) / max(1, len(y))
    y_var = sum((v - y_mean) ** 2 for v in y) / max(1, len(y))
    y_sd = math.sqrt(y_var) if y_var > 1e-9 else 1.0
    yz = [(v - y_mean) / y_sd for v in y]
    w = [0.0] * p
    b = 0.0
    n = max(1, len(y))
    for t in range(1200):
        gw = [0.0] * p
        gb = 0.0
        for row, target in zip(x, yz):
            pred = b + _dot(w, row)
            err = pred - target
            gb += err
            for j in range(p):
                gw[j] += err * row[j]
        lr = 0.05 / (1.0 + 0.002 * t)
        b -= lr * gb / n
        for j in range(p):
            grad = gw[j] / n + l2 * w[j] / n
            w[j] -= lr * grad
    return {"intercept": b, "weights": w, "y_mean": y_mean, "y_sd": y_sd}


def _predict_logistic(model: Mapping[str, Any], x: Sequence[float]) -> float:
    return _sigmoid(float(model["intercept"]) + _dot(model["weights"], x))


def _predict_linear(model: Mapping[str, Any], x: Sequence[float]) -> float:
    z = float(model["intercept"]) + _dot(model["weights"], x)
    return float(model["y_mean"]) + z * float(model["y_sd"])


def _brier(preds: Sequence[float], y: Sequence[float]) -> Optional[float]:
    return None if not y else sum((p - t) ** 2 for p, t in zip(preds, y)) / len(y)


def _mae(preds: Sequence[float], y: Sequence[float]) -> Optional[float]:
    return None if not y else sum(abs(p - t) for p, t in zip(preds, y)) / len(y)


def _serialized_model(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    names, raw = _matrix(rows)
    mu, sd = _fit_scaler(raw)
    x = _scale(raw, mu, sd)
    yb = [float(int(r.get("beat_close") or 0)) for r in rows]
    yc = [_safe_float(r.get("clv_pct"), 0.0) for r in rows]
    return {
        "feature_names": names,
        "means": mu,
        "stds": sd,
        "classifier": _fit_logistic(x, yb),
        "clv_regressor": _fit_linear(x, yc),
    }


def _score_model(model: Mapping[str, Any], row: Mapping[str, Any]) -> Tuple[float, float]:
    _, raw = _matrix([row], model["feature_names"])
    x = _scale(raw, model["means"], model["stds"])[0]
    p = _predict_logistic(model["classifier"], x)
    clv = _clip(_predict_linear(model["clv_regressor"], x), -50.0, 50.0)
    return p, clv


def _trust_band(prob: float) -> str:
    if prob >= 0.70:
        return "HIGH"
    if prob >= 0.55:
        return "ABOVE_AVERAGE"
    if prob >= 0.40:
        return "NEUTRAL"
    return "LOW"


def _active_run(db) -> Optional[Dict[str, Any]]:
    return db.fetchone("SELECT * FROM meta_edge_model_runs WHERE active=1 ORDER BY id DESC LIMIT 1")


def create_frozen_meta_model(db, min_clean_labels: int = 200, holdout_fraction: float = DEFAULT_HOLDOUT_FRACTION) -> Dict[str, Any]:
    existing = _active_run(db)
    if existing:
        return {"created": False, "reason": "already_frozen", "run_id": existing["id"]}
    rows = db.fetchall(
        "SELECT * FROM meta_edge_samples WHERE label_status='CLEAN_AB' ORDER BY captured_at,id"
    )
    n = len(rows)
    if n < int(min_clean_labels):
        return {"created": False, "reason": "insufficient_labels", "clean_labels": n}
    holdout_n = max(MIN_HOLDOUT, int(round(n * float(holdout_fraction))))
    holdout_n = min(holdout_n, max(1, n - 100))
    train = rows[:-holdout_n]
    holdout = rows[-holdout_n:]
    if len(train) < 100 or len(holdout) < 20:
        return {"created": False, "reason": "insufficient_split", "clean_labels": n}

    eval_model = _serialized_model(train)
    holdout_pb, holdout_pc = [], []
    for r in holdout:
        p, c = _score_model(eval_model, r)
        holdout_pb.append(p)
        holdout_pc.append(c)
    hy = [float(int(r.get("beat_close") or 0)) for r in holdout]
    hc = [_safe_float(r.get("clv_pct"), 0.0) for r in holdout]
    train_rate = sum(float(int(r.get("beat_close") or 0)) for r in train) / len(train)
    train_clv_mean = sum(_safe_float(r.get("clv_pct"), 0.0) for r in train) / len(train)
    base_brier = _brier([train_rate] * len(holdout), hy)
    base_mae = _mae([train_clv_mean] * len(holdout), hc)
    model_brier = _brier(holdout_pb, hy)
    model_mae = _mae(holdout_pc, hc)

    ranked = sorted(zip(holdout_pb, hc, hy), key=lambda z: z[0], reverse=True)
    qn = max(1, len(ranked) // 4)
    top = ranked[:qn]
    metrics = {
        "holdout_brier": model_brier,
        "baseline_brier": base_brier,
        "brier_improvement_pct": ((base_brier - model_brier) / base_brier * 100.0) if base_brier else None,
        "holdout_clv_mae": model_mae,
        "baseline_clv_mae": base_mae,
        "clv_mae_improvement_pct": ((base_mae - model_mae) / base_mae * 100.0) if base_mae else None,
        "holdout_beat_close_pct": sum(hy) / len(hy) * 100.0,
        "holdout_avg_clv_pct": sum(hc) / len(hc),
        "top_quartile_samples": len(top),
        "top_quartile_beat_close_pct": sum(x[2] for x in top) / len(top) * 100.0,
        "top_quartile_avg_clv_pct": sum(x[1] for x in top) / len(top),
    }
    final_model = _serialized_model(rows)
    frozen_at = _now()
    final_sample_id = max(int(r["id"]) for r in rows)
    final_captured_at = max(str(r["captured_at"]) for r in rows)
    holdout_ids = [int(r["id"]) for r in holdout]
    status = "FROZEN_SHADOW_EVALUATING"
    db.execute(
        """
        INSERT INTO meta_edge_model_runs(
            model_version,feature_version,status,active,frozen_at,clean_labels,
            train_labels,holdout_labels,trained_through_sample_id,trained_through_captured_at,
            holdout_start_at,holdout_sample_ids_json,model_json,metrics_json,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            META_MODEL_VERSION,str(rows[0].get("feature_version") or ""),status,1,frozen_at,n,
            len(train),len(holdout),final_sample_id,final_captured_at,str(holdout[0]["captured_at"]),
            json.dumps(holdout_ids,separators=(",",":")),json.dumps(final_model,separators=(",",":")),
            json.dumps(metrics,separators=(",",":")),frozen_at,frozen_at,
        ),
    )
    run = _active_run(db)
    for r, p, c in zip(holdout, holdout_pb, holdout_pc):
        db.execute(
            """INSERT INTO meta_edge_model_scores(
                model_run_id,sample_id,phase,prob_beat_close,expected_clv_pct,trust_band,scored_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (run["id"],r["id"],"HOLDOUT",p,c,_trust_band(p),frozen_at),
        )
    return {"created": True, "run_id": run["id"], "metrics": metrics, "clean_labels": n}


def score_forward_meta_samples(db) -> int:
    run = _active_run(db)
    if not run:
        return 0
    model = json.loads(run["model_json"])
    rows = db.fetchall(
        """
        SELECT s.* FROM meta_edge_samples s
        LEFT JOIN meta_edge_model_scores ms
          ON ms.model_run_id=? AND ms.sample_id=s.id
        WHERE s.id>? AND ms.id IS NULL
        ORDER BY s.id
        """,
        (run["id"],run["trained_through_sample_id"]),
    )
    n = 0
    now = _now()
    for r in rows:
        p, c = _score_model(model, r)
        db.execute(
            """INSERT INTO meta_edge_model_scores(
                model_run_id,sample_id,phase,prob_beat_close,expected_clv_pct,trust_band,scored_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (run["id"],r["id"],"FORWARD",p,c,_trust_band(p),now),
        )
        n += 1
    return n


def run_meta_model_maintenance(db, min_clean_labels: int = 200, enabled: bool = True) -> Dict[str, Any]:
    if not enabled:
        return {"enabled": False, "created": False, "scored": 0}
    created = create_frozen_meta_model(db, min_clean_labels=min_clean_labels)
    scored = score_forward_meta_samples(db)
    return {"enabled": True, "created": bool(created.get("created")), "reason": created.get("reason"), "run_id": created.get("run_id"), "scored": scored}


def meta_model_status(db) -> Dict[str, Any]:
    run = _active_run(db)
    if not run:
        clean = int((db.fetchone("SELECT COUNT(*) AS n FROM meta_edge_samples WHERE label_status='CLEAN_AB'") or {}).get("n") or 0)
        return {
            "status": "WAITING_FOR_FROZEN_MODEL",
            "model_version": META_MODEL_VERSION,
            "active": False,
            "research_only": True,
            "selection_authority": False,
            "clean_labels": clean,
        }
    metrics = json.loads(run.get("metrics_json") or "{}")
    forward = db.fetchone(
        """
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN s.label_status='CLEAN_AB' THEN 1 ELSE 0 END) AS labeled,
               AVG(CASE WHEN s.label_status='CLEAN_AB' THEN s.clv_pct END) AS avg_clv,
               AVG(CASE WHEN s.label_status='CLEAN_AB' THEN s.beat_close END)*100.0 AS beat_close
        FROM meta_edge_model_scores ms
        JOIN meta_edge_samples s ON s.id=ms.sample_id
        WHERE ms.model_run_id=? AND ms.phase='FORWARD'
        """,
        (run["id"],),
    ) or {}
    high = db.fetchone(
        """
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN s.label_status='CLEAN_AB' THEN 1 ELSE 0 END) AS labeled,
               AVG(CASE WHEN s.label_status='CLEAN_AB' THEN s.clv_pct END) AS avg_clv,
               AVG(CASE WHEN s.label_status='CLEAN_AB' THEN s.beat_close END)*100.0 AS beat_close
        FROM meta_edge_model_scores ms
        JOIN meta_edge_samples s ON s.id=ms.sample_id
        WHERE ms.model_run_id=? AND ms.phase='FORWARD' AND ms.trust_band='HIGH'
        """,
        (run["id"],),
    ) or {}
    return {
        "status": run["status"],
        "model_version": run["model_version"],
        "active": True,
        "research_only": True,
        "selection_authority": False,
        "frozen_at": run["frozen_at"],
        "clean_labels_at_freeze": int(run["clean_labels"]),
        "train_labels": int(run["train_labels"]),
        "holdout_labels": int(run["holdout_labels"]),
        "trained_through_sample_id": int(run["trained_through_sample_id"]),
        "metrics": metrics,
        "forward_scores": int(forward.get("n") or 0),
        "forward_clean_labels": int(forward.get("labeled") or 0),
        "forward_avg_clv_pct": forward.get("avg_clv"),
        "forward_beat_close_pct": forward.get("beat_close"),
        "high_trust_forward_scores": int(high.get("n") or 0),
        "high_trust_clean_labels": int(high.get("labeled") or 0),
        "high_trust_avg_clv_pct": high.get("avg_clv"),
        "high_trust_beat_close_pct": high.get("beat_close"),
    }


def latest_meta_model_scores(db, limit: int = 100) -> List[Dict[str, Any]]:
    run = _active_run(db)
    if not run:
        return []
    return db.fetchall(
        """
        SELECT ms.*,s.source_model,s.market_key,s.league,s.selection,s.offered_odds,s.edge_pct,
               s.label_status,s.clv_pct,s.beat_close,s.captured_at,s.commence_time
        FROM meta_edge_model_scores ms
        JOIN meta_edge_samples s ON s.id=ms.sample_id
        WHERE ms.model_run_id=?
        ORDER BY ms.id DESC LIMIT ?
        """,
        (run["id"], max(1, min(int(limit), 1000))),
    )
