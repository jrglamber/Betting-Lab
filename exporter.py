from __future__ import annotations

import csv
from datetime import datetime, timezone
from io import BytesIO, StringIO
import json
import zipfile
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from canonical import canonical_scoreboard
from db import sanitize_sensitive_text
from execution_shadow import execution_scoreboard, execution_funnel
from multiples_shadow import multiples_scoreboard
from manual_systems_shadow import manual_systems_scoreboard
from cohort_systems_shadow import cohort_systems_scoreboard
from instrumentation import instrumentation_report
from research_intelligence import research_intelligence, latest_weekly_reports
from tennis_shadow import tennis_scoreboard, tennis_segments
from multisport_shadow import multisport_scoreboard, multisport_segments, multisport_funnel
from multisport_lines_shadow import line_scoreboard, line_segments, line_funnel
from predictive_football import (
    predictive_scoreboard, predictive_funnel, predictive_league_summary,
    predictive_market_summary, predictive_market_funnel,
    predictive_bootstrap_status,
)
from predictive_football_pred2 import (
    predictive2_scoreboard, predictive2_funnel, predictive2_league_summary,
    predictive2_market_summary, predictive2_market_funnel,
)
from predictive_football_pred3 import (
    predictive3_scoreboard, predictive3_funnel, predictive3_league_summary,
    predictive3_market_summary, predictive3_market_funnel, predictive3_bootstrap_status,
)
from predictive_football_pred4 import (
    predictive4_scoreboard, predictive4_funnel, predictive4_league_summary,
    predictive4_market_summary, predictive4_market_funnel, predictive4_bootstrap_status,
)
from predictive_historical import historical_validation_summary
from meta_edge import meta_edge_scoreboard, meta_edge_segments
from meta_edge_model import meta_model_status
from proxy_xg import proxy_xg_status
from outcome_edge import outcome_edge_report


EXPORT_TABLES: Tuple[str, ...] = (
    "events",
    "odds_snapshots",
    "consensus_snapshots",
    "signals",
    "canonical_bets",
    "execution_shadow_bets",
    "candidate_evaluations",
    "execution_evaluations",
    "signal_price_observations",
    "execution_price_observations",
    "multiple_shadow_bets",
    "multiple_shadow_legs",
    "multiple_shadow_state",
    "manual_system_shadow_bets",
    "manual_system_shadow_legs",
    "manual_system_shadow_lines",
    "cohort_system_shadow_state",
    "cohort_system_shadow_bets",
    "cohort_system_shadow_legs",
    "cohort_system_shadow_lines",
    "event_results",
    "research_reports",
    "collector_runs",
    "quota_state",
    "tennis_tournament_state",
    "tennis_events",
    "tennis_odds_snapshots",
    "tennis_consensus_snapshots",
    "tennis_execution_evaluations",
    "tennis_execution_bets",
    "tennis_price_observations",
    "tennis_results",
    "multisport_league_state",
    "multisport_events",
    "multisport_odds_snapshots",
    "multisport_consensus_snapshots",
    "multisport_execution_evaluations",
    "multisport_execution_bets",
    "multisport_price_observations",
    "multisport_results",
    "multisport_lines_state",
    "multisport_line_odds_snapshots",
    "multisport_line_consensus_snapshots",
    "multisport_line_evaluations",
    "multisport_line_bets",
    "multisport_line_price_observations",
    "football_predictive_training_matches",
    "football_predictive_source_state",
    "football_predictive_predictions",
    "football_predictive_evaluations",
    "football_predictive_bets",
    "football_predictive_price_observations",
    "football_predictive_market_predictions",
    "football_predictive_market_evaluations",
    "football_predictive_market_bets",
    "football_predictive_market_price_observations",
    "football_predictive2_predictions",
    "football_predictive2_evaluations",
    "football_predictive2_bets",
    "football_predictive2_price_observations",
    "football_predictive2_market_predictions",
    "football_predictive2_market_evaluations",
    "football_predictive2_market_bets",
    "football_predictive2_market_price_observations",
    "football_predictive3_training_matches",
    "football_predictive3_source_state",
    "football_predictive3_statsbomb_manifest",
    "football_predictive3_predictions",
    "football_predictive3_evaluations",
    "football_predictive3_bets",
    "football_predictive3_price_observations",
    "football_predictive3_market_predictions",
    "football_predictive3_market_evaluations",
    "football_predictive3_market_bets",
    "football_predictive3_market_price_observations",
    "football_predictive4_predictions",
    "football_predictive4_evaluations",
    "football_predictive4_bets",
    "football_predictive4_price_observations",
    "football_predictive4_market_predictions",
    "football_predictive4_market_evaluations",
    "football_predictive4_market_bets",
    "football_predictive4_market_price_observations",
    "football_pxg_statsbomb_manifest",
    "football_pxg_statsbomb_samples",
    "football_pxg_models",
    "football_pxg_api_discovery_days",
    "football_pxg_api_manifest",
    "football_pxg_current_matches",
    "football_pxg_api_usage",
    "outcome_edge_watch_cohorts",
    "meta_edge_samples",
    "meta_edge_model_runs",
    "meta_edge_model_scores",
    "football_predictive_historical_validations",
    "football_predictive_historical_odds",
    "football_predictive_historical_bets",
)


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(x) for x in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def _rows_to_csv(rows: Sequence[Mapping[str, Any]], *, secrets: Sequence[str] = ()) -> str:
    if not rows:
        return ""
    columns: List[str] = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                columns.append(str(key))

    buf = StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        clean: Dict[str, Any] = {}
        for key in columns:
            value = row.get(key)
            if isinstance(value, (dict, list, tuple)):
                value = json.dumps(_json_safe(value), separators=(",", ":"))
            if isinstance(value, str):
                value = sanitize_sensitive_text(value, secrets=secrets)
            clean[key] = value
        writer.writerow(clean)
    return buf.getvalue()


