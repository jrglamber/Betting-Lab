from __future__ import annotations

"""Stable-identity/correct-join guard for adaptive TPRED4.

TPRED3 stores the provider match id in ``rich_match_id`` and may rebuild its
walk-forward table, producing new database row ids for the same historical
matches. TPRED4 must therefore join back to rich history on provider_match_id
and must mark history by (model,tour,provider_match_id), never by TPRED3 row id.

The initial catch-up is committed atomically with player profiles/weights so a
process restart cannot leave rows marked processed without their learned state.
"""

import json

import adaptive_winner_research as core

VERSION = "ADAPTIVE_TPRED4_IDENTITY_V1"
_INSTALLED = False


def _sql(db, text: str) -> str:
    return db._sql(text) if hasattr(db, "_sql") else text


def _pending(db) -> int:
    row = db.fetchone(
        """SELECT COUNT(*) AS n
           FROM tennis_predictive3_walkforward w
           LEFT JOIN tennis_predictive4_walkforward p4
             ON p4.model_version=?
            AND lower(p4.tour)=lower(w.tour)
            AND p4.rich_match_id=w.rich_match_id
           WHERE w.model_version=? AND p4.id IS NULL""",
        (core.TPRED4_MODEL_VERSION, core.tpred3.MODEL_VERSION),
    ) or {}
    return int(row.get("n") or 0)


