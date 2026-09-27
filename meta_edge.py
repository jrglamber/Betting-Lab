from __future__ import annotations

from datetime import datetime, timezone
import math
from statistics import mean
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from instrumentation import market_wave_metrics

META_EDGE_VERSION = "META1_CLV_TRUST_FEATURES_V1"
CLEAN_CLV_QUALITIES = {"A", "B"}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _entropy_binary(p: float) -> float:
    p = min(max(float(p), 1e-12), 1.0 - 1e-12)
    return -(p * math.log(p) + (1.0 - p) * math.log(1.0 - p)) / math.log(2.0)


def _entropy_multiclass(probs: Iterable[float]) -> float:
    vals = [min(max(float(x), 1e-12), 1.0) for x in probs]
    total = sum(vals)
    if total <= 0 or len(vals) <= 1:
        return 0.0
    vals = [x / total for x in vals]
    return -sum(x * math.log(x) for x in vals) / math.log(float(len(vals)))


def _odds_band(odds: float) -> str:
    if odds < 2.0:
        return "<2"
    if odds < 3.0:
        return "2-3"
    if odds < 5.0:
        return "3-5"
    if odds < 8.0:
        return "5-8"
    return "8+"


def _edge_band(edge: float) -> str:
    if edge < 5.0:
        return "3-5%"
    if edge < 10.0:
        return "5-10%"
    if edge < 20.0:
        return "10-20%"
    if edge < 40.0:
        return "20-40%"
    return "40%+"


def _uncertainty_band(score: float) -> str:
    if score < 0.55:
        return "LOW"
    if score < 0.80:
        return "MEDIUM"
    return "HIGH"


def _agreement_band(gap_pp: Optional[float]) -> str:
    if gap_pp is None:
        return "UNPAIRED"
    gap = abs(float(gap_pp))
    if gap <= 1.0 + 1e-9:
        return "TIGHT_<=1PP"
    if gap <= 3.0 + 1e-9:
        return "CLOSE_1-3PP"
    if gap <= 7.5:
        return "MIXED_3-7.5PP"
    return "DIVERGENT_>7.5PP"


def _archetype(market_key: str, selection: str, p: float, expected_total: Optional[float]) -> str:
    market = str(market_key or "h2h")
    if market == "h2h":
        if str(selection).lower() == "draw":
            base = "DRAW"
        elif p >= 0.65:
            base = "STRONG_FAV"
        elif p >= 0.50:
            base = "FAV"
        elif p <= 0.25:
            base = "LONGSHOT"
        else:
            base = "BALANCED"
    else:
        if p >= 0.65:
            base = "STRONG_SIDE"
        elif p >= 0.55:
            base = "LEAN"
        elif p >= 0.45:
            base = "COINFLIP"
        else:
            base = "CONTRARIAN"
    if expected_total is None:
        return base
    goal_band = "LOW_GOALS" if expected_total < 2.2 else ("HIGH_GOALS" if expected_total > 3.0 else "NORMAL_GOALS")
    return f"{base}|{goal_band}"


def _entry_wave(db, event_id: str, market_key: str, created_at: str) -> Optional[str]:
    # Entry features are strictly as-of the frozen bet timestamp. Never fall
    # forward to a later odds wave: that would leak post-entry information.
    row = db.fetchone(
        """
        SELECT captured_at FROM odds_snapshots
        WHERE event_id=? AND market_key=? AND captured_at<=?
        ORDER BY captured_at DESC LIMIT 1
        """,
        (event_id, market_key, created_at),
    )
    return str(row["captured_at"]) if row else None


