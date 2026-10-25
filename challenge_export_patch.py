from __future__ import annotations

"""Fast challenge-only export for currently actionable betting research.

This deliberately avoids the historical/full export builders. It reads only
future/open candidate and model rows for the next 48 hours plus a compact,
locally-computed settled lane-health snapshot. It makes no provider/API calls.
Research/read-only; no selection, staking or betting authority changes.
"""

import json
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any, Dict, Iterable, Mapping, Optional

from fastapi.responses import Response

from exporter import _json_safe, _rows_to_csv, _table_exists

VERSION = "CHALLENGE_EXPORT_V2_LANE_HEALTH"
HORIZON_HOURS = 48
ROUTE = "/export/challenge.zip"
_INSTALLED = False

_ODDS_BANDS = (
    (0.0, 1.50, "<1.50"),
    (1.50, 2.00, "1.50-1.99"),
    (2.00, 3.00, "2.00-2.99"),
    (3.00, 4.00, "3.00-3.99"),
    (4.00, 5.00, "4.00-4.99"),
    (5.00, 7.50, "5.00-7.49"),
    (7.50, float("inf"), "7.50+"),
)


def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if out.tzinfo is None:
            out = out.replace(tzinfo=timezone.utc)
        return out.astimezone(timezone.utc)
    except Exception:
        return None


def _in_window(value: Any, now: datetime, end: datetime) -> bool:
    dt = _parse(value)
    return bool(dt and now < dt <= end)


def _safe_rows(db, table: str, sql: str | None = None, params=()) -> list[dict[str, Any]]:
    try:
        if not _table_exists(db, table):
            return []
        query = sql or f"SELECT * FROM {table}"
        return [dict(x) for x in db.fetchall(query, params)]
    except Exception:
        return []


def _event_map(db, table: str, now: datetime, end: datetime) -> dict[str, dict[str, Any]]:
    rows = _safe_rows(db, table)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not _in_window(row.get("commence_time"), now, end):
            continue
        eid = str(row.get("event_id") or "")
        if eid:
            out[eid] = row
    return out


def _event_meta(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "commence_time": row.get("commence_time"),
        "sport_key": row.get("sport_key") or row.get("tour"),
        "league": row.get("league") or row.get("league_title") or row.get("tournament_title"),
        "home_team": row.get("home_team") or row.get("player_one"),
        "away_team": row.get("away_team") or row.get("player_two"),
    }


