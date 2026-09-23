from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Tuple


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _csv(name: str, default: str) -> Tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def _float_csv(name: str, default: str) -> Tuple[float, ...]:
    values = []
    for raw in os.getenv(name, default).split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            values.append(float(raw))
        except ValueError:
            continue
    return tuple(values)


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "development")
    database_url: str = os.getenv("DATABASE_URL", "")
    db_path: str = os.getenv("DB_PATH", "./betting_lab.sqlite")
    admin_secret: str = os.getenv("ADMIN_SECRET", "change-me")
    odds_api_key: str = os.getenv("ODDS_API_KEY", "")

    sport_keys: Tuple[str, ...] = _csv(
        "SPORT_KEYS", "soccer_epl,soccer_efl_champ"
    )
    odds_region: str = os.getenv("ODDS_REGION", "uk")
    configured_odds_markets: Tuple[str, ...] = _csv(
        "ODDS_MARKETS", "h2h,totals,btts"
    )
    enable_dnb_market: bool = _bool("ENABLE_DNB_MARKET", False)
    execution_bookmaker_keys: Tuple[str, ...] = _csv(
        "EXECUTION_BOOKMAKER_KEYS", "betfair_ex_uk,matchbook,smarkets"
    )
    execution_shadow_enabled: bool = _bool("EXECUTION_SHADOW_ENABLED", True)

    # v0.7.1 — Multiples API gate.
    #
    # This allowlist is intentionally EMPTY by default. A venue belongs here
    # only after we have verified that its official API can actually submit
    # the accumulator/multiple itself. A singles API is not enough.
    multiples_api_bookmaker_keys: Tuple[str, ...] = _csv(
        "MULTIPLES_API_BOOKMAKER_KEYS", ""
    )

    # v0.14.0 — MS2 Manual Systems Shadow. Research-only Yankee/Heinz
    # cards. William Hill/Ladbrokes are manual-placeable venues; the existing
    # exchange venues remain synthetic price-comparison controls only.
    manual_systems_enabled: bool = _bool("MANUAL_SYSTEMS_ENABLED", True)
    manual_systems_placeable_bookmaker_keys: Tuple[str, ...] = _csv(
        "MANUAL_SYSTEMS_PLACEABLE_BOOKMAKER_KEYS", "williamhill,ladbrokes_uk"
    )
    manual_systems_comparison_bookmaker_keys: Tuple[str, ...] = _csv(
        "MANUAL_SYSTEMS_COMPARISON_BOOKMAKER_KEYS", "betfair_ex_uk,matchbook,smarkets"
    )
    manual_systems_types: Tuple[str, ...] = _csv(
        "MANUAL_SYSTEMS_TYPES", "YANKEE,HEINZ"
    )
    manual_systems_source_cohorts: Tuple[str, ...] = _csv(
        "MANUAL_SYSTEMS_SOURCE_COHORTS", "MIXED_BEST,CONSENSUS,PRED1,PRED2"
    )
    manual_systems_quote_freshness_minutes: float = float(
        os.getenv("MANUAL_SYSTEMS_QUOTE_FRESHNESS_MINUTES", "45")
    )
    manual_systems_max_quote_spread_minutes: float = float(
        os.getenv("MANUAL_SYSTEMS_MAX_QUOTE_SPREAD_MINUTES", "15")
    )
    manual_systems_formation_horizon_hours: float = float(
        os.getenv("MANUAL_SYSTEMS_FORMATION_HORIZON_HOURS", "30")
    )
    manual_systems_refresh_horizon_hours: float = float(
        os.getenv("MANUAL_SYSTEMS_REFRESH_HORIZON_HOURS", "12")
    )
    manual_systems_refresh_interval_minutes: float = float(
        os.getenv("MANUAL_SYSTEMS_REFRESH_INTERVAL_MINUTES", "30")
    )
    manual_systems_max_events_per_refresh: int = int(
        os.getenv("MANUAL_SYSTEMS_MAX_EVENTS_PER_REFRESH", "6")
    )
    manual_systems_daily_credit_budget: int = int(
        os.getenv("MANUAL_SYSTEMS_DAILY_CREDIT_BUDGET", "300")
    )

    enable_live_betting: bool = _bool("ENABLE_LIVE_BETTING", False)
    min_consensus_books: int = int(os.getenv("MIN_CONSENSUS_BOOKS", "3"))
    min_edge_pct: float = float(os.getenv("MIN_EDGE_PCT", "3.0"))
    min_cross_market_edge_pct: float = float(
        os.getenv("MIN_CROSS_MARKET_EDGE_PCT", "4.0")
    )
    min_slow_book_gap_pct: float = float(
        os.getenv("MIN_SLOW_BOOK_GAP_PCT", "4.0")
    )

    quota_reserve_credits: int = int(os.getenv("QUOTA_RESERVE_CREDITS", "50"))
    daily_paid_credit_budget: int = int(
        os.getenv("DAILY_PAID_CREDIT_BUDGET", "1300")
    )
    max_events_per_odds_cycle: int = int(
        os.getenv("MAX_EVENTS_PER_ODDS_CYCLE", "3")
    )
    breadth_polls_per_day: int = int(
        os.getenv("BREADTH_POLLS_PER_DAY", "2")
    )

    run_worker: bool = _bool("RUN_WORKER", True)
    worker_tick_seconds: int = int(os.getenv("WORKER_TICK_SECONDS", "300"))
    discovery_interval_seconds: int = int(
        os.getenv("DISCOVERY_INTERVAL_SECONDS", "1800")
    )
    quota_refresh_interval_seconds: int = int(
        os.getenv("QUOTA_REFRESH_INTERVAL_SECONDS", "1800")
    )
    # Research-only exchange commission assumptions used to estimate
    # execution-shadow net P&L. They do not alter signal/execution rules.
    betfair_commission_pct: float = float(
        os.getenv("BETFAIR_COMMISSION_PCT", "5.0")
    )
    matchbook_commission_pct: float = float(
        os.getenv("MATCHBOOK_COMMISSION_PCT", "2.0")
    )
    smarkets_commission_pct: float = float(
        os.getenv("SMARKETS_COMMISSION_PCT", "2.0")
    )
    default_execution_commission_pct: float = float(
        os.getenv("DEFAULT_EXECUTION_COMMISSION_PCT", "5.0")
    )

    enable_score_collection: bool = _bool("ENABLE_SCORE_COLLECTION", True)
    result_min_minutes_after_kickoff: int = int(
        os.getenv("RESULT_MIN_MINUTES_AFTER_KICKOFF", "135")
    )
    result_poll_min_interval_seconds: int = int(
        os.getenv("RESULT_POLL_MIN_INTERVAL_SECONDS", "21600")
    )

    # v0.7.0 — Tennis Shadow (TS1). This is an isolated research lane.
    tennis_shadow_enabled: bool = _bool("TENNIS_SHADOW_ENABLED", True)
    tennis_market: str = os.getenv("TENNIS_MARKET", "h2h")
    tennis_min_consensus_books: int = int(
        os.getenv("TENNIS_MIN_CONSENSUS_BOOKS", "5")
    )
    tennis_min_edge_pct: float = float(
        os.getenv("TENNIS_MIN_EDGE_PCT", "3.0")
    )
    tennis_daily_paid_credit_budget: int = int(
        os.getenv("TENNIS_DAILY_PAID_CREDIT_BUDGET", "500")
    )
    tennis_quota_reserve_credits: int = int(
        os.getenv("TENNIS_QUOTA_RESERVE_CREDITS", "1000")
    )
    tennis_max_consensus_age_minutes: int = int(
        os.getenv("TENNIS_MAX_CONSENSUS_AGE_MINUTES", "240")
    )
    tennis_discovery_interval_seconds: int = int(
        os.getenv("TENNIS_DISCOVERY_INTERVAL_SECONDS", "21600")
    )
    tennis_result_min_minutes_after_start: int = int(
        os.getenv("TENNIS_RESULT_MIN_MINUTES_AFTER_START", "90")
    )
    tennis_result_poll_interval_seconds: int = int(
        os.getenv("TENNIS_RESULT_POLL_INTERVAL_SECONDS", "3600")
    )

    # v0.8.0 — Multi-Sport Shadow (MSP1).
    multisport_shadow_enabled: bool = _bool("MULTISPORT_SHADOW_ENABLED", True)
    multisport_sport_keys: Tuple[str, ...] = _csv(
        "MULTISPORT_SPORT_KEYS",
        (
            "baseball_mlb,"
            "americanfootball_nfl,"
            "americanfootball_ncaaf,"
            "basketball_wnba,"
            "aussierules_afl,"
            "rugbyleague_nrl,"
            "basketball_nba,"
            "basketball_euroleague,"
            "basketball_ncaab,"
            "icehockey_nhl,"
            "icehockey_sweden_hockey_league,"
            "icehockey_sweden_allsvenskan"
        ),
    )
    multisport_market: str = os.getenv("MULTISPORT_MARKET", "h2h")
    multisport_odds_region: str = os.getenv("MULTISPORT_ODDS_REGION", "uk")
    multisport_hockey_reference_region: str = os.getenv(
        "MULTISPORT_HOCKEY_REFERENCE_REGION", "us"
    )
    multisport_min_consensus_books: int = int(
        os.getenv("MULTISPORT_MIN_CONSENSUS_BOOKS", "5")
    )
    multisport_min_edge_pct: float = float(
        os.getenv("MULTISPORT_MIN_EDGE_PCT", "3.0")
    )
    multisport_daily_paid_credit_budget: int = int(
        os.getenv("MULTISPORT_DAILY_PAID_CREDIT_BUDGET", "1500")
    )
    multisport_quota_reserve_credits: int = int(
        os.getenv("MULTISPORT_QUOTA_RESERVE_CREDITS", "1000")
    )
    multisport_max_consensus_age_minutes: int = int(
        os.getenv("MULTISPORT_MAX_CONSENSUS_AGE_MINUTES", "240")
    )
    multisport_discovery_interval_seconds: int = int(
        os.getenv("MULTISPORT_DISCOVERY_INTERVAL_SECONDS", "21600")
    )
    multisport_result_poll_interval_seconds: int = int(
        os.getenv("MULTISPORT_RESULT_POLL_INTERVAL_SECONDS", "3600")
    )

    # v0.9.0 — Multi-Sport Lines Shadow (MSP2).
    multisport_lines_enabled: bool = _bool("MULTISPORT_LINES_ENABLED", True)
    multisport_lines_markets: Tuple[str, ...] = _csv(
        "MULTISPORT_LINES_MARKETS", "spreads,totals"
    )
    multisport_lines_min_consensus_books: int = int(
        os.getenv("MULTISPORT_LINES_MIN_CONSENSUS_BOOKS", "3")
    )
    multisport_lines_min_edge_pct: float = float(
        os.getenv("MULTISPORT_LINES_MIN_EDGE_PCT", "3.0")
    )
    multisport_lines_max_consensus_age_minutes: int = int(
        os.getenv("MULTISPORT_LINES_MAX_CONSENSUS_AGE_MINUTES", "240")
    )
    multisport_lines_us_reference_region: str = os.getenv(
        "MULTISPORT_LINES_US_REFERENCE_REGION", "us"
    )
    multisport_lines_au_reference_region: str = os.getenv(
        "MULTISPORT_LINES_AU_REFERENCE_REGION", "au"
    )
    multisport_lines_discovery_interval_seconds: int = int(
        os.getenv("MULTISPORT_LINES_DISCOVERY_INTERVAL_SECONDS", "21600")
    )
    multisport_lines_result_poll_interval_seconds: int = int(
        os.getenv("MULTISPORT_LINES_RESULT_POLL_INTERVAL_SECONDS", "3600")
    )

    # v0.10.0 — Predictive Football Shadow (PRED1).
    # The model itself is market-independent: scorelines only. Market/exchange
    # prices are consulted only after a prediction has been frozen, to test
    # whether the independent model can find executable value and beat close.
    predictive_football_enabled: bool = _bool(
        "PREDICTIVE_FOOTBALL_ENABLED", True
    )
    predictive_football_bootstrap_enabled: bool = _bool(
        "PREDICTIVE_FOOTBALL_BOOTSTRAP_ENABLED", True
    )
    predictive_football_bootstrap_refresh_seconds: int = int(
        os.getenv("PREDICTIVE_FOOTBALL_BOOTSTRAP_REFRESH_SECONDS", "86400")
    )
    predictive_football_bootstrap_sources_per_cycle: int = int(
        os.getenv("PREDICTIVE_FOOTBALL_BOOTSTRAP_SOURCES_PER_CYCLE", "4")
    )
    predictive_football_forecast_hours_before: float = float(
        os.getenv("PREDICTIVE_FOOTBALL_FORECAST_HOURS_BEFORE", "24")
    )
    predictive_football_min_league_matches: int = int(
        os.getenv("PREDICTIVE_FOOTBALL_MIN_LEAGUE_MATCHES", "40")
    )
    predictive_football_min_team_matches: float = float(
        os.getenv("PREDICTIVE_FOOTBALL_MIN_TEAM_MATCHES", "4")
    )
    predictive_football_prior_matches: float = float(
        os.getenv("PREDICTIVE_FOOTBALL_PRIOR_MATCHES", "5")
    )
    predictive_football_half_life_days: float = float(
        os.getenv("PREDICTIVE_FOOTBALL_HALF_LIFE_DAYS", "180")
    )
    predictive_football_lookback_days: int = int(
        os.getenv("PREDICTIVE_FOOTBALL_LOOKBACK_DAYS", "550")
    )
    predictive_football_min_edge_pct: float = float(
        os.getenv("PREDICTIVE_FOOTBALL_MIN_EDGE_PCT", "3.0")
    )
    predictive_football_strong_edge_pct: float = float(
        os.getenv("PREDICTIVE_FOOTBALL_STRONG_EDGE_PCT", "5.0")
    )
    # v0.11.0 — derive additional markets from the same already-frozen
    # score model. These settings do not alter the fitted team strengths.
    predictive_football_markets: Tuple[str, ...] = _csv(
        "PREDICTIVE_FOOTBALL_MARKETS", "h2h,btts,totals"
    )
    predictive_football_total_points: Tuple[float, ...] = _float_csv(
        "PREDICTIVE_FOOTBALL_TOTAL_POINTS", "1.5,2.5,3.5"
    )

    # v0.12.0 — PRED2 Dixon-Coles challenger. Uses the exact same score-only
    # information set, forecast horizon, execution venues and thresholds as
    # PRED1; only the low-score dependency model changes. This keeps the
    # forward comparison paired and does not add a new paid data source.
    predictive_football_pred2_enabled: bool = _bool(
        "PREDICTIVE_FOOTBALL_PRED2_ENABLED", True
    )
    predictive_football_pred2_rho_min: float = float(
        os.getenv("PREDICTIVE_FOOTBALL_PRED2_RHO_MIN", "-0.20")
    )
    predictive_football_pred2_rho_max: float = float(
        os.getenv("PREDICTIVE_FOOTBALL_PRED2_RHO_MAX", "0.20")
    )
    predictive_football_pred2_rho_step: float = float(
        os.getenv("PREDICTIVE_FOOTBALL_PRED2_RHO_STEP", "0.01")
    )

    # v0.16.0 — PRED3 StatsBomb xG challenger. Uses free StatsBomb Open Data
    # event xG as a genuinely different information set. Unsupported/stale
    # fixtures are skipped rather than backfilled from bookmaker prices.
    predictive_football_pred3_enabled: bool = _bool(
        "PREDICTIVE_FOOTBALL_PRED3_ENABLED", True
    )
    predictive_football_pred3_statsbomb_enabled: bool = _bool(
        "PREDICTIVE_FOOTBALL_PRED3_STATSBOMB_ENABLED", True
    )
    predictive_football_pred3_statsbomb_base_url: str = os.getenv(
        "PREDICTIVE_FOOTBALL_PRED3_STATSBOMB_BASE_URL",
        "https://raw.githubusercontent.com/hudl/open-data/master/data",
    )
    predictive_football_pred3_statsbomb_refresh_seconds: int = int(
        os.getenv("PREDICTIVE_FOOTBALL_PRED3_STATSBOMB_REFRESH_SECONDS", "86400")
    )
    predictive_football_pred3_statsbomb_matches_per_cycle: int = int(
        os.getenv("PREDICTIVE_FOOTBALL_PRED3_STATSBOMB_MATCHES_PER_CYCLE", "12")
    )
    predictive_football_pred3_statsbomb_seasons_per_competition: int = int(
        os.getenv("PREDICTIVE_FOOTBALL_PRED3_STATSBOMB_SEASONS_PER_COMPETITION", "4")
    )
    predictive_football_pred3_min_league_matches: int = int(
        os.getenv("PREDICTIVE_FOOTBALL_PRED3_MIN_LEAGUE_MATCHES", "20")
    )
    predictive_football_pred3_min_team_matches: float = float(
        os.getenv("PREDICTIVE_FOOTBALL_PRED3_MIN_TEAM_MATCHES", "2.5")
    )
    predictive_football_pred3_prior_matches: float = float(
        os.getenv("PREDICTIVE_FOOTBALL_PRED3_PRIOR_MATCHES", "4")
    )
    predictive_football_pred3_half_life_days: float = float(
        os.getenv("PREDICTIVE_FOOTBALL_PRED3_HALF_LIFE_DAYS", "365")
    )
    predictive_football_pred3_lookback_days: int = int(
        os.getenv("PREDICTIVE_FOOTBALL_PRED3_LOOKBACK_DAYS", "1200")
    )
    predictive_football_pred3_max_data_age_days: float = float(
        os.getenv("PREDICTIVE_FOOTBALL_PRED3_MAX_DATA_AGE_DAYS", "900")
    )

    # v0.17.0 — PXG1 current underlying-performance research. The proxy model
    # is trained only on free StatsBomb samples where true xG is known.
    # API-Football is optional and is disabled automatically until a key is
    # supplied. Existing predictive/execution lanes are not modified.
    proxy_xg_enabled: bool = _bool("PROXY_XG_ENABLED", True)
    proxy_xg_statsbomb_matches_per_cycle: int = int(
        os.getenv("PROXY_XG_STATSBOMB_MATCHES_PER_CYCLE", "12")
    )
    proxy_xg_min_training_samples: int = int(
        os.getenv("PROXY_XG_MIN_TRAINING_SAMPLES", "200")
    )
    proxy_xg_refit_every_samples: int = int(
        os.getenv("PROXY_XG_REFIT_EVERY_SAMPLES", "40")
    )
    proxy_xg_ridge_alpha: float = float(
        os.getenv("PROXY_XG_RIDGE_ALPHA", "3.0")
    )
    proxy_xg_api_football_key: str = os.getenv("API_FOOTBALL_KEY", "")
    proxy_xg_api_football_base_url: str = os.getenv(
        "PROXY_XG_API_FOOTBALL_BASE_URL", "https://v3.football.api-sports.io"
    )
    proxy_xg_api_daily_call_budget: int = int(
        os.getenv("PROXY_XG_API_DAILY_CALL_BUDGET", "90")
    )
    proxy_xg_api_provider_reserve: int = int(
        os.getenv("PROXY_XG_API_PROVIDER_RESERVE", "5")
    )
    proxy_xg_api_backfill_days: int = int(
        os.getenv("PROXY_XG_API_BACKFILL_DAYS", "45")
    )
    proxy_xg_api_matches_per_cycle: int = int(
        os.getenv("PROXY_XG_API_MATCHES_PER_CYCLE", "4")
    )

    # v0.19.0 — PRED4 current PXG challenger. Consumes only completed,
    # already-scored PXG1 current matches and makes zero provider calls.
    # It remains shadow/research-only and uses the same execution thresholds
    # and forecast horizon as PRED1/PRED2/PRED3.
    predictive_football_pred4_enabled: bool = _bool(
        "PREDICTIVE_FOOTBALL_PRED4_ENABLED", True
    )
    predictive_football_pred4_min_league_matches: int = int(
        os.getenv("PREDICTIVE_FOOTBALL_PRED4_MIN_LEAGUE_MATCHES", "20")
    )
    predictive_football_pred4_min_team_matches: int = int(
        os.getenv("PREDICTIVE_FOOTBALL_PRED4_MIN_TEAM_MATCHES", "5")
    )
    predictive_football_pred4_min_effective_team_matches: float = float(
        os.getenv("PREDICTIVE_FOOTBALL_PRED4_MIN_EFFECTIVE_TEAM_MATCHES", "3.0")
    )
    predictive_football_pred4_prior_matches: float = float(
        os.getenv("PREDICTIVE_FOOTBALL_PRED4_PRIOR_MATCHES", "3.0")
    )
    predictive_football_pred4_half_life_days: float = float(
        os.getenv("PREDICTIVE_FOOTBALL_PRED4_HALF_LIFE_DAYS", "21")
    )
    predictive_football_pred4_lookback_days: int = int(
        os.getenv("PREDICTIVE_FOOTBALL_PRED4_LOOKBACK_DAYS", "60")
    )
    predictive_football_pred4_max_data_age_days: float = float(
        os.getenv("PREDICTIVE_FOOTBALL_PRED4_MAX_DATA_AGE_DAYS", "30")
    )

    # v0.13.0 — evidence-quality expansion. These settings do not alter PRED1
    # or PRED2 probabilities, thresholds or execution decisions. They spend
    # additional quota only on denser pre-kickoff measurement and a separate
    # frozen historical validation cohort.
    predictive_football_high_res_price_path_enabled: bool = _bool(
        "PREDICTIVE_FOOTBALL_HIGH_RES_PRICE_PATH_ENABLED", True
    )
    predictive_football_broad_close_enabled: bool = _bool(
        "PREDICTIVE_FOOTBALL_BROAD_CLOSE_ENABLED", True
    )
    predictive_football_broad_close_hours_before: float = float(
        os.getenv("PREDICTIVE_FOOTBALL_BROAD_CLOSE_HOURS_BEFORE", "3")
    )
    predictive_football_historical_enabled: bool = _bool(
        "PREDICTIVE_FOOTBALL_HISTORICAL_ENABLED", True
    )
    predictive_football_historical_region: str = os.getenv(
        "PREDICTIVE_FOOTBALL_HISTORICAL_REGION", "uk"
    )
    predictive_football_historical_snapshot_minutes: Tuple[int, ...] = tuple(
        int(x) for x in _float_csv(
            "PREDICTIVE_FOOTBALL_HISTORICAL_SNAPSHOT_MINUTES",
            "1440,360,60,5",
        ) if int(x) > 0
    )
    predictive_football_historical_daily_credit_budget: int = int(
        os.getenv("PREDICTIVE_FOOTBALL_HISTORICAL_DAILY_CREDIT_BUDGET", "600")
    )
    predictive_football_historical_interval_seconds: int = int(
        os.getenv("PREDICTIVE_FOOTBALL_HISTORICAL_INTERVAL_SECONDS", "900")
    )
    predictive_football_historical_max_candidates_scan: int = int(
        os.getenv("PREDICTIVE_FOOTBALL_HISTORICAL_MAX_CANDIDATES_SCAN", "120")
    )

    # v0.15.0 — META1 CLV Trust Research. This layer makes zero provider calls
    # and has no selection/execution authority. It freezes entry-time features
    # from PRED1/PRED2 and labels them later with A/B-quality CLV. A predictive
    # meta-model is deliberately gated until a meaningful clean sample exists.
    meta_edge_enabled: bool = _bool("META_EDGE_ENABLED", True)
    meta_edge_min_clean_labels: int = int(
        os.getenv("META_EDGE_MIN_CLEAN_LABELS", "200")
    )
    # v0.18.0 — META2 frozen CLV-trust model. Once META1 reaches the clean-label
    # gate, one model is trained/frozen automatically. It only annotates future
    # PRED1/PRED2 opportunities; it cannot create, reject or resize a bet.
    meta_edge_model_enabled: bool = _bool("META_EDGE_MODEL_ENABLED", True)

    @property
    def odds_markets(self) -> Tuple[str, ...]:
        # DNB has so far produced no automation-capable executions. Keep old
        # Railway ODDS_MARKETS values safe by filtering it unless explicitly
        # re-enabled for a future research experiment.
        markets = tuple(self.configured_odds_markets)
        if not self.enable_dnb_market:
            markets = tuple(x for x in markets if x != "draw_no_bet")
        return markets or ("h2h",)

    @property
    def estimated_event_odds_cost(self) -> int:
        # One region; provider charges per unique market actually returned.
        return max(1, len(self.odds_markets))


settings = Settings()