def _entry_market_features(db, row: Mapping[str, Any]) -> Dict[str, Any]:
    wave = _entry_wave(db, str(row["event_id"]), str(row["market_key"]), str(row["created_at"]))
    if not wave:
        return {
            "entry_snapshot_at": None,
            "bookmaker_count": 0,
            "median_market_odds": None,
            "best_market_odds": None,
            "price_dispersion_pct": None,
            "chosen_vs_median_pct": None,
            "mean_overround_pct": None,
        }
    rows = db.fetchall(
        """
        SELECT * FROM odds_snapshots
        WHERE event_id=? AND captured_at=? AND market_key=?
        ORDER BY id
        """,
        (row["event_id"], wave, row["market_key"]),
    )
    metrics = market_wave_metrics(
        rows,
        market_key=str(row["market_key"]),
        selection=str(row["selection"]),
        point=row.get("point"),
        outcome_description=None,
        chosen_odds=float(row["offered_odds"]),
    )
    return {
        "entry_snapshot_at": wave,
        "bookmaker_count": int(metrics.get("bookmaker_count") or 0),
        "median_market_odds": metrics.get("median_odds"),
        "best_market_odds": metrics.get("best_odds"),
        "price_dispersion_pct": metrics.get("price_dispersion_pct"),
        "chosen_vs_median_pct": metrics.get("chosen_vs_median_pct"),
        "mean_overround_pct": metrics.get("mean_overround_pct"),
    }


def _selection_probability_from_prediction(pred: Mapping[str, Any], selection: str) -> Optional[float]:
    if str(selection) == str(pred.get("home_team")):
        return _safe_float(pred.get("home_probability"))
    if str(selection).lower() == "draw":
        return _safe_float(pred.get("draw_probability"))
    if str(selection) == str(pred.get("away_team")):
        return _safe_float(pred.get("away_probability"))
    return None


def _paired_features(db, row: Mapping[str, Any]) -> Dict[str, Any]:
    source = str(row["source_model"])
    other = "PRED2" if source == "PRED1" else "PRED1"
    market = str(row["market_key"])
    paired_p = None
    paired_bet_exists = 0
    same_top_outcome = None

    pred_table = "football_predictive2_predictions" if other == "PRED2" else "football_predictive_predictions"
    other_pred = db.fetchone(
        f"SELECT * FROM {pred_table} WHERE event_id=? AND created_at<=?",
        (row["event_id"], row["created_at"]),
    )
    if other_pred:
        if market == "h2h":
            paired_p = _selection_probability_from_prediction(other_pred, str(row["selection"]))
            own_probs = [
                (str(row.get("home_team")), _safe_float(row.get("home_probability")) or 0.0),
                ("Draw", _safe_float(row.get("draw_probability")) or 0.0),
                (str(row.get("away_team")), _safe_float(row.get("away_probability")) or 0.0),
            ]
            other_probs = [
                (str(other_pred.get("home_team")), _safe_float(other_pred.get("home_probability")) or 0.0),
                ("Draw", _safe_float(other_pred.get("draw_probability")) or 0.0),
                (str(other_pred.get("away_team")), _safe_float(other_pred.get("away_probability")) or 0.0),
            ]
            own_top = max(own_probs, key=lambda x: x[1])[0]
            other_top = max(other_probs, key=lambda x: x[1])[0]
            same_top_outcome = int(own_top == other_top)
            bet_table = "football_predictive2_bets" if other == "PRED2" else "football_predictive_bets"
            paired_bet_exists = int(bool(db.fetchone(
                f"SELECT id FROM {bet_table} WHERE event_id=? AND selection=? AND created_at<=? LIMIT 1",
                (row["event_id"], row["selection"], row["created_at"]),
            )))
        else:
            mp_table = "football_predictive2_market_predictions" if other == "PRED2" else "football_predictive_market_predictions"
            mp = db.fetchone(
                f"""
                SELECT * FROM {mp_table}
                WHERE event_id=? AND market_key=? AND selection=? AND line_key=?
                LIMIT 1
                """,
                (row["event_id"], row["market_key"], row["selection"], row.get("line_key")),
            )
            if mp:
                paired_p = _safe_float(mp.get("probability"))
            mb_table = "football_predictive2_market_bets" if other == "PRED2" else "football_predictive_market_bets"
            paired_bet_exists = int(bool(db.fetchone(
                f"""
                SELECT id FROM {mb_table}
                WHERE event_id=? AND market_key=? AND selection=? AND line_key=? AND created_at<=? LIMIT 1
                """,
                (row["event_id"], row["market_key"], row["selection"], row.get("line_key"), row["created_at"]),
            )))

    own_p = float(row["model_probability"])
    gap_pp = (own_p - paired_p) * 100.0 if paired_p is not None else None
    return {
        "paired_model": other if other_pred else None,
        "paired_probability": paired_p,
        "paired_probability_gap_pp": gap_pp,
        "agreement_band": _agreement_band(gap_pp),
        "paired_bet_exists": paired_bet_exists,
        "same_top_outcome": same_top_outcome,
    }


