from __future__ import annotations

"""Small runtime/reporting corrections for the 20 Oct 2026 research stack.

This patch is intentionally non-execution:
- prevents the retired 19-Oct HR boost wrapper from forcing today's HR cap back to 500;
- adds forward/open counts to the frozen singles shadow portfolio and dashboard;
- makes TPRED market-Brier comparisons explicitly paired and provisional until 500 market-comparable matches.

No selection/model coefficients, staking, same-book rules or betting authority are changed.
"""

from datetime import datetime, timezone
from html import escape
from typing import Any, Dict, Mapping, Sequence
from zoneinfo import ZoneInfo

import frozen_shadow_dashboard_patch as frozen_dashboard
import frozen_shadow_portfolio_patch as portfolio
import hit_rate_daily_boost_patch as legacy_hr_boost
import hit_rate_multiples_lab as hr
import tennis_predictive as tpred12
import tennis_predictive3 as tpred3
import tennis_predictive_dashboard as tpred_dashboard

VERSION = "OCT20_RUNTIME_REPORTING_FIXES_V1"
DASHBOARD_VERSION = "0.19.76"
MARKET_COMPARISON_MIN_SAMPLES = 500
LONDON = ZoneInfo("Europe/London")

_INSTALLED = False
_ORIGINAL_PORTFOLIO_SCOREBOARD = None
_ORIGINAL_FROZEN_SECTION = None
_ORIGINAL_TPRED12_METRICS = None
_ORIGINAL_TPRED3_METRICS = None
_ORIGINAL_TPRED_MODEL_PANEL = None


def _parse_dt(value: Any):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _selection_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    point = row.get("point")
    if point is None or point == "":
        point_key = ""
    else:
        try:
            point_key = f"{float(point):.4f}"
        except Exception:
            point_key = str(point)
    return (
        str(row.get("event_id") or ""),
        str(row.get("market_key") or ""),
        str(row.get("selection") or ""),
        point_key,
    )


def _query(db, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in db.fetchall(sql, tuple(params))]
    except Exception:
        return []


def _open_lane_rows(db, cohort_key: str, boundary: str) -> list[dict[str, Any]]:
    if cohort_key == "PRED4_ALL":
        rows = []
        rows.extend(_query(db, """
            SELECT b.event_id,'h2h' AS market_key,b.selection,NULL AS point,
                   b.created_at,e.commence_time
            FROM football_predictive4_bets b
            JOIN events e ON e.event_id=b.event_id
            WHERE b.status='OPEN' AND e.status='UPCOMING' AND b.created_at>=?
              AND b.offered_odds>1.0
        """, (boundary,)))
        rows.extend(_query(db, """
            SELECT b.event_id,b.market_key,b.selection,b.point,
                   b.created_at,e.commence_time
            FROM football_predictive4_market_bets b
            JOIN events e ON e.event_id=b.event_id
            WHERE b.status='OPEN' AND e.status='UPCOMING' AND b.created_at>=?
              AND b.offered_odds>1.0
        """, (boundary,)))
        return rows

    if cohort_key == "PRED12_BTTS":
        rows = []
        for table in ("football_predictive_market_bets", "football_predictive2_market_bets"):
            rows.extend(_query(db, f"""
                SELECT b.event_id,b.market_key,b.selection,b.point,
                       b.created_at,e.commence_time
                FROM {table} b
                JOIN events e ON e.event_id=b.event_id
                WHERE b.status='OPEN' AND e.status='UPCOMING' AND b.created_at>=?
                  AND b.market_key='btts' AND b.offered_odds>1.0
            """, (boundary,)))
        return rows

    ranges = {
        "NCAAF_3_TO_4_99": ("ncaaf", 3.0, 5.0),
        "NFL_2_TO_2_99": ("nfl", 2.0, 3.0),
        "EUROLEAGUE_1_50_1_99": ("basketball_euroleague", 1.5, 2.0),
        "EUROLEAGUE_3_TO_3_99": ("basketball_euroleague", 3.0, 4.0),
    }
    spec = ranges.get(cohort_key)
    if not spec:
        return []
    sport, lo, hi = spec
    if sport in {"ncaaf", "nfl"}:
        sport_clause = "lower(e.sport_key) LIKE ?"
        sport_param = f"%{sport}%"
    else:
        sport_clause = "lower(e.sport_key)=?"
        sport_param = sport
    return _query(db, f"""
        SELECT b.event_id,'h2h' AS market_key,b.selection,NULL AS point,
               b.created_at,e.commence_time
        FROM multisport_execution_bets b
        JOIN multisport_events e ON e.event_id=b.event_id
        WHERE b.status='OPEN' AND e.status='UPCOMING' AND b.created_at>=?
          AND {sport_clause} AND b.offered_odds>=? AND b.offered_odds<?
    """, (boundary, sport_param, lo, hi))