def _first(row: Mapping[str, Any], *keys: str):
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def _number(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _odds_band(value: Any) -> str:
    odds = _number(value)
    if odds is None:
        return ""
    for low, high, label in _ODDS_BANDS:
        if low <= odds < high:
            return label
    return ""


def _normal_candidate(source: str, bet: Mapping[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    meta = _event_meta(event)
    offered_odds = _first(bet, "offered_odds", "source_odds", "entry_odds")
    return {
        "source": source,
        "source_table": bet.get("_source_table"),
        "source_id": bet.get("id") or bet.get("source_id"),
        "event_id": bet.get("event_id") or event.get("event_id"),
        **meta,
        "market_key": _first(bet, "market_key") or "h2h",
        "selection": bet.get("selection"),
        "point": _first(bet, "point", "line_point"),
        "bookmaker_key": _first(bet, "bookmaker_key", "source_bookmaker_key"),
        "bookmaker_title": _first(bet, "bookmaker_title", "source_bookmaker_title"),
        "offered_odds": offered_odds,
        "odds_band": _odds_band(offered_odds),
        "fair_probability": _first(bet, "model_probability", "fair_probability", "estimated_probability"),
        "fair_odds": _first(bet, "model_fair_odds", "fair_odds"),
        "edge_pct": bet.get("edge_pct"),
        "min_odds": bet.get("min_odds"),
        "status": bet.get("status"),
        "created_at": _first(bet, "created_at", "first_seen_at", "generated_at", "source_created_at"),
        "notes": _first(bet, "strong_candidate", "confidence", "sample_quality", "source_lane"),
    }


def _future_candidates(db, now: datetime, end: datetime) -> list[dict[str, Any]]:
    football_events = _event_map(db, "events", now, end)
    multisport_events = _event_map(db, "multisport_events", now, end)
    tennis_events = _event_map(db, "tennis_events", now, end)
    out: list[dict[str, Any]] = []

    specs = (
        ("CORE", "execution_shadow_bets", football_events),
        ("PRED1_H2H", "football_predictive_bets", football_events),
        ("PRED1_MARKET", "football_predictive_market_bets", football_events),
        ("PRED2_H2H", "football_predictive2_bets", football_events),
        ("PRED2_MARKET", "football_predictive2_market_bets", football_events),
        ("PRED3_H2H", "football_predictive3_bets", football_events),
        ("PRED3_MARKET", "football_predictive3_market_bets", football_events),
        ("PRED4_H2H", "football_predictive4_bets", football_events),
        ("PRED4_MARKET", "football_predictive4_market_bets", football_events),
        ("TENNIS_EXEC", "tennis_execution_bets", tennis_events),
        ("MULTISPORT_H2H", "multisport_execution_bets", multisport_events),
        ("MULTISPORT_LINES", "multisport_line_bets", multisport_events),
    )
    for source, table, event_map in specs:
        for bet in _safe_rows(db, table, f"SELECT * FROM {table} WHERE status='OPEN'"):
            eid = str(bet.get("event_id") or "")
            event = event_map.get(eid)
            if event is None:
                continue
            bet["_source_table"] = table
            out.append(_normal_candidate(source, bet, event))

    forecasts = {
        int(r["id"]): r for r in _safe_rows(db, "api_sports_pred_forecasts")
        if r.get("id") is not None and _in_window(r.get("commence_time"), now, end)
    }
    for bet in _safe_rows(db, "api_sports_pred_shadow_bets", "SELECT * FROM api_sports_pred_shadow_bets WHERE status='OPEN'"):
        try:
            forecast = forecasts.get(int(bet.get("forecast_id")))
        except Exception:
            forecast = None
        if not forecast:
            continue
        synthetic_event = {
            "event_id": bet.get("odds_event_id") or forecast.get("odds_event_id"),
            "commence_time": forecast.get("commence_time"),
            "sport_key": forecast.get("lane_key"),
            "league": forecast.get("model_name"),
            "home_team": forecast.get("home_team"),
            "away_team": forecast.get("away_team"),
        }
        bet["_source_table"] = "api_sports_pred_shadow_bets"
        out.append(_normal_candidate(str(forecast.get("model_name") or "API_SPORTS"), bet, synthetic_event))

    price_by_candidate = {
        int(r["candidate_id"]): r for r in _safe_rows(db, "first_half_goal_entry_prices")
        if r.get("candidate_id") is not None
    }
    for row in _safe_rows(db, "first_half_goal_candidates", "SELECT * FROM first_half_goal_candidates WHERE status='OPEN'"):
        if not _in_window(row.get("commence_time"), now, end):
            continue
        price = price_by_candidate.get(int(row.get("id") or 0), {})
        bet = dict(row)
        bet.update({
            "market_key": "fhg_over_0.5",
            "selection": "Over 0.5 first-half goals",
            "offered_odds": price.get("entry_odds"),
            "bookmaker_key": price.get("bookmaker_key"),
            "bookmaker_title": price.get("bookmaker_title"),
            "fair_probability": row.get("estimated_probability"),
            "fair_odds": (1.0 / float(row["estimated_probability"])) if float(row.get("estimated_probability") or 0) > 0 else None,
            "edge_pct": price.get("model_edge_pct"),
            "_source_table": "first_half_goal_candidates",
        })
        out.append(_normal_candidate("FHG1", bet, row))

    for table, source, wanted_status in (
        ("live_yankee_candidates", "LIVE_YANKEE_QUEUE", "QUEUED"),
        ("hit_rate_candidates", "HIT_RATE_QUEUE", "QUEUED"),
    ):
        for row in _safe_rows(db, table, f"SELECT * FROM {table} WHERE status=?", (wanted_status,)):
            if not _in_window(row.get("commence_time"), now, end):
                continue
            row["_source_table"] = table
            out.append(_normal_candidate(source, row, row))

    out.sort(key=lambda r: (str(r.get("commence_time") or ""), str(r.get("source") or ""), str(r.get("selection") or "")))
    return out


def _model_support(db, now: datetime, end: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add_rows(model: str, table: str, rows: Iterable[Mapping[str, Any]]):
        for raw in rows:
            row = dict(raw)
            if not _in_window(row.get("commence_time"), now, end):
                continue
            item = {"model": model, "source_table": table}
            item.update(row)
            out.append(item)

    for model, table in (
        ("PRED1", "football_predictive_predictions"),
        ("PRED2", "football_predictive2_predictions"),
        ("PRED3", "football_predictive3_predictions"),
        ("PRED4", "football_predictive4_predictions"),
        ("API_SPORTS", "api_sports_pred_forecasts"),
        ("TPRED1", "tennis_predictive_predictions"),
        ("TPRED3", "tennis_predictive3_predictions"),
        ("TPRED4", "tennis_predictive4_predictions"),
        ("FHG1", "first_half_goal_candidates"),
    ):
        add_rows(model, table, _safe_rows(db, table))

    out.sort(key=lambda r: (str(r.get("commence_time") or ""), str(r.get("model") or ""), str(r.get("event_id") or "")))
    return out


def _evidence_grade(settled_n: int) -> tuple[str, str]:
    if settled_n >= 100:
        return "A", "ESTABLISHED"
    if settled_n >= 50:
        return "B", "DEVELOPING"
    if settled_n >= 25:
        return "C", "EARLY"
    if settled_n >= 10:
        return "D", "SMALL"
    return "E", "TINY"


def _health_record(
    source: str,
    source_table: str,
    scope: str,
    scope_key: str,
    rows: list[Mapping[str, Any]],
    *,
    sport_key: str = "",
    market_key: str = "",
    odds_band: str = "",
    evidence_kind: str = "PRICED_BETS",
) -> dict[str, Any]:
    result_rows = []
    for row in rows:
        result = str(row.get("result") or "").upper()
        if result in {"WIN", "LOSS", "PUSH", "VOID"}:
            result_rows.append(row)

    settled_n = len(result_rows)
    wins = sum(str(r.get("result") or "").upper() == "WIN" for r in result_rows)
    losses = sum(str(r.get("result") or "").upper() == "LOSS" for r in result_rows)
    pushes = sum(str(r.get("result") or "").upper() == "PUSH" for r in result_rows)
    voids = sum(str(r.get("result") or "").upper() == "VOID" for r in result_rows)
    decision_n = wins + losses

    pnl_values = []
    clv_values = []
    edge_values = []
    odds_values = []
    for row in result_rows:
        pnl = _number(_first(row, "net_pnl_units", "pnl_units"))
        if pnl is not None:
            pnl_values.append(pnl)
        clv = _number(_first(row, "clv_pct", "price_clv_pct", "clv_vs_consensus_pct"))
        if clv is not None:
            clv_values.append(clv)
        edge = _number(row.get("edge_pct"))
        if edge is not None:
            edge_values.append(edge)
        odds = _number(_first(row, "offered_odds", "source_odds", "entry_odds"))
        if odds is not None:
            odds_values.append(odds)

    net_pnl = sum(pnl_values) if pnl_values else None
    roi_denom = wins + losses + pushes
    net_roi = (100.0 * net_pnl / roi_denom) if net_pnl is not None and roi_denom else None
    grade, maturity = _evidence_grade(settled_n)
    settled_dates = [
        dt for dt in (_parse(r.get("settled_at")) for r in result_rows)
        if dt is not None
    ]

    return {
        "source": source,
        "source_table": source_table,
        "scope": scope,
        "scope_key": scope_key,
        "sport_key": sport_key,
        "market_key": market_key,
        "odds_band": odds_band,
        "evidence_kind": evidence_kind,
        "settled_n": settled_n,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "voids": voids,
        "hit_rate_pct": (100.0 * wins / decision_n) if decision_n else None,
        "net_pnl_units": net_pnl,
        "net_roi_pct": net_roi,
        "avg_offered_odds": (sum(odds_values) / len(odds_values)) if odds_values else None,
        "avg_edge_pct": (sum(edge_values) / len(edge_values)) if edge_values else None,
        "clv_samples": len(clv_values),
        "avg_clv_pct": (sum(clv_values) / len(clv_values)) if clv_values else None,
        "beat_close_pct": (100.0 * sum(v > 0 for v in clv_values) / len(clv_values)) if clv_values else None,
        "evidence_grade": grade,
        "maturity": maturity,
        "grade_basis": "sample_size_only",
        "first_settled_at": min(settled_dates).isoformat() if settled_dates else None,
        "last_settled_at": max(settled_dates).isoformat() if settled_dates else None,
    }


def _fhg_health(db) -> list[dict[str, Any]]:
    rows = _safe_rows(db, "first_half_goal_candidates", "SELECT * FROM first_half_goal_candidates WHERE status='SETTLED'")
    settled = [r for r in rows if str(r.get("fhg_hit") or "") in {"0", "1"}]
    out: list[dict[str, Any]] = []

    def record(scope: str, key: str, group: list[dict[str, Any]], league: str = ""):
        n = len(group)
        hits = sum(str(r.get("fhg_hit") or "") == "1" for r in group)
        grade, maturity = _evidence_grade(n)
        dates = [dt for dt in (_parse(r.get("settled_at")) for r in group) if dt is not None]
        out.append({
            "source": "FHG1",
            "source_table": "first_half_goal_candidates",
            "scope": scope,
            "scope_key": key,
            "sport_key": "",
            "league": league,
            "market_key": "fhg_over_0.5",
            "odds_band": "",
            "evidence_kind": "HIT_RATE_ONLY",
            "settled_n": n,
            "wins": hits,
            "losses": n - hits,
            "pushes": 0,
            "voids": 0,
            "hit_rate_pct": (100.0 * hits / n) if n else None,
            "net_pnl_units": None,
            "net_roi_pct": None,
            "avg_offered_odds": None,
            "avg_edge_pct": None,
            "clv_samples": 0,
            "avg_clv_pct": None,
            "beat_close_pct": None,
            "evidence_grade": grade,
            "maturity": maturity,
            "grade_basis": "sample_size_only",
            "first_settled_at": min(dates).isoformat() if dates else None,
            "last_settled_at": max(dates).isoformat() if dates else None,
        })

    if settled:
        record("SOURCE", "FHG1", settled)
        by_league: dict[str, list[dict[str, Any]]] = {}
        for row in settled:
            league = str(row.get("league") or "").strip()
            if league:
                by_league.setdefault(league, []).append(row)
        for league, group in sorted(by_league.items()):
            if len(group) >= 5:
                record("LEAGUE", league, group, league=league)
    return out


def _lane_health(db, candidates: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    active_pairs = {
        (str(r.get("source") or ""), str(r.get("source_table") or ""))
        for r in candidates
        if r.get("source") and r.get("source_table")
    }
    out: list[dict[str, Any]] = []
    if any(source == "FHG1" for source, _ in active_pairs):
        out.extend(_fhg_health(db))

    for source, table in sorted(active_pairs):
        if not table or source == "FHG1" or table in {"live_yankee_candidates", "hit_rate_candidates"}:
            continue

        settled = _safe_rows(db, table, f"SELECT * FROM {table} WHERE status='SETTLED'")
        if not settled:
            continue

        if table == "api_sports_pred_shadow_bets":
            settled = [r for r in settled if str(r.get("model_name") or "") == source]
        if not settled:
            continue

        out.append(_health_record(source, table, "SOURCE", source, settled))

        markets: dict[str, list[dict[str, Any]]] = {}
        sports: dict[str, list[dict[str, Any]]] = {}
        sport_markets: dict[tuple[str, str], list[dict[str, Any]]] = {}
        detailed: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

        for row in settled:
            market = str(row.get("market_key") or "h2h")
            sport = str(row.get("sport_key") or "")
            band = _odds_band(_first(row, "offered_odds", "source_odds", "entry_odds"))
            markets.setdefault(market, []).append(row)
            if sport:
                sports.setdefault(sport, []).append(row)
                sport_markets.setdefault((sport, market), []).append(row)
            if band:
                detailed.setdefault((sport, market, band), []).append(row)

        for market, group in sorted(markets.items()):
            if len(group) >= 10:
                out.append(_health_record(source, table, "MARKET", market, group, market_key=market))
        for sport, group in sorted(sports.items()):
            if len(group) >= 10:
                out.append(_health_record(source, table, "SPORT", sport, group, sport_key=sport))
        for (sport, market), group in sorted(sport_markets.items()):
            if len(group) >= 10:
                out.append(_health_record(source, table, "SPORT_MARKET", f"{sport}|{market}", group, sport_key=sport, market_key=market))
        for (sport, market, band), group in sorted(detailed.items()):
            if len(group) >= 5:
                out.append(_health_record(source, table, "ODDS_BAND", f"{sport or 'ALL'}|{market}|{band}", group, sport_key=sport, market_key=market, odds_band=band))

    scope_order = {"SOURCE": 0, "MARKET": 1, "SPORT": 2, "SPORT_MARKET": 3, "ODDS_BAND": 4, "LEAGUE": 5}
    out.sort(key=lambda r: (
        str(r.get("source") or ""),
        scope_order.get(str(r.get("scope") or ""), 99),
        str(r.get("scope_key") or ""),
    ))
    return out


def build_challenge_export(db, settings, app_version: str):
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=HORIZON_HOURS)
    candidates = _future_candidates(db, now, end)
    models = _model_support(db, now, end)
    lane_health = _lane_health(db, candidates)
    generated = now.strftime("%Y%m%dT%H%M%SZ")
    filename = f"betting-lab-challenge-{generated}.zip"
    source_counts = Counter(str(r.get("source") or "UNKNOWN") for r in candidates)
    model_counts = Counter(str(r.get("model") or "UNKNOWN") for r in models)

    payload = BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("challenge_candidates.csv", _rows_to_csv(candidates))
        z.writestr("challenge_model_support.csv", _rows_to_csv(models))
        z.writestr("challenge_lane_health.csv", _rows_to_csv(lane_health))
        z.writestr("challenge_summary.json", json.dumps(_json_safe({
            "generated_at_utc": now.isoformat(),
            "window_end_utc": end.isoformat(),
            "horizon_hours": HORIZON_HOURS,
            "candidate_rows": len(candidates),
            "model_support_rows": len(models),
            "lane_health_rows": len(lane_health),
            "candidate_sources": dict(source_counts),
            "model_sources": dict(model_counts),
            "filters": "future kickoff only for candidates; OPEN/QUEUED execution candidates; compact settled lane health only",
            "lane_health_note": "Evidence grade is sample-size maturity only, not a recommendation or quality score.",
            "provider_calls_added": 0,
            "betting_authority": False,
        }), indent=2))
        z.writestr("manifest.json", json.dumps(_json_safe({
            "app": "Project Exit Plan — Betting Lab",
            "app_version": app_version,
            "export_version": VERSION,
            "export_kind": "challenge",
            "generated_at_utc": now.isoformat(),
            "contains_secrets": False,
            "files": [
                "challenge_candidates.csv",
                "challenge_model_support.csv",
                "challenge_lane_health.csv",
                "challenge_summary.json",
            ],
        }), indent=2))
        z.writestr(
            "README.txt",
            "BETTING LAB — CHALLENGE EXPORT\n\n"
            "Purpose: fast decision support for the £10→£500 challenge.\n"
            "Contains only currently not-started research within the next 48 hours plus compact lane health.\n"
            "challenge_candidates.csv = current OPEN/QUEUED executable or near-executable candidates.\n"
            "challenge_model_support.csv = future model forecasts used to check cross-model agreement.\n"
            "challenge_lane_health.csv = compact settled evidence for active candidate sources/sublanes.\n"
            "Lane health includes settled n, hit rate, net ROI where priced, CLV where available, and sample-size maturity.\n"
            "FHG1 is HIT_RATE_ONLY until enough captured entry prices exist for genuine forward ROI/CLV.\n"
            "Evidence grade is based ONLY on settled sample size; it is not a performance or recommendation grade.\n"
            "No bulky training data or raw odds histories are included.\n"
            "This export makes zero provider calls and changes no betting logic.\n"
        )
    return payload.getvalue(), filename


def install(app, db, settings, app_version: str) -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "route": ROUTE, "version": VERSION}

    if not any(getattr(r, "path", None) == ROUTE for r in list(app.router.routes)):
        def challenge_export_route():
            payload, filename = build_challenge_export(db, settings, app_version)
            return Response(
                content=payload,
                media_type="application/zip",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Cache-Control": "no-store",
                },
            )
        app.add_api_route(ROUTE, challenge_export_route, methods=["GET"], include_in_schema=False)

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "route": ROUTE,
        "version": VERSION,
        "horizon_hours": HORIZON_HOURS,
        "provider_calls_added": 0,
        "betting_authority_changed": False,
    }