def _source_rows(db) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    specs = [
        ("PRED1", "football_predictive_bets", "football_predictive_predictions", None),
        ("PRED2", "football_predictive2_bets", "football_predictive2_predictions", None),
        ("PRED1", "football_predictive_market_bets", "football_predictive_predictions", "market"),
        ("PRED2", "football_predictive2_market_bets", "football_predictive2_predictions", "market"),
    ]
    for source_model, bet_table, pred_table, kind in specs:
        if kind == "market":
            rows = db.fetchall(
                f"""
                SELECT b.*,p.sport_key,p.league,p.commence_time,p.home_team,p.away_team,
                       p.home_probability,p.draw_probability,p.away_probability,
                       p.expected_home_goals,p.expected_away_goals,p.league_training_matches,
                       p.home_effective_matches,p.away_effective_matches,
                       p.id AS source_prediction_id
                FROM {bet_table} b
                JOIN {pred_table} p ON p.id=b.prediction_id
                ORDER BY b.id
                """
            )
        else:
            rows = db.fetchall(
                f"""
                SELECT b.*,p.sport_key,p.league,p.commence_time,p.home_team,p.away_team,
                       p.home_probability,p.draw_probability,p.away_probability,
                       p.expected_home_goals,p.expected_away_goals,p.league_training_matches,
                       p.home_effective_matches,p.away_effective_matches,
                       p.id AS source_prediction_id
                FROM {bet_table} b
                JOIN {pred_table} p ON p.id=b.prediction_id
                ORDER BY b.id
                """
            )
            for r in rows:
                r["market_key"] = "h2h"
                r["point"] = None
                r["line_key"] = "h2h"
        for r in rows:
            r["source_model"] = source_model
            r["source_bet_table"] = bet_table
            out.append(r)
    return out


def _uncertainty(row: Mapping[str, Any]) -> float:
    if str(row["market_key"]) == "h2h":
        return _entropy_multiclass([
            float(row.get("home_probability") or 0.0),
            float(row.get("draw_probability") or 0.0),
            float(row.get("away_probability") or 0.0),
        ])
    return _entropy_binary(float(row["model_probability"]))


def _sample_key(row: Mapping[str, Any]) -> str:
    return f"{row['source_model']}|{row['source_bet_table']}|{row['id']}"


