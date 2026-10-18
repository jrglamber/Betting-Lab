from __future__ import annotations

"""Safety corrections for TPRED3 historical training.

The rich provider's completed-results contract lists the winner as player1 and
loser as player2. Training directly on that presentation order would leak the
outcome. This patch makes historical orientation outcome-independent by sorting
on provider player ID before prediction, then assigns the label from the
provider winner afterwards.

It also replaces the generic stat parser with the provider's documented
player1Stats/player2Stats schema and derives percentage features from counts.
No betting authority is added.
"""

import json
from typing import Any, Dict, Mapping, Optional, Tuple

import tennis_predictive3 as tp3

VERSION = "TPRED3_SAFETY_PATCH_V1"
MODEL_VERSION = "TPRED3_RICH_PREQUENTIAL_V2"


def _ratio(num: Any, den: Any) -> Optional[float]:
    try:
        n, d = float(num), float(den)
        if d <= 0:
            return None
        return max(0.0, min(1.0, n / d))
    except Exception:
        return None


def _exact_stats(payload_text: Any) -> Tuple[Dict[str, float], Dict[str, float]]:
    if not payload_text:
        return {}, {}
    try:
        payload = json.loads(payload_text) if isinstance(payload_text, str) else payload_text
    except Exception:
        return {}, {}
    if not isinstance(payload, Mapping):
        return {}, {}
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    if not isinstance(data, Mapping):
        return {}, {}
    raw1 = data.get("player1Stats") if isinstance(data.get("player1Stats"), Mapping) else {}
    raw2 = data.get("player2Stats") if isinstance(data.get("player2Stats"), Mapping) else {}

    def one(raw: Mapping[str, Any], other: Mapping[str, Any]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        first = _ratio(raw.get("winningOnFirstServe"), raw.get("winningOnFirstServeOf"))
        second = _ratio(raw.get("winningOnSecondServe"), raw.get("winningOnSecondServeOf"))
        total_service_points = raw.get("firstServeOf")
        try:
            own_service_won = float(raw.get("winningOnFirstServe") or 0) + float(raw.get("winningOnSecondServe") or 0)
        except Exception:
            own_service_won = 0.0
        service = _ratio(own_service_won, total_service_points)

        # Prefer explicit return-point fields when available. Otherwise derive
        # return points won from the opponent's service points.
        return_pct = _ratio(
            raw.get("returnPtsWin") if raw.get("returnPtsWin") is not None else raw.get("rpw"),
            raw.get("returnPtsWinOf") if raw.get("returnPtsWinOf") is not None else raw.get("rpwOf"),
        )
        if return_pct is None:
            try:
                opp_total = float(other.get("firstServeOf") or 0)
                opp_service_won = float(other.get("winningOnFirstServe") or 0) + float(other.get("winningOnSecondServe") or 0)
                return_pct = _ratio(opp_total - opp_service_won, opp_total)
            except Exception:
                return_pct = None

        saved = _ratio(raw.get("breakPointSavedGm"), raw.get("breakPointFacedGm"))
        ace_rate = _ratio(raw.get("aces"), total_service_points)
        df_rate = _ratio(raw.get("doubleFaults"), total_service_points)
        for key, value in (
            ("first_serve_won", first),
            ("second_serve_won", second),
            ("serve_points_won", service),
            ("return_points_won", return_pct),
            ("break_points_saved", saved),
            ("ace_rate", ace_rate),
            ("double_fault_rate", df_rate),
        ):
            if value is not None:
                out[key] = float(value)
        return out

    return one(raw1, raw2), one(raw2, raw1)


def _ordered_match(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        original_p1 = int(row.get("player1_id"))
        original_p2 = int(row.get("player2_id"))
    except Exception:
        return None
    if original_p1 == original_p2:
        return None

    # Results endpoint contract: provider player1 is the realised winner.
    winner_id = original_p1
    original_stats1, original_stats2 = _exact_stats(row.get("stats_json"))
    if original_p1 < original_p2:
        return {
            "p1id": original_p1,
            "p2id": original_p2,
            "p1": str(row.get("player1_name") or original_p1),
            "p2": str(row.get("player2_name") or original_p2),
            "y": 1,
            "stats1": original_stats1,
            "stats2": original_stats2,
        }
    return {
        "p1id": original_p2,
        "p2id": original_p1,
        "p1": str(row.get("player2_name") or original_p2),
        "p2": str(row.get("player1_name") or original_p1),
        "y": 0,
        "stats1": original_stats2,
        "stats2": original_stats1,
    }


def _rebuild_walkforward(db) -> Dict[str, Any]:
    tp3.install_schema(db)
    matches = db.fetchall(
        """
        SELECT * FROM tennis_rich_matches
        WHERE player1_id IS NOT NULL AND player2_id IS NOT NULL AND match_date IS NOT NULL
        ORDER BY match_date,provider_match_id
        """
    )
    rankings = tp3._load_rankings(db)
    canonical = tp3._canonical_index(db)
    state = tp3._new_model_state()
    rows_out = []
    for row in matches:
        at = tp3._parse_dt(row.get("match_date"))
        ordered = _ordered_match(row)
        if not at or not ordered:
            continue
        p1id, p2id = ordered["p1id"], ordered["p2id"]
        p1, p2, y = ordered["p1"], ordered["p2"], int(ordered["y"])
        surface = str(row.get("surface") or "").strip() or None
        tour = str(row.get("tour") or "").lower()
        feat = tp3._features(state, rankings, tour, p1id, p2id, at, surface)
        probability = tp3._predict(state, feat)
        ready = tp3._evidence_ready(feat, surface)

        pair = sorted((tp3._norm_name(p1), tp3._norm_name(p2)))
        canonical_row = canonical.get((at.date().isoformat(), pair[0], pair[1]))
        canonical_id = str(canonical_row.get("event_id")) if canonical_row else None
        market_p = None
        if canonical_row:
            canonical_p1 = str(canonical_row.get("player_one") or "")
            canonical_prob = tp3._market_probability(db, canonical_id, canonical_p1, at)
            if canonical_prob is not None:
                market_p = canonical_prob if tp3._norm_name(canonical_p1) == tp3._norm_name(p1) else 1.0 - canonical_prob

        rows_out.append((row,at,p1,p2,tour,surface,feat,probability,y,ready,market_p,canonical_id))
        tp3._learn(state, feat, probability, y)
        tp3._update_player_state(
            state,p1id,p2id,at,surface,y,ordered["stats1"],ordered["stats2"]
        )

    db.execute("DELETE FROM tennis_predictive3_walkforward WHERE model_version=?", (tp3.MODEL_VERSION,))
    created_at = tp3._iso(tp3._now())
    for row,at,p1,p2,tour,surface,feat,probability,y,ready,market_p,canonical_id in rows_out:
        db.execute(
            """
            INSERT INTO tennis_predictive3_walkforward(
                model_name,model_version,rich_match_id,canonical_event_id,commence_time,tour,surface,
                tournament_title,player_one,player_two,predicted_p1,market_p1,outcome_p1,brier,
                log_loss,market_brier,evidence_ready,feature_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                tp3.MODEL,tp3.MODEL_VERSION,int(row["provider_match_id"]),canonical_id,tp3._iso(at),tour,surface,
                row.get("tournament_name"),p1,p2,probability,market_p,y,tp3._brier(probability,y),
                tp3._log_loss(probability,y),tp3._brier(market_p,y) if market_p is not None else None,
                ready,json.dumps(feat,separators=(",",":"),default=str),created_at,
            ),
        )
    return {
        "matches": len(matches),
        "walkforward_rows": len(rows_out),
        "evidence_ready": sum(x[9] for x in rows_out),
        "market_comparable": sum(1 for x in rows_out if x[10] is not None),
        "weights": dict(state["weights"]),
        "state": state,
        "historical_orientation": "provider_id_sorted_before_prediction",
        "result_order_leakage": False,
    }


def _build_current_state(db, before) -> Tuple[Dict[str, Any], Dict[Any, Any]]:
    matches = db.fetchall(
        """
        SELECT * FROM tennis_rich_matches
        WHERE player1_id IS NOT NULL AND player2_id IS NOT NULL AND match_date IS NOT NULL
          AND match_date<? ORDER BY match_date,provider_match_id
        """,
        (tp3._iso(before),),
    )
    rankings = tp3._load_rankings(db)
    state = tp3._new_model_state()
    for row in matches:
        at = tp3._parse_dt(row.get("match_date"))
        ordered = _ordered_match(row)
        if not at or not ordered:
            continue
        surface = str(row.get("surface") or "").strip() or None
        feat = tp3._features(
            state,rankings,str(row.get("tour") or "").lower(),ordered["p1id"],ordered["p2id"],at,surface
        )
        probability = tp3._predict(state,feat)
        tp3._learn(state,feat,probability,int(ordered["y"]))
        tp3._update_player_state(
            state,ordered["p1id"],ordered["p2id"],at,surface,int(ordered["y"]),ordered["stats1"],ordered["stats2"]
        )
    return state,rankings


def install() -> Dict[str, Any]:
    tp3.MODEL_VERSION = MODEL_VERSION
    tp3._extract_stats = _exact_stats
    tp3.rebuild_walkforward = _rebuild_walkforward
    tp3._build_current_state = _build_current_state
    return {
        "installed": True,
        "version": VERSION,
        "model_version": MODEL_VERSION,
        "historical_orientation": "provider_id_sorted_before_prediction",
        "provider_completed_order": "winner_first",
        "exact_stat_schema": True,
        "betting_authority": False,
    }