def _process_history_stable(db):
    weights = core._tpred4_weights(db)
    players = core._load_players(db)
    rows = db.fetchall(
        """SELECT
             w.id AS source_walkforward_id,w.rich_match_id,w.commence_time,
             w.tour,w.surface,w.player_one,w.player_two,w.predicted_p1,
             w.market_p1,w.outcome_p1,w.evidence_ready,w.feature_json,
             r.player1_name AS rich_p1,r.player2_name AS rich_p2,
             r.result AS rich_result,r.stats_json
           FROM tennis_predictive3_walkforward w
           LEFT JOIN tennis_predictive4_walkforward p4
             ON p4.model_version=?
            AND lower(p4.tour)=lower(w.tour)
            AND p4.rich_match_id=w.rich_match_id
           LEFT JOIN tennis_rich_matches r
             ON lower(r.tour)=lower(w.tour)
            AND r.provider_match_id=w.rich_match_id
           WHERE w.model_version=? AND p4.id IS NULL
           ORDER BY w.commence_time,w.id""",
        (core.TPRED4_MODEL_VERSION, core.tpred3.MODEL_VERSION),
    )
    if not rows:
        core._set_state(db, "tennis_predictive4_state", "history_ready", "1")
        return {
            "ok": True,
            "processed": 0,
            "remaining": 0,
            "history_ready": True,
            "stable_identity": "tour+provider_match_id",
        }

    inserts = []
    updates = int(core._state(db, "tennis_predictive4_state", "learning_updates") or 0)
    max_source_id = int(core._state(db, "tennis_predictive4_state", "last_source_walkforward_id") or 0)
    for row in rows:
        rich_id = row.get("rich_match_id")
        if rich_id is None:
            continue
        source_id = int(row.get("source_walkforward_id") or 0)
        max_source_id = max(max_source_id, source_id)
        tour = str(row.get("tour") or "")
        p1 = str(row.get("player_one") or "")
        p2 = str(row.get("player_two") or "")
        base = float(row.get("predicted_p1"))
        y = int(row.get("outcome_p1") or 0)
        feat = core._profile_features(players, tour, p1, p2)
        p = core._tpred4_predict(base, weights, feat)
        market = core._f(row.get("market_p1"))
        brier = (p - y) ** 2
        market_brier = ((market - y) ** 2) if market is not None else None
        try:
            t3feat = json.loads(str(row.get("feature_json") or "{}"))
        except Exception:
            t3feat = {}
        payload = {
            "tpred3_probability": base,
            **feat,
            "tpred3_features": t3feat,
        }
        inserts.append(
            (
                source_id, rich_id, row.get("commence_time"), tour,
                row.get("surface"), p1, p2, p, market, y, brier,
                market_brier, int(row.get("evidence_ready") or 0),
                json.dumps(payload, sort_keys=True, default=str),
                core.TPRED4_MODEL_VERSION, core.utc_now_iso(),
            )
        )
        updates += 1
        core._tpred4_learn(weights, feat, p, y, updates)
        stats1, stats2 = core._extract_stats(row.get("stats_json"))
        rp1 = core._norm_name(row.get("rich_p1"))
        if rp1 and rp1 != core._norm_name(p1) and rp1 == core._norm_name(p2):
            stats1, stats2 = stats2, stats1
        core._update_profiles(
            players, tour, p1, p2, y, stats1, stats2,
            core._dominance_magnitude(row.get("rich_result")),
        )

    player_rows = []
    now = core.utc_now_iso()
    for (tour, norm), player in players.items():
        if not norm:
            continue
        player_rows.append(
            (
                tour, norm, int(player.get("matches") or 0),
                float(player.get("serve") or 0.5),
                float(player.get("return") or 0.5),
                float(player.get("dom") or 0.0),
                float(player.get("recent_dom") or 0.0), now,
            )
        )

    insert_sql = """INSERT INTO tennis_predictive4_walkforward(
        source_walkforward_id,rich_match_id,commence_time,tour,surface,
        player_one,player_two,predicted_p1,market_p1,outcome_p1,brier,
        market_brier,evidence_ready,feature_json,model_version,created_at
      ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(model_version,tour,rich_match_id) DO NOTHING"""
    player_sql = """INSERT INTO tennis_predictive4_player_state(
        tour,player_norm,matches,adj_serve,adj_return,dominance,recent_dominance,updated_at
      ) VALUES(?,?,?,?,?,?,?,?)
      ON CONFLICT(tour,player_norm) DO UPDATE SET
        matches=excluded.matches,adj_serve=excluded.adj_serve,
        adj_return=excluded.adj_return,dominance=excluded.dominance,
        recent_dominance=excluded.recent_dominance,updated_at=excluded.updated_at"""
    state_sql = """INSERT INTO tennis_predictive4_state(state_key,state_value,updated_at)
      VALUES(?,?,?)
      ON CONFLICT(state_key) DO UPDATE SET
        state_value=excluded.state_value,updated_at=excluded.updated_at"""
    state_rows = (
        ("last_source_walkforward_id", str(max_source_id), now),
        ("weights_json", json.dumps(weights, sort_keys=True), now),
        ("learning_updates", str(updates), now),
        ("history_ready", "1", now),
    )

    # All learned state and processed-row markers move together.
    with db.connection() as conn:
        cur = conn.cursor()
        if inserts:
            cur.executemany(_sql(db, insert_sql), inserts)
        if player_rows:
            cur.executemany(_sql(db, player_sql), player_rows)
        cur.executemany(_sql(db, state_sql), state_rows)

    remaining = _pending(db)
    if remaining:
        core._set_state(db, "tennis_predictive4_state", "history_ready", "0")
    return {
        "ok": True,
        "processed": len(inserts),
        "remaining": remaining,
        "history_ready": remaining == 0,
        "stable_identity": "tour+provider_match_id",
        "correct_rich_join": "tour+provider_match_id",
        "weights": weights,
    }


def install(db):
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    core.ensure_schema(db)
    # New deployment has no rows yet. The unique index also protects future
    # TPRED3 rebuilds from replaying the same provider match into TPRED4.
    db.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_tpred4_rich_match
           ON tennis_predictive4_walkforward(model_version,tour,rich_match_id)"""
    )
    core._process_tpred4_history = _process_history_stable
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "stable_identity": "tour+provider_match_id",
        "rich_join": "tour+provider_match_id",
        "tpred3_rebuild_replay_protected": True,
        "atomic_history_commit": True,
        "betting_authority": False,
    }