def _open_portfolio_counts(db, state: Mapping[str, Any]) -> Dict[str, Any]:
    start = str(state.get("portfolio_started_at") or "")
    registry = {
        str(r.get("cohort_key")): dict(r)
        for r in _query(db, "SELECT * FROM outcome_edge_watch_cohorts")
    }
    candidates = []
    for rule in portfolio.POLICY:
        if not bool(rule.get("included")) or float(rule.get("stake_gbp") or 0.0) <= 0:
            continue
        key = str(rule.get("cohort_key") or "")
        frozen_at = str((registry.get(key) or {}).get("frozen_at") or start)
        boundary = max(start, frozen_at)
        for row in _open_lane_rows(db, key, boundary):
            candidates.append((str(row.get("created_at") or ""), int(rule.get("priority") or 999), key, row))

    candidates.sort(key=lambda x: (x[0], x[1], _selection_key(x[3])))
    chosen: dict[tuple[str, str, str, str], tuple[str, dict[str, Any]]] = {}
    for _, _, lane, row in candidates:
        chosen.setdefault(_selection_key(row), (lane, row))

    now = datetime.now(timezone.utc)
    today_local = now.astimezone(LONDON).date()
    upcoming = []
    for lane, row in chosen.values():
        kickoff = _parse_dt(row.get("commence_time"))
        if kickoff is None or kickoff <= now:
            continue
        item = dict(row)
        item["lane"] = lane
        item["kickoff_utc"] = kickoff.isoformat()
        upcoming.append(item)

    today = [
        r for r in upcoming
        if _parse_dt(r.get("commence_time")).astimezone(LONDON).date() == today_local
    ]
    return {
        "open_unique_bets": len(upcoming),
        "kickoff_today": len(today),
        "today_timezone": "Europe/London",
    }


def _paired_metrics(original):
    def wrapped(rows):
        rows = list(rows)
        out = dict(original(rows))
        comparable = [r for r in rows if r.get("market_brier") is not None]
        market_brier = (
            sum(float(r.get("market_brier")) for r in comparable) / len(comparable)
            if comparable else None
        )
        matched_model_brier = (
            sum(float(r.get("brier")) for r in comparable) / len(comparable)
            if comparable else None
        )
        raw_delta = None
        if market_brier and matched_model_brier is not None and market_brier > 0:
            raw_delta = (market_brier - matched_model_brier) / market_brier * 100.0
        n = len(comparable)
        ready = n >= MARKET_COMPARISON_MIN_SAMPLES
        out.update({
            "market_samples": n,
            "market_brier": market_brier,
            "matched_model_brier": matched_model_brier,
            "provisional_brier_improvement_vs_market_pct": raw_delta,
            "market_comparison_ready": ready,
            "market_comparison_min_samples": MARKET_COMPARISON_MIN_SAMPLES,
            "brier_improvement_vs_market_pct": raw_delta if ready else None,
        })
        return out
    return wrapped


def _fmt(value: Any, digits: int = 4, suffix: str = "") -> str:
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except Exception:
        return "—"