def _table_exists(db, table: str) -> bool:
    try:
        db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")
        return True
    except Exception:
        return False


def _safe_settings(settings) -> Dict[str, Any]:
    # Deliberately excludes ODDS_API_KEY, DATABASE_URL and ADMIN_SECRET.
    return {
        "sport_keys": list(settings.sport_keys),
        "odds_region": settings.odds_region,
        "odds_markets": list(settings.odds_markets),
        "dnb_collection_enabled": bool(getattr(settings, "enable_dnb_market", False)),
        "collection_model": "broad_discovery_then_narrow_execution_convergence",
        "execution_shadow_enabled": bool(settings.execution_shadow_enabled),
        "execution_bookmaker_keys": list(settings.execution_bookmaker_keys),
        "multiples_api_bookmaker_keys": list(getattr(settings, "multiples_api_bookmaker_keys", ()) or ()),
        "manual_systems_enabled": bool(getattr(settings, "manual_systems_enabled", True)),
        "manual_systems_placeable_bookmaker_keys": list(getattr(settings, "manual_systems_placeable_bookmaker_keys", ()) or ()),
        "manual_systems_comparison_bookmaker_keys": list(getattr(settings, "manual_systems_comparison_bookmaker_keys", ()) or ()),
        "manual_systems_types": list(getattr(settings, "manual_systems_types", ()) or ()),
        "manual_systems_source_cohorts": list(getattr(settings, "manual_systems_source_cohorts", ()) or ()),
        "manual_systems_daily_credit_budget": int(getattr(settings, "manual_systems_daily_credit_budget", 0)),
        "min_consensus_books": int(settings.min_consensus_books),
        "min_edge_pct": float(settings.min_edge_pct),
        "min_cross_market_edge_pct": float(settings.min_cross_market_edge_pct),
        "min_slow_book_gap_pct": float(settings.min_slow_book_gap_pct),
        "daily_paid_credit_budget": int(settings.daily_paid_credit_budget),
        "quota_reserve_credits": int(settings.quota_reserve_credits),
        "breadth_polls_per_day": int(settings.breadth_polls_per_day),
        "max_events_per_odds_cycle": int(settings.max_events_per_odds_cycle),
        "enable_live_betting": bool(settings.enable_live_betting),
        "betfair_commission_pct": float(getattr(settings, "betfair_commission_pct", 5.0)),
        "matchbook_commission_pct": float(getattr(settings, "matchbook_commission_pct", 2.0)),
        "smarkets_commission_pct": float(getattr(settings, "smarkets_commission_pct", 2.0)),
        "default_execution_commission_pct": float(getattr(settings, "default_execution_commission_pct", 5.0)),
        "tennis_shadow_enabled": bool(getattr(settings, "tennis_shadow_enabled", False)),
        "tennis_market": str(getattr(settings, "tennis_market", "h2h")),
        "tennis_min_consensus_books": int(getattr(settings, "tennis_min_consensus_books", 5)),
        "tennis_min_edge_pct": float(getattr(settings, "tennis_min_edge_pct", 3.0)),
        "tennis_daily_paid_credit_budget": int(getattr(settings, "tennis_daily_paid_credit_budget", 0)),
        "tennis_quota_reserve_credits": int(getattr(settings, "tennis_quota_reserve_credits", 0)),
        "tennis_max_consensus_age_minutes": int(getattr(settings, "tennis_max_consensus_age_minutes", 240)),
        "multisport_shadow_enabled": bool(getattr(settings, "multisport_shadow_enabled", False)),
        "multisport_sport_keys": list(getattr(settings, "multisport_sport_keys", ()) or ()),
        "multisport_market": str(getattr(settings, "multisport_market", "h2h")),
        "multisport_odds_region": str(getattr(settings, "multisport_odds_region", "uk")),
        "multisport_hockey_reference_region": str(
            getattr(settings, "multisport_hockey_reference_region", "us")
        ),
        "multisport_min_consensus_books": int(getattr(settings, "multisport_min_consensus_books", 5)),
        "multisport_min_edge_pct": float(getattr(settings, "multisport_min_edge_pct", 3.0)),
        "multisport_daily_paid_credit_budget": int(getattr(settings, "multisport_daily_paid_credit_budget", 0)),
        "multisport_quota_reserve_credits": int(getattr(settings, "multisport_quota_reserve_credits", 0)),
        "multisport_max_consensus_age_minutes": int(getattr(settings, "multisport_max_consensus_age_minutes", 240)),
        "multisport_lines_enabled": bool(getattr(settings, "multisport_lines_enabled", False)),
        "multisport_lines_markets": list(getattr(settings, "multisport_lines_markets", ()) or ()),
        "multisport_lines_min_consensus_books": int(getattr(settings, "multisport_lines_min_consensus_books", 3)),
        "multisport_lines_min_edge_pct": float(getattr(settings, "multisport_lines_min_edge_pct", 3.0)),
        "multisport_lines_max_consensus_age_minutes": int(getattr(settings, "multisport_lines_max_consensus_age_minutes", 240)),
        "multisport_lines_us_reference_region": str(getattr(settings, "multisport_lines_us_reference_region", "us")),
        "multisport_lines_au_reference_region": str(getattr(settings, "multisport_lines_au_reference_region", "au")),
        "predictive_football_enabled": bool(getattr(settings, "predictive_football_enabled", False)),
        "predictive_football_bootstrap_enabled": bool(getattr(settings, "predictive_football_bootstrap_enabled", False)),
        "predictive_football_forecast_hours_before": float(getattr(settings, "predictive_football_forecast_hours_before", 24)),
        "predictive_football_min_league_matches": int(getattr(settings, "predictive_football_min_league_matches", 40)),
        "predictive_football_min_team_matches": float(getattr(settings, "predictive_football_min_team_matches", 4)),
        "predictive_football_prior_matches": float(getattr(settings, "predictive_football_prior_matches", 5)),
        "predictive_football_half_life_days": float(getattr(settings, "predictive_football_half_life_days", 180)),
        "predictive_football_lookback_days": int(getattr(settings, "predictive_football_lookback_days", 550)),
        "predictive_football_min_edge_pct": float(getattr(settings, "predictive_football_min_edge_pct", 3.0)),
        "predictive_football_strong_edge_pct": float(getattr(settings, "predictive_football_strong_edge_pct", 5.0)),
        "predictive_football_markets": list(getattr(settings, "predictive_football_markets", ("h2h","btts","totals"))),
        "predictive_football_total_points": list(getattr(settings, "predictive_football_total_points", (1.5,2.5,3.5))),
        "predictive_football_pred2_enabled": bool(getattr(settings, "predictive_football_pred2_enabled", False)),
        "predictive_football_pred2_rho_min": float(getattr(settings, "predictive_football_pred2_rho_min", -0.20)),
        "predictive_football_pred2_rho_max": float(getattr(settings, "predictive_football_pred2_rho_max", 0.20)),
        "predictive_football_pred2_rho_step": float(getattr(settings, "predictive_football_pred2_rho_step", 0.01)),
        "predictive_football_pred3_enabled": bool(getattr(settings, "predictive_football_pred3_enabled", True)),
        "predictive_football_pred3_statsbomb_enabled": bool(getattr(settings, "predictive_football_pred3_statsbomb_enabled", True)),
        "predictive_football_pred3_statsbomb_matches_per_cycle": int(getattr(settings, "predictive_football_pred3_statsbomb_matches_per_cycle", 12)),
        "predictive_football_pred3_statsbomb_seasons_per_competition": int(getattr(settings, "predictive_football_pred3_statsbomb_seasons_per_competition", 4)),
        "predictive_football_pred3_min_league_matches": int(getattr(settings, "predictive_football_pred3_min_league_matches", 20)),
        "predictive_football_pred3_min_team_matches": float(getattr(settings, "predictive_football_pred3_min_team_matches", 2.5)),
        "predictive_football_pred3_half_life_days": float(getattr(settings, "predictive_football_pred3_half_life_days", 365)),
        "predictive_football_pred3_lookback_days": int(getattr(settings, "predictive_football_pred3_lookback_days", 1200)),
        "predictive_football_pred3_max_data_age_days": float(getattr(settings, "predictive_football_pred3_max_data_age_days", 1200)),
        "proxy_xg_enabled": bool(getattr(settings, "proxy_xg_enabled", True)),
        "proxy_xg_api_football_configured": bool(str(getattr(settings, "proxy_xg_api_football_key", "") or "").strip()),
        "proxy_xg_api_daily_call_budget": int(getattr(settings, "proxy_xg_api_daily_call_budget", 90)),
        "proxy_xg_api_backfill_days": int(getattr(settings, "proxy_xg_api_backfill_days", 45)),
        "proxy_xg_api_matches_per_cycle": int(getattr(settings, "proxy_xg_api_matches_per_cycle", 4)),
        "proxy_xg_min_training_samples": int(getattr(settings, "proxy_xg_min_training_samples", 200)),
        "predictive_football_pred4_enabled": bool(getattr(settings, "predictive_football_pred4_enabled", True)),
        "predictive_football_pred4_min_league_matches": int(getattr(settings, "predictive_football_pred4_min_league_matches", 20)),
        "predictive_football_pred4_min_team_matches": int(getattr(settings, "predictive_football_pred4_min_team_matches", 5)),
        "predictive_football_pred4_min_effective_team_matches": float(getattr(settings, "predictive_football_pred4_min_effective_team_matches", 3.0)),
        "predictive_football_pred4_prior_matches": float(getattr(settings, "predictive_football_pred4_prior_matches", 3.0)),
        "predictive_football_pred4_half_life_days": float(getattr(settings, "predictive_football_pred4_half_life_days", 21)),
        "predictive_football_pred4_lookback_days": int(getattr(settings, "predictive_football_pred4_lookback_days", 60)),
        "predictive_football_pred4_max_data_age_days": float(getattr(settings, "predictive_football_pred4_max_data_age_days", 30)),
        "predictive_football_high_res_price_path_enabled": bool(getattr(settings, "predictive_football_high_res_price_path_enabled", True)),
        "predictive_football_broad_close_enabled": bool(getattr(settings, "predictive_football_broad_close_enabled", True)),
        "predictive_football_broad_close_hours_before": float(getattr(settings, "predictive_football_broad_close_hours_before", 3.0)),
        "predictive_football_historical_enabled": bool(getattr(settings, "predictive_football_historical_enabled", False)),
        "predictive_football_historical_region": str(getattr(settings, "predictive_football_historical_region", "uk")),
        "predictive_football_historical_snapshot_minutes": list(getattr(settings, "predictive_football_historical_snapshot_minutes", (1440,360,60,5))),
        "predictive_football_historical_daily_credit_budget": int(getattr(settings, "predictive_football_historical_daily_credit_budget", 0)),
        "predictive_football_historical_interval_seconds": int(getattr(settings, "predictive_football_historical_interval_seconds", 900)),
        "meta_edge_enabled": bool(getattr(settings, "meta_edge_enabled", True)),
        "meta_edge_min_clean_labels": int(getattr(settings, "meta_edge_min_clean_labels", 200)),
        "meta_edge_model_enabled": bool(getattr(settings, "meta_edge_model_enabled", True)),
    }