def capture_meta_edge_samples(db) -> int:
    inserted = 0
    now = _iso_now()
    for row in _source_rows(db):
        key = _sample_key(row)
        if db.fetchone("SELECT id FROM meta_edge_samples WHERE sample_key=?", (key,)):
            continue
        entry = _entry_market_features(db, row)
        pair = _paired_features(db, row)
        kickoff = _parse_iso(row["commence_time"])
        created = _parse_iso(row["created_at"])
        hours_to_kickoff = max(0.0, (kickoff - created).total_seconds() / 3600.0)
        uncertainty = _uncertainty(row)
        expected_total = float(row.get("expected_home_goals") or 0.0) + float(row.get("expected_away_goals") or 0.0)
        p = float(row["model_probability"])
        odds = float(row["offered_odds"])
        edge = float(row["edge_pct"])
        training_support = min(float(row.get("home_effective_matches") or 0.0), float(row.get("away_effective_matches") or 0.0))
        db.execute(
            """
            INSERT INTO meta_edge_samples(
                sample_key,feature_version,source_model,source_bet_table,source_bet_id,
                source_prediction_id,event_id,captured_at,entry_snapshot_at,commence_time,
                sport_key,league,market_key,selection,point,line_key,bookmaker_key,
                offered_odds,model_probability,model_fair_odds,edge_pct,strong_candidate,
                hours_to_kickoff,odds_band,edge_band,bookmaker_count,median_market_odds,
                best_market_odds,price_dispersion_pct,chosen_vs_median_pct,mean_overround_pct,
                paired_model,paired_probability,paired_probability_gap_pp,agreement_band,
                paired_bet_exists,same_top_outcome,uncertainty_score,uncertainty_band,
                expected_total_goals,league_training_matches,training_support_matches,
                archetype,label_status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                key,META_EDGE_VERSION,row["source_model"],row["source_bet_table"],row["id"],
                row["source_prediction_id"],row["event_id"],row["created_at"],entry["entry_snapshot_at"],row["commence_time"],
                row["sport_key"],row["league"],row["market_key"],row["selection"],row.get("point"),row.get("line_key"),row["bookmaker_key"],
                odds,p,float(row["model_fair_odds"]),edge,int(row.get("strong_candidate") or 0),
                hours_to_kickoff,_odds_band(odds),_edge_band(edge),entry["bookmaker_count"],entry["median_market_odds"],
                entry["best_market_odds"],entry["price_dispersion_pct"],entry["chosen_vs_median_pct"],entry["mean_overround_pct"],
                pair["paired_model"],pair["paired_probability"],pair["paired_probability_gap_pp"],pair["agreement_band"],
                pair["paired_bet_exists"],pair["same_top_outcome"],uncertainty,_uncertainty_band(uncertainty),
                expected_total,int(row.get("league_training_matches") or 0),training_support,
                _archetype(str(row["market_key"]),str(row["selection"]),p,expected_total),"PENDING",now,now,
            ),
        )
        inserted += 1
    return inserted


def label_meta_edge_samples(db) -> int:
    updated = 0
    now = _iso_now()
    rows = db.fetchall("SELECT * FROM meta_edge_samples WHERE label_status='PENDING' ORDER BY id")
    for sample in rows:
        bet = db.fetchone(
            f"SELECT * FROM {sample['source_bet_table']} WHERE id=?",
            (sample["source_bet_id"],),
        )
        if not bet or bet.get("clv_pct") is None:
            continue
        quality = str(bet.get("clv_quality") or "")
        status = "CLEAN_AB" if quality in CLEAN_CLV_QUALITIES else "NON_HEADLINE"
        clv = float(bet["clv_pct"])
        db.execute(
            """
            UPDATE meta_edge_samples
            SET label_status=?,clv_pct=?,clv_quality=?,beat_close=?,closing_odds=?,
                result=?,net_pnl_units=?,labeled_at=?,updated_at=?
            WHERE id=?
            """,
            (
                status,clv,quality,int(clv>0.0),bet.get("closing_odds"),
                bet.get("result"),bet.get("net_pnl_units"),now,now,sample["id"],
            ),
        )
        updated += 1
    return updated


def _segment(db, field: str) -> List[Dict[str, Any]]:
    allowed = {
        "source_model","market_key","league","agreement_band","odds_band",
        "edge_band","uncertainty_band","archetype","bookmaker_key",
    }
    if field not in allowed:
        raise ValueError("unsupported meta-edge segment")
    rows = db.fetchall(
        f"""
        SELECT {field} AS segment,COUNT(*) AS samples,
               AVG(clv_pct) AS avg_clv_pct,
               AVG(beat_close)*100.0 AS beat_close_pct,
               AVG(edge_pct) AS avg_claimed_edge_pct,
               AVG(net_pnl_units) AS avg_net_pnl_units
        FROM meta_edge_samples
        WHERE label_status='CLEAN_AB'
        GROUP BY {field}
        ORDER BY samples DESC,segment
        """
    )
    return rows


def meta_edge_segments(db) -> Dict[str, List[Dict[str, Any]]]:
    return {field: _segment(db, field) for field in (
        "source_model","market_key","league","agreement_band","odds_band",
        "edge_band","uncertainty_band","archetype","bookmaker_key",
    )}


def meta_edge_scoreboard(db, min_clean_labels: int = 200) -> Dict[str, Any]:
    total = int((db.fetchone("SELECT COUNT(*) AS n FROM meta_edge_samples") or {}).get("n") or 0)
    clean = int((db.fetchone("SELECT COUNT(*) AS n FROM meta_edge_samples WHERE label_status='CLEAN_AB'") or {}).get("n") or 0)
    pending = int((db.fetchone("SELECT COUNT(*) AS n FROM meta_edge_samples WHERE label_status='PENDING'") or {}).get("n") or 0)
    row = db.fetchone(
        """
        SELECT AVG(clv_pct) AS avg_clv_pct,AVG(beat_close)*100.0 AS beat_close_pct,
               AVG(edge_pct) AS avg_claimed_edge_pct,AVG(ABS(paired_probability_gap_pp)) AS avg_pair_gap_pp,
               AVG(uncertainty_score) AS avg_uncertainty
        FROM meta_edge_samples WHERE label_status='CLEAN_AB'
        """
    ) or {}
    pred1 = db.fetchone(
        """
        SELECT COUNT(*) AS n,AVG(clv_pct) AS avg_clv_pct,AVG(beat_close)*100.0 AS beat_close_pct
        FROM meta_edge_samples WHERE label_status='CLEAN_AB' AND source_model='PRED1'
        """
    ) or {}
    pred2 = db.fetchone(
        """
        SELECT COUNT(*) AS n,AVG(clv_pct) AS avg_clv_pct,AVG(beat_close)*100.0 AS beat_close_pct
        FROM meta_edge_samples WHERE label_status='CLEAN_AB' AND source_model='PRED2'
        """
    ) or {}
    status = "READY_FOR_FROZEN_META_MODEL" if clean >= int(min_clean_labels) else "COLLECTING_FEATURE_LABELS"
    return {
        "feature_version": META_EDGE_VERSION,
        "status": status,
        "research_only": True,
        "selection_authority": False,
        "extra_provider_calls": 0,
        "current_inputs": [
            "stored PRED1/PRED2 probabilities",
            "stored Odds API entry prices/market depth",
            "time to kickoff",
            "training depth",
            "PRED1/PRED2 agreement",
            "future A/B-quality CLV label",
        ],
        "pred3_external_inputs_required": [
            "xG/shot-quality event data",
            "lineups/injuries/availability",
            "optional tactical/style features",
        ],
        "samples": total,
        "clean_ab_labels": clean,
        "pending_labels": pending,
        "min_clean_labels_for_model": int(min_clean_labels),
        "progress_pct": min(100.0, clean / max(1, int(min_clean_labels)) * 100.0),
        "avg_clv_pct": row.get("avg_clv_pct"),
        "beat_close_pct": row.get("beat_close_pct"),
        "avg_claimed_edge_pct": row.get("avg_claimed_edge_pct"),
        "edge_minus_clv_pp": (
            float(row["avg_claimed_edge_pct"]) - float(row["avg_clv_pct"])
            if row.get("avg_claimed_edge_pct") is not None and row.get("avg_clv_pct") is not None else None
        ),
        "avg_pair_probability_gap_pp": row.get("avg_pair_gap_pp"),
        "avg_uncertainty": row.get("avg_uncertainty"),
        "pred1": {"samples": int(pred1.get("n") or 0), "avg_clv_pct": pred1.get("avg_clv_pct"), "beat_close_pct": pred1.get("beat_close_pct")},
        "pred2": {"samples": int(pred2.get("n") or 0), "avg_clv_pct": pred2.get("avg_clv_pct"), "beat_close_pct": pred2.get("beat_close_pct")},
    }


def latest_meta_edge_samples(db, limit: int = 100) -> List[Dict[str, Any]]:
    return db.fetchall(
        "SELECT * FROM meta_edge_samples ORDER BY id DESC LIMIT ?",
        (max(1, min(int(limit), 1000)),),
    )


def run_meta_edge_maintenance(db) -> Dict[str, Any]:
    captured = capture_meta_edge_samples(db)
    labeled = label_meta_edge_samples(db)
    return {"captured": captured, "labeled": labeled}