def _tpred_model_panel(name: str, model: Mapping[str, Any]) -> str:
    ready = dict(model.get("evidence_ready_walkforward") or {})
    all_wf = dict(model.get("all_walkforward") or {})
    n = int(ready.get("market_samples") or 0)
    threshold = int(ready.get("market_comparison_min_samples") or MARKET_COMPARISON_MIN_SAMPLES)
    raw_delta = ready.get("provisional_brier_improvement_vs_market_pct")
    comparison_ready = bool(ready.get("market_comparison_ready"))
    if n <= 0:
        vs_value = "—"
        vs_note = "no paired market sample yet"
    elif comparison_ready:
        vs_value = _fmt(raw_delta, 1, "%")
        vs_note = f"paired on the same {n} matches"
    else:
        vs_value = "PROVISIONAL"
        vs_note = f"{n}/{threshold} paired · raw Δ {_fmt(raw_delta,1,'%')}"

    metric = tpred_dashboard._metric
    return f"""
    <div class='panel'>
      <h3>{escape(name)}</h3>
      <div class='grid compact'>
        {metric("Walk-forward samples", str(ready.get("samples") or 0), f"{all_wf.get('samples') or 0} including low-history matches")}
        {metric("Brier — all ready", _fmt(ready.get("brier"),4), "lower is better")}
        {metric("Model Brier — paired", _fmt(ready.get("matched_model_brier"),4), f"same {n} matches as market")}
        {metric("Market Brier — paired", _fmt(ready.get("market_brier"),4), f"same {n} matches as model")}
        {metric("Vs market", vs_value, vs_note)}
        {metric("Log loss", _fmt(ready.get("log_loss"),4), "all evidence-ready matches")}
      </div>
    </div>
    """


def install(db) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_PORTFOLIO_SCOREBOARD, _ORIGINAL_FROZEN_SECTION
    global _ORIGINAL_TPRED12_METRICS, _ORIGINAL_TPRED3_METRICS, _ORIGINAL_TPRED_MODEL_PANEL
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    # The legacy 19-Oct wrapper is still around the HR refresh function. Make its
    # post-19-Oct value agree with the new paced policy instead of silently forcing 500.
    def legacy_effective_cap(now=None):
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        day = now.astimezone(timezone.utc).date().isoformat()
        if day == "2026-10-19":
            return 1250
        if day == "2026-10-20":
            return 650
        return 400

    legacy_hr_boost._effective_cap = legacy_effective_cap
    hr.DAILY_REFRESH_CREDIT_BUDGET = legacy_effective_cap()

    _ORIGINAL_PORTFOLIO_SCOREBOARD = portfolio.scoreboard
    def scoreboard_with_open(db_obj):
        state = dict(_ORIGINAL_PORTFOLIO_SCOREBOARD(db_obj))
        state.update(_open_portfolio_counts(db_obj, state))
        return state
    portfolio.scoreboard = scoreboard_with_open

    _ORIGINAL_FROZEN_SECTION = frozen_dashboard._section
    def section_with_open_tile(db_obj):
        html = _ORIGINAL_FROZEN_SECTION(db_obj)
        try:
            state = portfolio.scoreboard(db_obj)
            tile = (
                "<div class='card metric-card'><div class='label'>Open shadow bets</div>"
                f"<div class='value'>{int(state.get('open_unique_bets') or 0)}</div>"
                f"<div class='muted'>{int(state.get('kickoff_today') or 0)} kick off today · Europe/London</div></div>"
            )
            html = html.replace("<div class='grid compact'>", "<div class='grid compact'>" + tile, 1)
        except Exception:
            pass
        return html
    frozen_dashboard._section = section_with_open_tile
    frozen_dashboard.DASHBOARD_VERSION = DASHBOARD_VERSION
    try:
        import dashboard_layout_v2
        dashboard_layout_v2.VERSION = DASHBOARD_VERSION
    except Exception:
        pass

    _ORIGINAL_TPRED12_METRICS = tpred12._metrics
    _ORIGINAL_TPRED3_METRICS = tpred3._metrics
    _ORIGINAL_TPRED_MODEL_PANEL = tpred_dashboard._model_panel
    tpred12._metrics = _paired_metrics(_ORIGINAL_TPRED12_METRICS)
    tpred3._metrics = _paired_metrics(_ORIGINAL_TPRED3_METRICS)
    tpred_dashboard._model_panel = _tpred_model_panel
    tpred_dashboard.VERSION = "TPRED_DASHBOARD_V2_3_PAIRED_MARKET"

    state = portfolio.scoreboard(db)
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "dashboard_version": DASHBOARD_VERSION,
        "hr_effective_cap": int(hr.DAILY_REFRESH_CREDIT_BUDGET),
        "frozen_open_unique_bets": int(state.get("open_unique_bets") or 0),
        "frozen_kickoff_today": int(state.get("kickoff_today") or 0),
        "tpred_market_comparison_min_samples": MARKET_COMPARISON_MIN_SAMPLES,
        "tpred_market_comparison": "paired same-match Brier; headline delta withheld below threshold",
        "betting_authority_changed": False,
    }
