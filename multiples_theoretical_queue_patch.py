from __future__ import annotations

"""Pre-price candidate queues for Live Yankee and HR1/HR2.

The previous forward lanes were seeded mainly from already-created execution
shadow bets.  That made the visible queue much smaller than the underlying
research opportunity set because a usable execution/reference price generally
had to exist before a selection could even enter the queue.

This patch moves observation upstream while preserving the placeability rules:
- theoretical/model/reference candidates may enter the queue up to 72h out;
- paid manual-book polling still uses the existing ~30h horizon/cadence/caps;
- same-book, fresh-price, target-band and minimum-odds checks are unchanged;
- no betting/execution authority is added;
- HR gets a new policy version so old forward evidence stays immutable.
"""

from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any, Dict, Mapping, Optional, Sequence

import hit_rate_multiples_lab as hr
import live_yankee_feed as ly
import live_yankee_results_patch as ly_results
from db import utc_now_iso

VERSION = "MULTIPLES_THEORETICAL_QUEUE_V1"
HR_POLICY_VERSION = "HRM_2026-10-18_V3_THEORETICAL_QUEUE"
QUEUE_VISIBILITY_HOURS = 72.0
PRICE_POLL_HORIZON_HOURS = float(getattr(hr, "FORMATION_HORIZON_HOURS", 30.0))

_INSTALLED = False
_ORIGINAL_HR_SOURCE_ROWS = None
_ORIGINAL_HR_KEY = None
_ORIGINAL_HR_SOURCE_RESULT = None
_ORIGINAL_LY_SOURCE_ROWS = None
_ORIGINAL_LY_SOURCE_RESULT = None
_ORIGINAL_HR_PAGE = None


def _parse(value: Any):
    return ly._parse_iso(value)


def _visible(value: Any, now: datetime) -> bool:
    dt = _parse(value)
    return bool(dt and now < dt <= now + timedelta(hours=QUEUE_VISIBILITY_HOURS))


def _point_key(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.8f}".rstrip("0").rstrip(".")
    except Exception:
        return str(value or "").strip().lower()