def build_research_export(db, settings, version: str) -> tuple[bytes, str]:
    """
    Build a self-contained ZIP for ChatGPT/offline research analysis.

    Secrets are never exported. The ZIP contains derived summaries plus the
    underlying research tables as CSV files.
    """
    generated = datetime.now(timezone.utc)
    stamp = generated.strftime("%Y%m%dT%H%M%SZ")
    filename = f"betting-lab-research-{stamp}.zip"

    row_counts: Dict[str, int] = {}
    table_payloads: Dict[str, str] = {}
    export_secrets = tuple(
        x for x in (
            str(getattr(settings, "odds_api_key", "") or ""),
            str(getattr(settings, "proxy_xg_api_football_key", "") or ""),
        ) if x
    )

    for table in EXPORT_TABLES:
        if not _table_exists(db, table):
            row_counts[table] = 0
            table_payloads[table] = ""
            continue
        rows = db.fetchall(f"SELECT * FROM {table}")
        row_counts[table] = len(rows)
        table_payloads[table] = _rows_to_csv(rows, secrets=export_secrets)

    execution = execution_scoreboard(db)
    canonical = canonical_scoreboard(db)
    funnel = execution_funnel(db)
    intelligence = research_intelligence(db)
    weekly = latest_weekly_reports(db, 104)
    multiples = multiples_scoreboard(db)
    manual_systems = manual_systems_scoreboard(db)
    cohort_systems = cohort_systems_scoreboard(db)
    instrumentation = instrumentation_report(db)
    tennis = tennis_scoreboard(db)
    tennis_research = tennis_segments(db)
    multisport = multisport_scoreboard(db)
    multisport_research = multisport_segments(db)
    multisport_funnel_data = multisport_funnel(db)
    multisport_lines = line_scoreboard(db)
    multisport_lines_research = line_segments(db)
    multisport_lines_funnel = line_funnel(db)
    predictive = predictive_scoreboard(db)
    predictive_funnel_data = predictive_funnel(db)
    predictive_market_summary_data = predictive_market_summary(db)
    predictive_market_funnel_data = predictive_market_funnel(db)
    predictive_bootstrap = predictive_bootstrap_status(db)
    predictive_leagues = predictive_league_summary(db)
    predictive2 = predictive2_scoreboard(db)
    predictive2_funnel_data = predictive2_funnel(db)
    predictive2_market_summary_data = predictive2_market_summary(db)
    predictive2_market_funnel_data = predictive2_market_funnel(db)
    predictive2_leagues = predictive2_league_summary(db)
    predictive3 = predictive3_scoreboard(db)
    predictive3_funnel_data = predictive3_funnel(db)
    predictive3_market_summary_data = predictive3_market_summary(db)
    predictive3_market_funnel_data = predictive3_market_funnel(db)
    predictive3_bootstrap = predictive3_bootstrap_status(db)
    predictive3_leagues = predictive3_league_summary(db)
    predictive4 = predictive4_scoreboard(db)
    predictive4_funnel_data = predictive4_funnel(db)
    predictive4_market_summary_data = predictive4_market_summary(db)
    predictive4_market_funnel_data = predictive4_market_funnel(db)
    predictive4_bootstrap = predictive4_bootstrap_status(db)
    predictive4_leagues = predictive4_league_summary(db)
    predictive_historical = historical_validation_summary(db)
    pxg = proxy_xg_status(db, settings)
    outcome_edge = outcome_edge_report(db)
    meta_edge = meta_edge_scoreboard(db, int(getattr(settings, "meta_edge_min_clean_labels", 200)))
    meta_edge_research = meta_edge_segments(db)
    meta_edge_model = meta_model_status(db)

    manifest = {
        "app": "Project Exit Plan — Betting Lab",
        "version": version,
        "generated_at_utc": generated.isoformat(),
        "database_backend": "postgres" if db.is_postgres else "sqlite",
        "contains_secrets": False,
        "row_counts": row_counts,
        "settings": _safe_settings(settings),
        "notes": [
            "Headline/live-candidate analysis should use execution_shadow_bets.",
            "canonical_bets are theoretical all-bookmaker opportunities.",
            "signals are raw strategy/bookmaker detections and can overlap.",
            "odds_snapshots contains the raw bookmaker price observations.",
            "multiple_shadow_bets is a downstream research-only doubles/trebles layer and never changes singles execution.",
            "v0.7.1 gates all NEW multiple formation to an explicit verified accumulator-capable API venue allowlist.",
            "Historical non-API MS1 rows remain in the export but automation_eligible=0 and are excluded from headline multiples metrics.",
            "Multiples Shadow uses only already-stored odds and makes no extra provider calls.",
            "v0.14.0 adds MS2 Manual Systems Shadow: Yankee/Heinz cards at William Hill/Ladbrokes plus synthetic comparison pricing at Betfair Exchange, Matchbook and Smarkets.",
            "MS2 uses targeted quote refreshes only when at least four qualifying source fixtures can contribute to a system card; it never places an order.",
            "Every MS2 card risks exactly 1u total and is compared with the same 1u split equally across the constituent singles.",
            "v0.6.9 instrumentation uses only already-stored odds/results and adds zero provider calls.",
            "v0.6.10 sanitizes stored/exported provider errors and paces breadth across the day.",
            "v0.7.0 Tennis Shadow is isolated from football and uses match-winner h2h only.",
            "Tennis Shadow has a separate daily paid-credit lane and does not alter football thresholds or evidence.",
            "v0.8.0 Multi-Sport Shadow is a separate two-way h2h/moneyline research cohort for configured in-season sports.",
            "Multi-Sport Shadow rejects market waves that are not exactly two-way.",
            "v0.8.1 preserves explicit zero-cost provider responses across football, tennis and MSP1 accounting.",
            "v0.8.1 adds bet-level settlement provenance for AFL/baseball rule-sensitive outcomes and sport-aware hockey reference-region handling.",
            "Multi-Sport Shadow has its own paid-credit lane and does not alter football or Tennis TS1 evidence.",
            "PRED1 predictive football freezes score-only Bayesian Poisson forecasts before consulting any market price.",
            "PRED1 optionally bootstraps free historical scorelines from football-data.co.uk; bookmaker odds from that source are not imported.",
            "v0.11.0 derives BTTS and O/U 1.5/2.5/3.5 from the same frozen score forecast; no extra predictive model is fitted.",
            "v0.12.0 adds PRED2 Dixon-Coles as an isolated paired challenger using the same score-only history, forecast horizon, thresholds and approved-venue odds waves as PRED1.",
            "PRED2 adds no separate paid odds-data feed; shared event/market convergence candidates are deduplicated before provider polling.",
            "v0.13.0 increases PRED1/PRED2 approved-venue price-path density near kickoff and captures broad h2h consensus closes without creating extra signals.",
            "v0.13.0 historical validation is a separate frozen corroboration cohort: model training is cut off at the original forecast freeze time, and historical odds never enter forward PRED1/PRED2 tables.",
            "Historical sampled execution uses fixed 24h/6h/1h/5m checkpoints and must not be interpreted as exact first-acceptable execution.",
            "v0.15.0 META1 freezes entry-time PRED1/PRED2 market/model features and labels them later with A/B-quality CLV; it makes zero provider calls and has no selection authority.",
            "v0.16.0 adds PRED3 as an isolated StatsBomb Open Data xG/event challenger. It uses no bookmaker prices before forecast freeze and skips fixtures without sufficient non-stale StatsBomb history.",
            "v0.17.0 adds PXG1 as a research-only current underlying-performance foundation. It learns a proxy-xG mapping from genuine StatsBomb xG and ordinary match statistics, then can apply it to current API-Football match statistics without changing PRED1/PRED2/PRED3.",
            "v0.17.0 also adds Outcome Edge research: unique settled selections are compared by observed hit rate versus entry-implied probability, with ROI and CLV retained as supporting diagnostics.",
            "v0.18.0 adds META2: one frozen time-validated shadow model after the META1 clean-label gate. It predicts probability of beating close and expected CLV for future PRED1/PRED2 opportunities but has no betting authority.",
            "v0.18.1 lets PXG1 retry stale transient API-Football discovery-day failures after plan upgrades/timeouts, with cooldown and a hard retry cap; no betting authority or thresholds change.",
            "v0.19.0 adds PRED4 as an isolated current PXG1 forecasting challenger. It requires repeated recent team histories, makes zero provider calls itself, freezes before consulting prices, and remains shadow-only.",
            "v0.19.0 freezes PRED1/PRED2 BTTS and 4.00-4.99 / 4.00-7.49 odds-band hypotheses into forward-only watch cohorts so discovery and validation samples stay separate; no selection thresholds are changed.",
            "v0.19.1 adds MS3 Cohort Systems Shadow: forward-only BTTS, 4.00-7.49 and hybrid Yankee/Heinz cards with 1u system versus 1u singles controls.",
            "v0.19.2 hardens MS3 formation: a candidate card arms first, its exact event/market legs receive priority approved-venue convergence refreshes, and the card only confirms if every post-arm quote remains eligible within the 45-minute confirmation window. This can add a small number of targeted Odds API calls but still has no execution authority.",
            "MS3 synthetic system prices are products of constituent singles prices and must not be interpreted as native bookmaker multiple quotes.",
            "PRED3 StatsBomb ingestion uses free GitHub-hosted open-data JSON and consumes zero Odds API credits; the normal stored approved-venue waves are reused after forecast freeze.",
            "META1 remains the immutable entry-feature/label dataset. META2 is not created before the configured minimum clean A/B label count and does not self-retrain after its first freeze.",
            "Experiment fingerprints contain only a secret-free configuration hash.",
            "No API keys, database credentials or admin secrets are included.",
        ],
    }

    summary = {
        "generated_at_utc": generated.isoformat(),
        "execution_scoreboard": execution,
        "canonical_scoreboard": canonical,
        "execution_funnel": funnel,
        "research_intelligence": intelligence,
        "weekly_reports": weekly,
        "multiples_shadow": multiples,
        "manual_systems_shadow": manual_systems,
        "cohort_systems_shadow": cohort_systems,
        "instrumentation": instrumentation,
        "tennis_shadow": tennis,
        "tennis_segments": tennis_research,
        "multisport_shadow": multisport,
        "multisport_segments": multisport_research,
        "multisport_funnel": multisport_funnel_data,
        "multisport_lines_shadow": multisport_lines,
        "multisport_lines_segments": multisport_lines_research,
        "multisport_lines_funnel": multisport_lines_funnel,
        "predictive_football": predictive,
        "predictive_football_funnel": predictive_funnel_data,
        "predictive_football_market_summary": predictive_market_summary_data,
        "predictive_football_market_funnel": predictive_market_funnel_data,
        "predictive_football_bootstrap": predictive_bootstrap,
        "predictive_football_leagues": predictive_leagues,
        "predictive_football_pred2": predictive2,
        "predictive_football_pred2_funnel": predictive2_funnel_data,
        "predictive_football_pred2_market_summary": predictive2_market_summary_data,
        "predictive_football_pred2_market_funnel": predictive2_market_funnel_data,
        "predictive_football_pred2_leagues": predictive2_leagues,
        "predictive_football_pred3": predictive3,
        "predictive_football_pred3_funnel": predictive3_funnel_data,
        "predictive_football_pred3_market_summary": predictive3_market_summary_data,
        "predictive_football_pred3_market_funnel": predictive3_market_funnel_data,
        "predictive_football_pred3_bootstrap": predictive3_bootstrap,
        "predictive_football_pred3_leagues": predictive3_leagues,
        "predictive_football_pred4": predictive4,
        "predictive_football_pred4_funnel": predictive4_funnel_data,
        "predictive_football_pred4_market_summary": predictive4_market_summary_data,
        "predictive_football_pred4_market_funnel": predictive4_market_funnel_data,
        "predictive_football_pred4_bootstrap": predictive4_bootstrap,
        "predictive_football_pred4_leagues": predictive4_leagues,
        "predictive_football_historical_validation": predictive_historical,
        "proxy_xg": pxg,
        "outcome_edge": outcome_edge,
        "meta_edge": meta_edge,
        "meta_edge_segments": meta_edge_research,
        "meta_edge_model": meta_edge_model,
    }

    readme = f"""Project Exit Plan — Betting Lab Research Export
Version: {version}
Generated UTC: {generated.isoformat()}

PURPOSE
-------
Upload this ZIP back into ChatGPT for Betting Lab analysis.

PRIMARY TABLES
--------------
tables/execution_shadow_bets.csv
    Automation-capable theoretical executions. Includes gross P&L, estimated
    commission, net P&L, close observation time, minutes before kickoff and
    CLV quality. Headline CLV should use A/B quality only.

tables/execution_price_observations.csv
    Price path on the chosen approved execution venue.

tables/execution_evaluations.csv
    Accepted/rejected execution-shadow decisions and rejection reasons.

tables/multiple_shadow_bets.csv
    Research-only same-book doubles and trebles constructed downstream from
    current executable-shadow singles. Includes combined entry price, fair
    probability, P&L and combined CLV.

tables/multiple_shadow_legs.csv
    Frozen constituent legs and same-book entry/closing prices for each
    multiple. Legs always come from different fixtures.

tables/manual_system_shadow_bets.csv
    MS2 Yankee/Heinz research cards. William Hill/Ladbrokes rows are tagged
    MANUAL_PLACEABLE; Betfair Exchange/Matchbook/Smarkets rows are synthetic
    price-comparison controls. Each card uses 1u total stake.

tables/manual_system_shadow_legs.csv
    Frozen constituent legs, source engine (CONSENSUS/PRED1/PRED2), venue price,
    close and CLV for each manual-system card.

tables/manual_system_shadow_lines.csv
    Every Yankee/Heinz component line with equal line stake, expected value and
    realised line P&L.

tables/event_results.csv
    Final football scores used for automatic settlement.

tables/football_predictive_predictions.csv
    PRED1 frozen score-only 1X2 forecasts, team mappings, expected goals,
    probabilities, Brier/log-loss results and closing-market comparison.

tables/football_predictive_bets.csv
    First acceptable executable prices found only after a PRED1 forecast was
    frozen, with A/B/C/STALE CLV and commission-aware P&L.

tables/football_predictive_training_matches.csv
    Scoreline-only model training history from Betting Lab results and the
    optional football-data.co.uk bootstrap. No bookmaker odds are stored here.

tables/football_predictive_historical_validations.csv
    Frozen retrospective PRED1/PRED2 fixture forecasts reconstructed strictly
    from score history available at the original forecast freeze time. Includes
    model Brier/log-loss and broad closing-market Brier. This cohort never
    modifies forward PRED1/PRED2 evidence.

tables/football_predictive_historical_odds.csv
    Historical h2h bookmaker prices sampled at configured pre-kickoff
    checkpoints (default 24h, 6h, 1h and 5m).

tables/meta_edge_samples.csv
    META1 entry-time feature/label dataset for learning which PRED1/PRED2
    opportunities survive to A/B-quality close. Research-only; no execution
    authority and no provider calls.

tables/meta_edge_model_runs.csv
    META2 frozen shadow-model metadata, time-ordered holdout metrics and the
    final frozen model parameters. It is created once after the clean-label
    threshold and does not self-retrain.

tables/meta_edge_model_scores.csv
    META2 holdout and forward annotations: probability of beating close,
    expected CLV and trust band. These scores have no betting authority.

tables/football_predictive_historical_bets.csv
    Sampled-checkpoint execution corroboration for PRED1/PRED2. Includes
    executable approved-venue price, model edge, close/CLV and net P&L. It is
    intentionally not described as exact first-acceptable historical execution.

tables/football_predictive_market_predictions.csv
    Frozen BTTS and O/U 1.5/2.5/3.5 probabilities derived from the exact same
    score forecast used by PRED1 1X2.

tables/football_predictive_market_bets.csv
    First acceptable approved-venue BTTS/totals model-value shadows with
    exact line matching, CLV and commission-aware settlement.

tables/football_predictive2_predictions.csv
    PRED2 Dixon-Coles challenger forecasts. Uses the same score-only history,
    forecast horizon and base expected-goal strengths as PRED1, then fits a
    league-history low-score dependency rho before deriving probabilities.

tables/football_predictive2_bets.csv
    PRED2 1X2 executable shadows, isolated from PRED1 for paired forward CLV.

tables/football_predictive2_market_predictions.csv
    PRED2 BTTS/totals probabilities from the Dixon-Coles corrected score grid.

tables/football_predictive2_market_bets.csv
    PRED2 BTTS/totals executable shadows with the same 3%/5% gates as PRED1.

tables/tennis_execution_bets.csv
    TS1 executable tennis match-winner shadows, with first acceptable approved
    price, A/B/C/STALE closing quality, CLV and commission-aware P&L.

tables/tennis_consensus_snapshots.csv
    Two-way de-vigged fixed-bookmaker fair probabilities for tennis.

tables/tennis_odds_snapshots.csv
    Raw tennis breadth and approved-venue convergence prices.

tables/tennis_results.csv
    Provider-completed tennis winners used for research settlement.

tables/multisport_execution_bets.csv
    MSP1 executable two-way h2h/moneyline shadows across the configured
    in-season sports, including A/B CLV and commission-aware P&L.

tables/multisport_consensus_snapshots.csv
    Two-way de-vigged fixed-bookmaker fair probabilities for MSP1.

tables/multisport_odds_snapshots.csv
    Raw Multi-Sport breadth and approved-venue convergence quotes.

tables/multisport_results.csv
    Provider-completed final scores used for MSP1 settlement. Equal final
    scores are retained as PUSH research outcomes.

tables/canonical_bets.csv
    Theoretical opportunities before automation-capable venue filtering.

tables/signals.csv
    Raw strategy/bookmaker detections. These can overlap and should not be
    treated as independent bets.

tables/odds_snapshots.csv
    Raw bookmaker market snapshots used by the research engines.

tables/candidate_evaluations.csv
    Full candidate audit trail including rejected strategy candidates.

SUMMARY FILES
-------------
manifest.json
    Version, safe configuration and table row counts.

analysis_summary.json
    Execution/canonical scoreboards, funnel, Research Intelligence and current
    instrumentation summaries including calibration, overlap and data quality.

SECURITY
--------
This export deliberately contains no ODDS_API_KEY, DATABASE_URL or ADMIN_SECRET.
"""

    payload = BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", readme)
        z.writestr("manifest.json", json.dumps(_json_safe(manifest), indent=2))
        z.writestr("analysis_summary.json", json.dumps(_json_safe(summary), indent=2))
        for table, csv_text in table_payloads.items():
            z.writestr(f"tables/{table}.csv", csv_text)

    return payload.getvalue(), filename
