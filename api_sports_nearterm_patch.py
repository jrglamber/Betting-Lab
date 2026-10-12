from __future__ import annotations

"""Near-term forecast horizon for API-Sports PRED1.

The paid American-football feed exposes the full remaining schedule. PRED1 should
not freeze October probabilities for January fixtures: those are materially
different forecasting horizons and would contaminate the forward test. This
patch keeps the live/shadow lane near-term while leaving the historical
walk-forward study untouched.
"""

from datetime import timedelta
import json

import api_sports_predictive as core

VERSION = "APISPORTS_PRED1_NEARTERM_V1"
HORIZON_DAYS = 10


def _prune_premature_forecasts(db, lane, horizon_end: str) -> int:
    """Remove unused far-future OPEN forecasts so they can be regenerated later.

    Never delete a forecast that already has a frozen shadow bet; preserving any
    genuine price observation takes precedence over horizon cleanup.
    """
    rows = db.fetchall(
        """
        SELECT f.id
        FROM api_sports_pred_forecasts f
        WHERE f.model_name=?
          AND f.status='OPEN'
          AND f.commence_time>?
          AND NOT EXISTS (
              SELECT 1 FROM api_sports_pred_shadow_bets b WHERE b.forecast_id=f.id
          )
        """,
        (lane.model_name, horizon_end),
    )
    for row in rows:
        db.execute("DELETE FROM api_sports_pred_forecasts WHERE id=?", (row["id"],))
    return len(rows)


def _make_forecasts_nearterm(db, lane):
    now_iso = core.utc_now_iso()
    horizon_end = core._iso(core._now() + timedelta(days=HORIZON_DAYS))
    pruned = _prune_premature_forecasts(db, lane, horizon_end)

    finished = db.fetchall(
        """
        SELECT * FROM api_sports_pred_games
        WHERE lane_key=? AND status='FINISHED'
        ORDER BY commence_time ASC,provider_game_id ASC
        """,
        (lane.key,),
    )
    elo, perf = core._build_ratings(finished)
    upcoming = db.fetchall(
        """
        SELECT * FROM api_sports_pred_games
        WHERE lane_key=? AND status='UPCOMING'
          AND commence_time>?
          AND commence_time<=?
        ORDER BY commence_time ASC
        """,
        (lane.key, now_iso, horizon_end),
    )

    created = bets = 0
    for g in upcoming:
        if db.fetchone(
            "SELECT id FROM api_sports_pred_forecasts WHERE model_name=? AND provider_game_id=?",
            (lane.model_name, g["provider_game_id"]),
        ):
            continue

        home = str(g["home_team"])
        away = str(g["away_team"])
        he = elo.get(home, 1500.0)
        ae = elo.get(away, 1500.0)
        hpf, hpa, hn = core._weighted_team_stats(perf, home)
        apf, apa, an = core._weighted_team_stats(perf, away)
        if hn < 3 or an < 3:
            continue

        base_p = core._logistic_elo((he + core.HOME_ADVANTAGE_ELO) - ae)
        home_exp = ((hpf or 0.0) + (apa or 0.0)) / 2.0
        away_exp = ((apf or 0.0) + (hpa or 0.0)) / 2.0
        margin = home_exp - away_exp
        adj = max(-0.08, min(0.08, margin / 100.0))
        hp = max(0.04, min(0.96, base_p + adj))
        ap = 1.0 - hp
        odds_event_id = core._match_odds_event(db, lane, g)
        generated = core.utc_now_iso()
        metadata = {
            "method": "elo_plus_recent_scoring_margin",
            "elo_k": core.ELO_K,
            "home_advantage_elo": core.HOME_ADVANTAGE_ELO,
            "edge_threshold_pct": core.EDGE_THRESHOLD_PCT,
            "forecast_horizon_days": HORIZON_DAYS,
            "forecast_horizon_policy": VERSION,
            "research_only": True,
        }
        db.execute(
            """
            INSERT INTO api_sports_pred_forecasts(
              model_name,model_version,lane_key,provider_game_id,odds_event_id,generated_at,
              commence_time,home_team,away_team,home_probability,away_probability,
              home_fair_odds,away_fair_odds,expected_home_points,expected_away_points,
              expected_margin,expected_total,home_elo,away_elo,home_sample,away_sample,status,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                lane.model_name, core.MODEL_VERSION, lane.key, g["provider_game_id"], odds_event_id,
                generated, g["commence_time"], home, away, hp, ap,
                core._prob_to_odds(hp), core._prob_to_odds(ap), home_exp, away_exp,
                margin, home_exp + away_exp, he, ae, hn, an, "OPEN",
                json.dumps(metadata, sort_keys=True),
            ),
        )
        created += 1
        forecast = db.fetchone(
            "SELECT * FROM api_sports_pred_forecasts WHERE model_name=? AND provider_game_id=?",
            (lane.model_name, g["provider_game_id"]),
        )
        if not forecast or not odds_event_id:
            continue

        for selection, p in ((home, hp), (away, ap)):
            quote = core._latest_best_price(db, odds_event_id, selection)
            if not quote:
                continue
            book, offered = quote
            edge = (p * offered - 1.0) * 100.0
            if edge < core.EDGE_THRESHOLD_PCT:
                continue
            db.execute(
                """
                INSERT INTO api_sports_pred_shadow_bets(
                  forecast_id,model_name,created_at,odds_event_id,selection,bookmaker_key,
                  offered_odds,fair_probability,fair_odds,edge_pct,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(forecast_id,selection) DO NOTHING
                """,
                (
                    forecast["id"], lane.model_name, generated, odds_event_id, selection,
                    book, offered, p, core._prob_to_odds(p), edge, "OPEN",
                ),
            )
            bets += 1

    return {
        "forecasts_created": created,
        "shadow_bets_created": bets,
        "premature_forecasts_pruned": pruned,
        "forecast_horizon_days": HORIZON_DAYS,
    }


def install():
    core.make_forecasts = _make_forecasts_nearterm
    return {
        "installed": True,
        "version": VERSION,
        "horizon_days": HORIZON_DAYS,
        "policy": "forecast only near-term fixtures; regenerate far-future games when they enter horizon",
        "research_only": True,
    }