def _latest(rows: Sequence[Mapping[str, Any]], *, market_default: str = "h2h") -> list[dict[str, Any]]:
    """Keep the newest evaluation for each underlying selection."""
    out = []
    seen = set()
    for raw in rows:
        row = dict(raw)
        key = (
            str(row.get("event_id") or ""),
            str(row.get("market_key") or market_default).lower(),
            str(row.get("selection") or "").strip().lower(),
            _point_key(row.get("point")),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _football_eval_rows(db, tables: Sequence[tuple[str, str]], now: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for table, lane in tables:
        is_market = "market_evaluations" in table
        market_cols = "v.market_key,v.point" if is_market else "'h2h' AS market_key,NULL AS point"
        try:
            rows = db.fetchall(
                f"""SELECT v.id AS source_id,v.evaluated_at AS source_created_at,v.event_id,
                           {market_cols},v.selection,v.min_required_odds,
                           v.model_fair_odds,v.best_executable_odds,v.edge_pct,v.decision,v.reason,
                           e.sport_key,e.league,e.commence_time,e.home_team,e.away_team
                    FROM {table} v JOIN events e ON e.event_id=v.event_id
                    WHERE e.status='UPCOMING'
                    ORDER BY v.evaluated_at DESC,v.id DESC"""
            )
        except Exception:
            rows = []
        for row in _latest(rows):
            if not _visible(row.get("commence_time"), now):
                continue
            try:
                minimum = float(row.get("min_required_odds") or 0.0)
            except Exception:
                continue
            if minimum <= 1.0:
                continue
            row["source_model_lane"] = lane
            out.append(row)
    return out


def _tennis_eval_rows(db, now: datetime) -> list[dict[str, Any]]:
    try:
        rows = db.fetchall(
            """SELECT v.id AS source_id,v.evaluated_at AS source_created_at,v.event_id,
                      'h2h' AS market_key,NULL AS point,v.selection,v.min_required_odds,
                      v.fair_odds AS model_fair_odds,v.best_executable_odds,v.edge_pct,v.decision,v.reason,
                      e.sport_key,e.tournament_title AS league,e.commence_time,
                      e.player_one AS home_team,e.player_two AS away_team
               FROM tennis_execution_evaluations v
               JOIN tennis_events e ON e.event_id=v.event_id
               WHERE e.status='UPCOMING'
               ORDER BY v.evaluated_at DESC,v.id DESC"""
        )
    except Exception:
        rows = []
    return [r for r in _latest(rows) if _visible(r.get("commence_time"), now)]


def _multisport_eval_rows(db, now: datetime) -> list[dict[str, Any]]:
    try:
        rows = db.fetchall(
            """SELECT v.id AS source_id,v.evaluated_at AS source_created_at,v.event_id,
                      'h2h' AS market_key,NULL AS point,v.selection,v.min_required_odds,
                      v.fair_odds AS model_fair_odds,v.best_executable_odds,v.edge_pct,v.decision,v.reason,
                      e.sport_key,e.league_title AS league,e.commence_time,e.home_team,e.away_team
               FROM multisport_execution_evaluations v
               JOIN multisport_events e ON e.event_id=v.event_id
               WHERE e.status='UPCOMING'
               ORDER BY v.evaluated_at DESC,v.id DESC"""
        )
    except Exception:
        rows = []
    return [r for r in _latest(rows) if _visible(r.get("commence_time"), now)]


def _canonical_football_rows(db, now: datetime) -> list[dict[str, Any]]:
    """Reference/canonical football selections before approved-venue execution."""
    try:
        rows = db.fetchall(
            """SELECT c.id AS source_id,c.created_at AS source_created_at,c.event_id,
                      c.market_key,c.selection,c.point,s.min_odds,
                      c.offered_odds AS source_odds,c.edge_pct,
                      e.sport_key,e.league,e.commence_time,e.home_team,e.away_team
               FROM canonical_bets c
               JOIN signals s ON s.id=c.source_signal_id
               JOIN events e ON e.event_id=c.event_id
               WHERE c.status='OPEN' AND e.status='UPCOMING'
               ORDER BY c.created_at,c.id"""
        )
    except Exception:
        rows = []
    return [dict(r) for r in rows if _visible(r.get("commence_time"), now)]


def _target_price(minimum: float, lo: float, hi: float) -> Optional[float]:
    """Representative queue target; actual quote still has to land in [lo,hi)."""
    if minimum <= 1.0 or minimum >= hi - 1e-12:
        return None
    target = max(float(lo), float(minimum))
    return target if target < hi - 1e-12 else None


def _hr_add(
    out: list[dict[str, Any]],
    row: Mapping[str, Any],
    *,
    exp: str,
    lane: str,
    lo: float,
    hi: float,
    source_table: str,
) -> None:
    try:
        minimum = float(row.get("min_required_odds") or row.get("min_odds") or 0.0)
    except Exception:
        return
    target = _target_price(minimum, lo, hi)
    if target is None:
        return
    item = dict(row)
    item.update(
        experiment_key=exp,
        source_lane=lane,
        source_table=source_table,
        min_odds=minimum,
        source_odds=target,
        band_min=float(lo),
        band_max=float(hi),
    )
    out.append(item)


def _hr_theoretical_rows(db, now: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    # Existing canonical/reference football opportunities no longer need an
    # approved execution-shadow bet before joining the HR queue.
    for row in _canonical_football_rows(db, now):
        for exp, meta in hr._qualify("FOOTBALL", row).items():
            item = dict(row)
            item.update(
                experiment_key=exp,
                source_lane=meta["lane"],
                source_table="canonical_bets",
                band_min=float(meta["lo"]),
                band_max=float(meta["hi"]),
            )
            out.append(item)

    # Predictive football rows can enter before any acceptable execution price.
    football = _football_eval_rows(
        db,
        (
            ("football_predictive_evaluations", "PRED1"),
            ("football_predictive_market_evaluations", "PRED1"),
            ("football_predictive2_evaluations", "PRED2"),
            ("football_predictive2_market_evaluations", "PRED2"),
        ),
        now,
    )
    for row in football:
        market = str(row.get("market_key") or "").lower()
        selection = str(row.get("selection") or "").strip().lower()
        source = f"THEORETICAL_{row.get('source_model_lane')}_{market.upper()}"
        if market == "btts":
            _hr_add(out, row, exp="HR1", lane="FOOTBALL_BTTS_1_80_1_99", lo=1.8, hi=2.0, source_table=source)
            _hr_add(out, row, exp="HR2", lane="FOOTBALL_BTTS_1_80_1_99", lo=1.8, hi=2.0, source_table=source)
            if selection == "no":
                _hr_add(out, row, exp="HR2", lane="PRED12_BTTS_NO_2_50_2_99", lo=2.5, hi=3.0, source_table=source)
        elif market == "totals":
            _hr_add(out, row, exp="HR1", lane="FOOTBALL_TOTALS_1_80_1_99", lo=1.8, hi=2.0, source_table=source)
            _hr_add(out, row, exp="HR2", lane="FOOTBALL_TOTALS_1_80_2_99", lo=1.8, hi=3.0, source_table=source)
        elif market == "h2h":
            _hr_add(out, row, exp="HR2", lane="FOOTBALL_H2H_2_50_2_99", lo=2.5, hi=3.0, source_table=source)

    for row in _tennis_eval_rows(db, now):
        _hr_add(out, row, exp="HR1", lane="TENNIS_H2H_1_80_1_99", lo=1.8, hi=2.0, source_table="THEORETICAL_TENNIS_H2H")
        _hr_add(out, row, exp="HR2", lane="TENNIS_H2H_1_80_2_99", lo=1.8, hi=3.0, source_table="THEORETICAL_TENNIS_H2H")

    for row in _multisport_eval_rows(db, now):
        sport = str(row.get("sport_key") or "").lower()
        if sport == "basketball_euroleague":
            _hr_add(out, row, exp="HR1", lane="EUROLEAGUE_H2H_1_50_2_99", lo=1.5, hi=3.0, source_table="THEORETICAL_MULTISPORT_H2H")
            _hr_add(out, row, exp="HR2", lane="EUROLEAGUE_H2H_1_50_2_99", lo=1.5, hi=3.0, source_table="THEORETICAL_MULTISPORT_H2H")
            _hr_add(out, row, exp="HR2", lane="EUROLEAGUE_H2H_3_00_3_99", lo=3.0, hi=4.0, source_table="THEORETICAL_MULTISPORT_H2H")
        elif "nfl" in sport:
            _hr_add(out, row, exp="HR1", lane="NFL_H2H_1_50_2_99", lo=1.5, hi=3.0, source_table="THEORETICAL_MULTISPORT_H2H")
            _hr_add(out, row, exp="HR2", lane="NFL_H2H_1_50_2_99", lo=1.5, hi=3.0, source_table="THEORETICAL_MULTISPORT_H2H")
        elif sport == "baseball_mlb":
            _hr_add(out, row, exp="HR1", lane="MLB_H2H_1_50_1_99", lo=1.5, hi=2.0, source_table="THEORETICAL_MULTISPORT_H2H")
            _hr_add(out, row, exp="HR2", lane="MLB_H2H_1_50_1_99", lo=1.5, hi=2.0, source_table="THEORETICAL_MULTISPORT_H2H")
    return out


def _hr_source_rows(db, now: datetime):
    rows = list(_ORIGINAL_HR_SOURCE_ROWS(db, now))
    rows.extend(_hr_theoretical_rows(db, now))
    return sorted(rows, key=lambda r: (
        str(r.get("source_created_at") or ""),
        str(r.get("experiment_key") or ""),
        str(r.get("source_lane") or ""),
        int(r.get("source_id") or 0),
    ))


def _hr_key(policy: str, exp: str, system: str, row: Mapping[str, Any]) -> str:
    if policy != HR_POLICY_VERSION:
        return _ORIGINAL_HR_KEY(policy, exp, system, row)
    raw = "|".join([
        policy,
        exp,
        system,
        str(row.get("event_id") or "").lower(),
        str(row.get("market_key") or "").lower(),
        str(row.get("selection") or "").lower(),
        _point_key(row.get("point")),
        str(row.get("source_lane") or "").lower(),
        f"{float(row.get('band_min') or 0):.4f}",
        f"{float(row.get('band_max') or 0):.4f}",
    ])
    return hashlib.sha256(raw.encode()).hexdigest()


def _ly_add(
    out: list[dict[str, Any]],
    row: Mapping[str, Any],
    *,
    lane: str,
    lo: Optional[float],
    hi: Optional[float],
    source_table: str,
) -> None:
    try:
        minimum = float(row.get("min_required_odds") or row.get("min_odds") or 0.0)
    except Exception:
        return
    if minimum <= 1.0:
        return
    if lo is None or hi is None:
        target = max(float(row.get("model_fair_odds") or minimum), 1.0001)
    else:
        target = _target_price(minimum, float(lo), float(hi))
        if target is None:
            return
    item = dict(row)
    item.update(
        source_lane=lane,
        source_table=source_table,
        min_odds=minimum,
        source_odds=float(target),
        source_bookmaker_key="",
        source_bookmaker_title="",
    )
    out.append(item)


def _ly_theoretical_rows(db, now: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    # Canonical football reference selections at the focused 4.00-4.99 band,
    # irrespective of whether an approved execution venue could create a bet.
    for row in _canonical_football_rows(db, now):
        try:
            price = float(row.get("source_odds") or 0.0)
        except Exception:
            continue
        if 4.0 <= price < 5.0 and float(row.get("min_odds") or 0.0) > 1.0:
            item = dict(row)
            item.update(source_lane="FOOTBALL_4_TO_4_99", source_table="canonical_bets")
            out.append(item)

    # PRED4 is explicitly model-driven in the Yankee lane. Every frozen model
    # selection has a minimum acceptable price; it may queue before that price
    # exists and only becomes placeable if a fresh manual quote later clears it.
    pred4 = _football_eval_rows(
        db,
        (
            ("football_predictive4_evaluations", "PRED4"),
            ("football_predictive4_market_evaluations", "PRED4"),
        ),
        now,
    )
    for row in pred4:
        _ly_add(
            out,
            row,
            lane="PRED4",
            lo=None,
            hi=None,
            source_table=f"THEORETICAL_PRED4_{str(row.get('market_key') or 'h2h').upper()}",
        )

    # The focused football 4-4.99 lane can also observe PRED1/2 forecast
    # selections before a live price appears. The final price must still be
    # 4.00-4.99 and at/above the model minimum.
    football = _football_eval_rows(
        db,
        (
            ("football_predictive_evaluations", "PRED1"),
            ("football_predictive_market_evaluations", "PRED1"),
            ("football_predictive2_evaluations", "PRED2"),
            ("football_predictive2_market_evaluations", "PRED2"),
        ),
        now,
    )
    for row in football:
        _ly_add(
            out,
            row,
            lane="FOOTBALL_4_TO_4_99",
            lo=4.0,
            hi=5.0,
            source_table=f"THEORETICAL_{row.get('source_model_lane')}_{str(row.get('market_key') or 'h2h').upper()}",
        )

    for row in _multisport_eval_rows(db, now):
        sport = str(row.get("sport_key") or "").lower()
        if "ncaaf" in sport:
            _ly_add(out, row, lane="NCAAF_3_TO_4_99", lo=3.0, hi=5.0, source_table="THEORETICAL_MULTISPORT_H2H")
        elif "nfl" in sport:
            _ly_add(out, row, lane="NFL_2_TO_4_99", lo=2.0, hi=5.0, source_table="THEORETICAL_MULTISPORT_H2H")
        elif sport.startswith("basketball_"):
            _ly_add(out, row, lane="BASKETBALL_1_50_TO_3_99", lo=1.5, hi=4.0, source_table="THEORETICAL_MULTISPORT_H2H")
        elif sport.startswith("baseball_"):
            _ly_add(out, row, lane="BASEBALL_1_50_TO_1_99", lo=1.5, hi=2.0, source_table="THEORETICAL_MULTISPORT_H2H")
    return out


def _ly_source_rows(db, start_iso: str, now: datetime):
    rows = [dict(x) for x in _ORIGINAL_LY_SOURCE_ROWS(db, start_iso, now)]
    rows.extend(_ly_theoretical_rows(db, now))
    # Keep exactly one underlying selection in the Yankee queue. If an existing
    # priced/reference source already exists it wins because original rows are
    # added first; otherwise the pre-price model row seeds the candidate.
    seen = set()
    out = []
    for row in rows:
        key = ly._candidate_key(row.get("event_id"), row.get("market_key"), row.get("selection"), row.get("point"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return sorted(out, key=lambda r: (
        str(r.get("source_created_at") or ""),
        str(r.get("source_lane") or ""),
        int(r.get("source_id") or 0),
    ))


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _football_result(db, candidate: Mapping[str, Any]) -> Optional[str]:
    event_id = str(candidate.get("event_id") or "")
    row = db.fetchone("SELECT * FROM event_results WHERE event_id=?", (event_id,)) or {}
    if not row:
        return None
    event = db.fetchone("SELECT home_team,away_team FROM events WHERE event_id=?", (event_id,)) or {}
    try:
        home = int(row.get("home_score")); away = int(row.get("away_score"))
    except Exception:
        return None
    market = str(candidate.get("market_key") or "h2h").lower()
    selection = _norm(candidate.get("selection"))
    if market == "h2h":
        winner = _norm(event.get("home_team")) if home > away else _norm(event.get("away_team")) if away > home else "draw"
        return "WIN" if selection == winner else "LOSS"
    if market == "btts":
        hit = home > 0 and away > 0
        wants = selection in {"yes", "y", "true"}
        return "WIN" if hit == wants else "LOSS"
    if market == "totals":
        try:
            point = float(candidate.get("point"))
        except Exception:
            return None
        total = home + away
        if abs(total - point) < 1e-12:
            return "PUSH"
        hit = total > point
        wants_over = selection.startswith("over")
        wants_under = selection.startswith("under")
        if not (wants_over or wants_under):
            return None
        return "WIN" if (hit and wants_over) or ((not hit) and wants_under) else "LOSS"
    return None


def _tennis_result(db, candidate: Mapping[str, Any]) -> Optional[str]:
    row = db.fetchone("SELECT winner FROM tennis_results WHERE event_id=?", (str(candidate.get("event_id") or ""),)) or {}
    winner = _norm(row.get("winner"))
    if not winner:
        return None
    return "WIN" if _norm(candidate.get("selection")) == winner else "LOSS"


def _multisport_result(db, candidate: Mapping[str, Any]) -> Optional[str]:
    event_id = str(candidate.get("event_id") or "")
    row = db.fetchone("SELECT home_score,away_score FROM multisport_results WHERE event_id=?", (event_id,)) or {}
    if not row:
        return None
    event = db.fetchone("SELECT home_team,away_team FROM multisport_events WHERE event_id=?", (event_id,)) or {}
    try:
        home = int(row.get("home_score")); away = int(row.get("away_score"))
    except Exception:
        return None
    if home == away:
        return "PUSH"
    winner = _norm(event.get("home_team")) if home > away else _norm(event.get("away_team"))
    return "WIN" if _norm(candidate.get("selection")) == winner else "LOSS"


def _theoretical_result(db, candidate: Mapping[str, Any]) -> Optional[str]:
    table = str(candidate.get("source_table") or "")
    if table == "canonical_bets":
        try:
            row = db.fetchone("SELECT result,status,pnl_units FROM canonical_bets WHERE id=?", (int(candidate.get("source_id") or 0),)) or {}
        except Exception:
            row = {}
        result = str(row.get("result") or "").strip().upper()
        if result in {"WIN", "LOSS", "PUSH", "VOID"}:
            return result
        # Fall back to event settlement below when canonical settlement lags.
    if table == "THEORETICAL_TENNIS_H2H":
        return _tennis_result(db, candidate)
    if table == "THEORETICAL_MULTISPORT_H2H":
        return _multisport_result(db, candidate)
    if table.startswith("THEORETICAL_") or table == "canonical_bets":
        return _football_result(db, candidate)
    return None


def _hr_source_result(db, leg: Mapping[str, Any]) -> Optional[str]:
    table = str(leg.get("source_table") or "")
    if table == "canonical_bets" or table.startswith("THEORETICAL_"):
        result = _theoretical_result(db, leg)
        if result:
            return result
    return _ORIGINAL_HR_SOURCE_RESULT(db, leg)


def _ly_source_result(db, leg: Mapping[str, Any]) -> Optional[str]:
    candidate = db.fetchone("SELECT * FROM live_yankee_candidates WHERE id=?", (int(leg["candidate_id"]),)) or {}
    table = str(candidate.get("source_table") or "")
    if table == "canonical_bets" or table.startswith("THEORETICAL_"):
        result = _theoretical_result(db, candidate)
        if result:
            return result
    return _ORIGINAL_LY_SOURCE_RESULT(db, leg)


def _hr_page(web_module) -> str:
    html = _ORIGINAL_HR_PAGE(web_module)
    # The legacy heading said Source odds. Pre-price rows deliberately carry a
    # target/reference value instead, so make the semantics explicit.
    return html.replace("<th>Source odds</th>", "<th>Queue ref/target</th>", 1)


def install(db) -> Dict[str, Any]:
    global _INSTALLED
    global _ORIGINAL_HR_SOURCE_ROWS, _ORIGINAL_HR_KEY, _ORIGINAL_HR_SOURCE_RESULT
    global _ORIGINAL_LY_SOURCE_ROWS, _ORIGINAL_LY_SOURCE_RESULT, _ORIGINAL_HR_PAGE

    if _INSTALLED:
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
            "hr_policy_version": hr.POLICY_VERSION,
        }

    _ORIGINAL_HR_SOURCE_ROWS = hr._source_rows
    _ORIGINAL_HR_KEY = hr._key
    _ORIGINAL_HR_SOURCE_RESULT = hr._source_result
    _ORIGINAL_LY_SOURCE_ROWS = ly._source_rows
    _ORIGINAL_LY_SOURCE_RESULT = ly_results._source_result
    _ORIGINAL_HR_PAGE = hr._page

    # New policy version prevents the broader pre-price candidate universe from
    # being mixed into the older HR forward evidence. Existing cards/results stay
    # untouched; old unconsumed queues are retired at the policy boundary.
    old_policy = str(hr.POLICY_VERSION)
    hr.POLICY_VERSION = HR_POLICY_VERSION
    hr._source_rows = _hr_source_rows
    hr._key = _hr_key
    hr._source_result = _hr_source_result
    hr._page = _hr_page

    ly._source_rows = _ly_source_rows
    ly.ALGORITHM_VERSION = "YANKEE_LIVE_FEED_V3_PREPRICE_QUEUE"
    ly_results._source_result = _ly_source_result

    stamp = utc_now_iso()
    try:
        db.execute(
            "UPDATE hit_rate_candidates SET status='EXPIRED',expired_at=? WHERE status='QUEUED' AND policy_version<>?",
            (stamp, HR_POLICY_VERSION),
        )
    except Exception:
        pass
    try:
        hr._activate_policy(db)
    except Exception:
        pass
    try:
        ly._ensure_schema(db)
        state = db.fetchone("SELECT source_policy_json FROM live_yankee_state WHERE singleton_id=1") or {}
        import json
        try:
            policy = json.loads(str(state.get("source_policy_json") or "{}"))
        except Exception:
            policy = {}
        policy.update({
            "queue_entry": "pre-price theoretical/model/reference candidate",
            "queue_visibility_hours": QUEUE_VISIBILITY_HOURS,
            "price_poll_horizon_hours": PRICE_POLL_HORIZON_HOURS,
            "placeability_rules_changed": False,
        })
        db.execute(
            "UPDATE live_yankee_state SET algorithm_version=?,source_policy_json=? WHERE singleton_id=1",
            (ly.ALGORITHM_VERSION, json.dumps(policy, sort_keys=True)),
        )
    except Exception:
        pass

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "old_hr_policy_version": old_policy,
        "hr_policy_version": HR_POLICY_VERSION,
        "yankee_algorithm_version": ly.ALGORITHM_VERSION,
        "queue_visibility_hours": QUEUE_VISIBILITY_HOURS,
        "price_poll_horizon_hours": PRICE_POLL_HORIZON_HOURS,
        "provider_calls_added": 0,
        "same_book_rules_changed": False,
        "minimum_odds_rules_changed": False,
        "betting_authority_changed": False,
    }
