from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_sensitive_text(value: Any, secrets: Sequence[str] = ()) -> str:
    """Redact provider/API credentials from logs and exported text."""
    text = str(value or "")
    patterns = (
        re.compile(r"([?&](?:apiKey|api_key)=)[^&\s\"'<>]+", re.IGNORECASE),
        re.compile(r"(\bODDS_API_KEY\s*[=:]\s*)[^\s,;\"']+", re.IGNORECASE),
        re.compile(r"(\bAuthorization\s*:\s*Bearer\s+)[^\s,;\"']+", re.IGNORECASE),
        re.compile(r"(\bx-apisports-key\s*:\s*)[^\s,;\"']+", re.IGNORECASE),
    )
    for pattern in patterns:
        text = pattern.sub(lambda m: f"{m.group(1)}[REDACTED]", text)
    for secret in secrets:
        secret = str(secret or "")
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


class Database:
    def __init__(self, database_url: str = "", db_path: str = "./betting_lab.sqlite"):
        self.database_url = database_url or ""
        self.db_path = db_path
        self.is_postgres = self.database_url.startswith(("postgres://", "postgresql://"))

    def _connect(self):
        if self.is_postgres:
            import psycopg2
            import psycopg2.extras
            return psycopg2.connect(self.database_url)
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.is_postgres else sql

    @contextmanager
    def connection(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self.connection() as conn:
            cur = conn.cursor()
            cur.execute(self._sql(sql), tuple(params))

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self.connection() as conn:
            cur = conn.cursor()
            cur.executemany(self._sql(sql), list(rows))

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        with self.connection() as conn:
            if self.is_postgres:
                import psycopg2.extras
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            else:
                cur = conn.cursor()
            cur.execute(self._sql(sql), tuple(params))
            return [dict(row) for row in cur.fetchall()]

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        rows = self.fetchall(sql, params)
        return rows[0] if rows else None

    def _column_names(self, conn, table: str) -> set[str]:
        cur = conn.cursor()
        if self.is_postgres:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema=current_schema() AND table_name=%s
                """,
                (table,),
            )
            return {str(row[0]) for row in cur.fetchall()}
        cur.execute(f"PRAGMA table_info({table})")
        return {str(row[1]) for row in cur.fetchall()}

    def _ensure_column(self, conn, table: str, column: str, definition: str) -> None:
        if column in self._column_names(conn, table):
            return
        cur = conn.cursor()
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def init_schema(self) -> None:
        id_col = "BIGSERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        ddl = [
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                sport_key TEXT NOT NULL,
                league TEXT NOT NULL,
                commence_time TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_odds_poll_at TEXT,
                odds_quarantine_until TEXT,
                odds_failure_count INTEGER NOT NULL DEFAULT 0,
                odds_last_failure_code INTEGER,
                odds_last_failure_at TEXT,
                status TEXT NOT NULL DEFAULT 'UPCOMING'
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS odds_snapshots (
                id {id_col},
                event_id TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL,
                bookmaker_last_update TEXT,
                market_key TEXT NOT NULL,
                outcome_name TEXT NOT NULL,
                outcome_description TEXT,
                point REAL,
                price REAL NOT NULL,
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS consensus_snapshots (
                id {id_col},
                event_id TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                outcome_description TEXT,
                point REAL,
                fair_probability REAL NOT NULL,
                fair_odds REAL NOT NULL,
                num_books INTEGER NOT NULL,
                model TEXT NOT NULL DEFAULT 'bookmaker_consensus'
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS signals (
                id {id_col},
                created_at TEXT NOT NULL,
                event_id TEXT NOT NULL,
                strategy TEXT NOT NULL,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                outcome_description TEXT,
                point REAL,
                bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL,
                offered_odds REAL NOT NULL,
                fair_odds REAL NOT NULL,
                fair_probability REAL NOT NULL,
                edge_pct REAL NOT NULL,
                min_odds REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                closing_odds REAL,
                clv_pct REAL,
                result TEXT,
                pnl_units REAL,
                metadata_json TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS quota_state (
                singleton_id INTEGER PRIMARY KEY,
                credits_remaining INTEGER,
                credits_used INTEGER,
                last_cost INTEGER,
                last_checked_at TEXT,
                paid_polling_paused INTEGER NOT NULL DEFAULT 0,
                pause_reason TEXT
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS collector_runs (
                id {id_col},
                started_at TEXT NOT NULL,
                finished_at TEXT,
                run_type TEXT NOT NULL,
                event_id TEXT,
                sport_key TEXT,
                requested_markets TEXT,
                estimated_cost INTEGER NOT NULL DEFAULT 0,
                actual_cost INTEGER NOT NULL DEFAULT 0,
                ok INTEGER NOT NULL DEFAULT 0,
                detail TEXT
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS candidate_evaluations (
                id {id_col},
                evaluated_at TEXT NOT NULL,
                event_id TEXT NOT NULL,
                strategy TEXT NOT NULL,
                market_key TEXT NOT NULL,
                selection TEXT,
                outcome_description TEXT,
                point REAL,
                bookmaker_key TEXT,
                bookmaker_title TEXT,
                offered_odds REAL,
                fair_odds REAL,
                fair_probability REAL,
                edge_pct REAL,
                peer_books INTEGER,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                metadata_json TEXT
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS signal_price_observations (
                id {id_col},
                signal_id INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                source_snapshot_at TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                price REAL NOT NULL,
                move_vs_entry_pct REAL NOT NULL,
                FOREIGN KEY(signal_id) REFERENCES signals(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS event_results (
                event_id TEXT PRIMARY KEY,
                fetched_at TEXT NOT NULL,
                completed_at TEXT,
                home_score INTEGER NOT NULL,
                away_score INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'the_odds_api',
                raw_json TEXT,
                settlement_quality TEXT,
                settlement_provenance TEXT,
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS canonical_bets (
                id {id_col},
                bet_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                event_id TEXT NOT NULL,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                outcome_description TEXT,
                point REAL,
                source_signal_id INTEGER NOT NULL,
                bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL,
                offered_odds REAL NOT NULL,
                fair_odds REAL NOT NULL,
                fair_probability REAL NOT NULL,
                edge_pct REAL NOT NULL,
                strategy_count INTEGER NOT NULL DEFAULT 1,
                bookmaker_count INTEGER NOT NULL DEFAULT 1,
                detection_count INTEGER NOT NULL DEFAULT 1,
                strategies_json TEXT,
                status TEXT NOT NULL DEFAULT 'OPEN',
                closing_odds REAL,
                clv_pct REAL,
                result TEXT,
                pnl_units REAL,
                FOREIGN KEY(event_id) REFERENCES events(event_id),
                FOREIGN KEY(source_signal_id) REFERENCES signals(id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS execution_shadow_bets (
                id {id_col},
                execution_key TEXT NOT NULL UNIQUE,
                canonical_bet_id INTEGER,
                created_at TEXT NOT NULL,
                event_id TEXT NOT NULL,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                outcome_description TEXT,
                point REAL,
                source_signal_id INTEGER NOT NULL,
                bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL,
                offered_odds REAL NOT NULL,
                fair_odds REAL NOT NULL,
                fair_probability REAL NOT NULL,
                edge_pct REAL NOT NULL,
                min_odds REAL NOT NULL,
                execution_venue_count INTEGER NOT NULL DEFAULT 1,
                strategy_count INTEGER NOT NULL DEFAULT 1,
                bookmaker_count INTEGER NOT NULL DEFAULT 1,
                detection_count INTEGER NOT NULL DEFAULT 1,
                strategies_json TEXT,
                reference_best_odds REAL,
                reference_best_bookmaker_key TEXT,
                reference_best_bookmaker_title TEXT,
                gap_to_reference_pct REAL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                closing_odds REAL,
                clv_pct REAL,
                closing_observed_at TEXT,
                closing_minutes_before_kickoff REAL,
                clv_quality TEXT,
                result TEXT,
                pnl_units REAL,
                commission_rate_pct REAL,
                commission_units REAL,
                net_pnl_units REAL,
                app_version TEXT,
                experiment_version TEXT,
                strategy_version TEXT,
                config_hash TEXT,
                entry_consensus_bookmaker_count INTEGER,
                entry_consensus_median_odds REAL,
                entry_consensus_best_odds REAL,
                entry_price_dispersion_pct REAL,
                entry_chosen_vs_median_pct REAL,
                entry_mean_overround_pct REAL,
                closing_consensus_median_odds REAL,
                closing_reference_best_odds REAL,
                closing_reference_observed_at TEXT,
                closing_reference_minutes_before_kickoff REAL,
                closing_reference_quality TEXT,
                closing_reference_status TEXT,
                clv_vs_consensus_pct REAL,
                clv_vs_best_reference_pct REAL,
                FOREIGN KEY(event_id) REFERENCES events(event_id),
                FOREIGN KEY(source_signal_id) REFERENCES signals(id),
                FOREIGN KEY(canonical_bet_id) REFERENCES canonical_bets(id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS execution_evaluations (
                id {id_col},
                evaluated_at TEXT NOT NULL,
                event_id TEXT NOT NULL,
                execution_key TEXT NOT NULL,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                outcome_description TEXT,
                point REAL,
                approved_books_seen INTEGER NOT NULL DEFAULT 0,
                best_executable_odds REAL,
                min_required_odds REAL,
                fair_odds REAL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                metadata_json TEXT
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS execution_price_observations (
                id {id_col},
                execution_bet_id INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                source_snapshot_at TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                price REAL NOT NULL,
                move_vs_entry_pct REAL NOT NULL,
                FOREIGN KEY(execution_bet_id) REFERENCES execution_shadow_bets(id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS multiple_shadow_bets (
                id {id_col},
                multiple_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                algorithm_version TEXT NOT NULL,
                leg_count INTEGER NOT NULL,
                bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL,
                source_execution_ids_json TEXT NOT NULL,
                market_mix TEXT NOT NULL,
                kickoff_date TEXT NOT NULL,
                first_kickoff TEXT NOT NULL,
                last_kickoff TEXT NOT NULL,
                combined_odds REAL NOT NULL,
                fair_probability REAL NOT NULL,
                fair_odds REAL NOT NULL,
                edge_pct REAL NOT NULL,
                max_entry_quote_age_minutes REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                closing_combined_odds REAL,
                clv_pct REAL,
                clv_quality TEXT,
                result TEXT,
                settled_combined_odds REAL,
                pnl_units REAL,
                settled_at TEXT,
                app_version TEXT,
                experiment_version TEXT,
                config_hash TEXT,
                common_bookmaker_count INTEGER,
                common_bookmakers_json TEXT,
                entry_quote_time_spread_minutes REAL,
                source_pool_size INTEGER,
                automation_eligible INTEGER NOT NULL DEFAULT 0,
                venue_policy TEXT,
                allowed_api_bookmakers_json TEXT,
                closing_consensus_combined_odds REAL,
                closing_best_common_book_odds REAL,
                closing_best_common_bookmaker_key TEXT,
                closing_common_bookmaker_count INTEGER,
                closing_reference_quality TEXT,
                clv_vs_closing_consensus_pct REAL,
                clv_vs_closing_best_common_pct REAL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS multiple_shadow_legs (
                id {id_col},
                multiple_bet_id INTEGER NOT NULL,
                leg_order INTEGER NOT NULL,
                execution_bet_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                outcome_description TEXT,
                point REAL,
                entry_odds REAL NOT NULL,
                min_odds REAL NOT NULL,
                fair_probability REAL NOT NULL,
                fair_odds REAL NOT NULL,
                entry_quote_captured_at TEXT NOT NULL,
                entry_quote_age_minutes REAL NOT NULL,
                closing_odds REAL,
                closing_observed_at TEXT,
                closing_minutes_before_kickoff REAL,
                clv_quality TEXT,
                result TEXT,
                UNIQUE(multiple_bet_id, leg_order),
                FOREIGN KEY(multiple_bet_id) REFERENCES multiple_shadow_bets(id),
                FOREIGN KEY(execution_bet_id) REFERENCES execution_shadow_bets(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS multiple_shadow_state (
                singleton_id INTEGER PRIMARY KEY,
                started_at TEXT NOT NULL,
                algorithm_version TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS manual_system_shadow_bets (
                id {id_col},
                system_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                algorithm_version TEXT NOT NULL,
                system_type TEXT NOT NULL,
                leg_count INTEGER NOT NULL,
                line_count INTEGER NOT NULL,
                bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL,
                placement_mode TEXT NOT NULL,
                manual_placeable INTEGER NOT NULL DEFAULT 0,
                source_cohort TEXT NOT NULL,
                source_engines_json TEXT NOT NULL,
                kickoff_date TEXT NOT NULL,
                first_kickoff TEXT NOT NULL,
                last_kickoff TEXT NOT NULL,
                total_stake_units REAL NOT NULL DEFAULT 1,
                line_stake_units REAL NOT NULL,
                singles_control_stake_units REAL NOT NULL,
                expected_return_units REAL,
                expected_pnl_units REAL,
                expected_roi_pct REAL,
                singles_expected_return_units REAL,
                singles_expected_pnl_units REAL,
                singles_expected_roi_pct REAL,
                all_win_return_units REAL,
                entry_quote_time_spread_minutes REAL,
                entry_price_index REAL,
                closing_price_index REAL,
                clv_pct REAL,
                avg_leg_clv_pct REAL,
                clv_quality TEXT,
                status TEXT NOT NULL DEFAULT 'OPEN',
                result TEXT,
                winning_legs INTEGER,
                system_return_units REAL,
                system_pnl_units REAL,
                system_roi_pct REAL,
                singles_return_units REAL,
                singles_pnl_units REAL,
                singles_roi_pct REAL,
                settled_at TEXT,
                armed_at TEXT,
                confirmed_at TEXT,
                rejected_at TEXT,
                rejection_reason TEXT,
                app_version TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS manual_system_shadow_legs (
                id {id_col},
                system_bet_id INTEGER NOT NULL,
                leg_order INTEGER NOT NULL,
                source_engine TEXT NOT NULL,
                source_table TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                outcome_description TEXT,
                point REAL,
                entry_odds REAL NOT NULL,
                min_odds REAL NOT NULL,
                fair_probability REAL NOT NULL,
                fair_odds REAL NOT NULL,
                source_edge_pct REAL NOT NULL,
                venue_edge_pct REAL NOT NULL,
                entry_quote_captured_at TEXT NOT NULL,
                entry_quote_age_minutes REAL NOT NULL,
                closing_odds REAL,
                closing_observed_at TEXT,
                closing_minutes_before_kickoff REAL,
                clv_quality TEXT,
                clv_pct REAL,
                result TEXT,
                UNIQUE(system_bet_id, leg_order),
                FOREIGN KEY(system_bet_id) REFERENCES manual_system_shadow_bets(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS manual_system_shadow_lines (
                id {id_col},
                system_bet_id INTEGER NOT NULL,
                line_order INTEGER NOT NULL,
                line_size INTEGER NOT NULL,
                leg_orders_json TEXT NOT NULL,
                entry_odds REAL NOT NULL,
                fair_probability REAL NOT NULL,
                expected_return_units REAL,
                expected_pnl_units REAL,
                stake_units REAL NOT NULL,
                result TEXT,
                settled_odds REAL,
                return_units REAL,
                pnl_units REAL,
                UNIQUE(system_bet_id, line_order),
                FOREIGN KEY(system_bet_id) REFERENCES manual_system_shadow_bets(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS tennis_tournament_state (
                sport_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                tour TEXT NOT NULL,
                tournament_level TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_broad_poll_at TEXT,
                last_convergence_poll_at TEXT,
                last_results_poll_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS tennis_events (
                event_id TEXT PRIMARY KEY,
                sport_key TEXT NOT NULL,
                tournament_title TEXT NOT NULL,
                tour TEXT NOT NULL,
                tournament_level TEXT NOT NULL,
                commence_time TEXT NOT NULL,
                player_one TEXT NOT NULL,
                player_two TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'UPCOMING',
                FOREIGN KEY(sport_key) REFERENCES tennis_tournament_state(sport_key)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS tennis_odds_snapshots (
                id {id_col},
                event_id TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                capture_mode TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL,
                bookmaker_last_update TEXT,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                price REAL NOT NULL,
                FOREIGN KEY(event_id) REFERENCES tennis_events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS tennis_consensus_snapshots (
                id {id_col},
                event_id TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                selection TEXT NOT NULL,
                fair_probability REAL NOT NULL,
                fair_odds REAL NOT NULL,
                num_books INTEGER NOT NULL,
                median_reference_odds REAL,
                best_reference_odds REAL,
                price_dispersion_pct REAL,
                mean_overround_pct REAL,
                model TEXT NOT NULL DEFAULT 'two_way_bookmaker_consensus',
                UNIQUE(event_id,captured_at,selection),
                FOREIGN KEY(event_id) REFERENCES tennis_events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS tennis_execution_evaluations (
                id {id_col},
                evaluated_at TEXT NOT NULL,
                event_id TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                approved_books_seen INTEGER NOT NULL DEFAULT 0,
                best_executable_odds REAL,
                min_required_odds REAL,
                fair_probability REAL,
                fair_odds REAL,
                edge_pct REAL,
                consensus_captured_at TEXT,
                consensus_age_minutes REAL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                metadata_json TEXT,
                FOREIGN KEY(event_id) REFERENCES tennis_events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS tennis_execution_bets (
                id {id_col},
                execution_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                event_id TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL,
                offered_odds REAL NOT NULL,
                fair_probability REAL NOT NULL,
                fair_odds REAL NOT NULL,
                edge_pct REAL NOT NULL,
                min_odds REAL NOT NULL,
                approved_books_seen INTEGER NOT NULL DEFAULT 1,
                consensus_captured_at TEXT NOT NULL,
                consensus_age_minutes REAL NOT NULL,
                reference_book_count INTEGER NOT NULL,
                reference_median_odds REAL,
                reference_best_odds REAL,
                reference_dispersion_pct REAL,
                reference_mean_overround_pct REAL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                closing_odds REAL,
                clv_pct REAL,
                closing_observed_at TEXT,
                closing_minutes_before_start REAL,
                clv_quality TEXT,
                closing_consensus_fair_odds REAL,
                closing_consensus_observed_at TEXT,
                closing_consensus_minutes_before_start REAL,
                closing_consensus_quality TEXT,
                clv_vs_consensus_pct REAL,
                result TEXT,
                pnl_units REAL,
                commission_rate_pct REAL,
                commission_units REAL,
                net_pnl_units REAL,
                settled_at TEXT,
                settlement_quality TEXT,
                settlement_provenance TEXT,
                app_version TEXT,
                experiment_version TEXT,
                config_hash TEXT,
                FOREIGN KEY(event_id) REFERENCES tennis_events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS tennis_price_observations (
                id {id_col},
                tennis_bet_id INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                source_snapshot_at TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                price REAL NOT NULL,
                move_vs_entry_pct REAL NOT NULL,
                UNIQUE(tennis_bet_id,source_snapshot_at),
                FOREIGN KEY(tennis_bet_id) REFERENCES tennis_execution_bets(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS tennis_results (
                event_id TEXT PRIMARY KEY,
                fetched_at TEXT NOT NULL,
                completed_at TEXT,
                winner TEXT NOT NULL,
                player_one_score INTEGER,
                player_two_score INTEGER,
                source TEXT NOT NULL DEFAULT 'the_odds_api',
                settlement_quality TEXT NOT NULL DEFAULT 'PROVIDER_COMPLETED',
                settlement_provenance TEXT,
                raw_json TEXT,
                FOREIGN KEY(event_id) REFERENCES tennis_events(event_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS multisport_league_state (
                sport_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                group_name TEXT NOT NULL,
                sport_family TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0,
                targeted INTEGER NOT NULL DEFAULT 1,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_broad_poll_at TEXT,
                last_convergence_poll_at TEXT,
                last_results_poll_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS multisport_events (
                event_id TEXT PRIMARY KEY,
                sport_key TEXT NOT NULL,
                league_title TEXT NOT NULL,
                sport_family TEXT NOT NULL,
                commence_time TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'UPCOMING',
                FOREIGN KEY(sport_key) REFERENCES multisport_league_state(sport_key)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS multisport_odds_snapshots (
                id {id_col},
                event_id TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                capture_mode TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL,
                bookmaker_last_update TEXT,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                price REAL NOT NULL,
                FOREIGN KEY(event_id) REFERENCES multisport_events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS multisport_consensus_snapshots (
                id {id_col},
                event_id TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                selection TEXT NOT NULL,
                fair_probability REAL NOT NULL,
                fair_odds REAL NOT NULL,
                num_books INTEGER NOT NULL,
                median_reference_odds REAL,
                best_reference_odds REAL,
                price_dispersion_pct REAL,
                mean_overround_pct REAL,
                model TEXT NOT NULL DEFAULT 'two_way_bookmaker_consensus',
                UNIQUE(event_id,captured_at,selection),
                FOREIGN KEY(event_id) REFERENCES multisport_events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS multisport_execution_evaluations (
                id {id_col},
                evaluated_at TEXT NOT NULL,
                event_id TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                approved_books_seen INTEGER NOT NULL DEFAULT 0,
                valid_two_way_books_seen INTEGER NOT NULL DEFAULT 0,
                best_executable_odds REAL,
                min_required_odds REAL,
                fair_probability REAL,
                fair_odds REAL,
                edge_pct REAL,
                consensus_captured_at TEXT,
                consensus_age_minutes REAL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                metadata_json TEXT,
                FOREIGN KEY(event_id) REFERENCES multisport_events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS multisport_execution_bets (
                id {id_col},
                execution_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                event_id TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL,
                offered_odds REAL NOT NULL,
                fair_probability REAL NOT NULL,
                fair_odds REAL NOT NULL,
                edge_pct REAL NOT NULL,
                min_odds REAL NOT NULL,
                approved_books_seen INTEGER NOT NULL DEFAULT 1,
                consensus_captured_at TEXT NOT NULL,
                consensus_age_minutes REAL NOT NULL,
                reference_book_count INTEGER NOT NULL,
                reference_median_odds REAL,
                reference_best_odds REAL,
                reference_dispersion_pct REAL,
                reference_mean_overround_pct REAL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                closing_odds REAL,
                clv_pct REAL,
                closing_observed_at TEXT,
                closing_minutes_before_start REAL,
                clv_quality TEXT,
                closing_consensus_fair_odds REAL,
                closing_consensus_observed_at TEXT,
                closing_consensus_minutes_before_start REAL,
                closing_consensus_quality TEXT,
                clv_vs_consensus_pct REAL,
                result TEXT,
                pnl_units REAL,
                commission_rate_pct REAL,
                commission_units REAL,
                net_pnl_units REAL,
                settled_at TEXT,
                app_version TEXT,
                experiment_version TEXT,
                config_hash TEXT,
                FOREIGN KEY(event_id) REFERENCES multisport_events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS multisport_price_observations (
                id {id_col},
                multisport_bet_id INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                source_snapshot_at TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                price REAL NOT NULL,
                move_vs_entry_pct REAL NOT NULL,
                UNIQUE(multisport_bet_id,source_snapshot_at),
                FOREIGN KEY(multisport_bet_id) REFERENCES multisport_execution_bets(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS multisport_results (
                event_id TEXT PRIMARY KEY,
                fetched_at TEXT NOT NULL,
                completed_at TEXT,
                home_score INTEGER NOT NULL,
                away_score INTEGER NOT NULL,
                winner TEXT,
                result_kind TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'the_odds_api',
                settlement_quality TEXT NOT NULL DEFAULT 'PROVIDER_COMPLETED',
                settlement_provenance TEXT,
                raw_json TEXT,
                FOREIGN KEY(event_id) REFERENCES multisport_events(event_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS multisport_lines_state (
                sport_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                sport_family TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_broad_poll_at TEXT,
                last_convergence_poll_at TEXT,
                last_results_poll_at TEXT
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS multisport_line_odds_snapshots (
                id {id_col},
                event_id TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                capture_mode TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL,
                bookmaker_last_update TEXT,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                outcome_point REAL NOT NULL,
                line_point REAL NOT NULL,
                price REAL NOT NULL,
                FOREIGN KEY(event_id) REFERENCES multisport_events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS multisport_line_consensus_snapshots (
                id {id_col},
                event_id TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                market_key TEXT NOT NULL,
                line_point REAL NOT NULL,
                selection TEXT NOT NULL,
                fair_probability REAL NOT NULL,
                fair_odds REAL NOT NULL,
                num_books INTEGER NOT NULL,
                median_reference_odds REAL,
                best_reference_odds REAL,
                price_dispersion_pct REAL,
                mean_overround_pct REAL,
                UNIQUE(event_id,captured_at,market_key,line_point,selection),
                FOREIGN KEY(event_id) REFERENCES multisport_events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS multisport_line_evaluations (
                id {id_col},
                evaluated_at TEXT NOT NULL,
                event_id TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                line_point REAL,
                approved_books_seen INTEGER NOT NULL DEFAULT 0,
                valid_books_seen INTEGER NOT NULL DEFAULT 0,
                best_executable_odds REAL,
                min_required_odds REAL,
                fair_probability REAL,
                fair_odds REAL,
                edge_pct REAL,
                consensus_captured_at TEXT,
                consensus_age_minutes REAL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                metadata_json TEXT,
                FOREIGN KEY(event_id) REFERENCES multisport_events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS multisport_line_bets (
                id {id_col},
                execution_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                event_id TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                line_point REAL NOT NULL,
                bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL,
                offered_odds REAL NOT NULL,
                fair_probability REAL NOT NULL,
                fair_odds REAL NOT NULL,
                edge_pct REAL NOT NULL,
                min_odds REAL NOT NULL,
                approved_books_seen INTEGER NOT NULL DEFAULT 1,
                consensus_captured_at TEXT NOT NULL,
                consensus_age_minutes REAL NOT NULL,
                reference_book_count INTEGER NOT NULL,
                reference_median_odds REAL,
                reference_best_odds REAL,
                reference_dispersion_pct REAL,
                reference_mean_overround_pct REAL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                closing_odds REAL,
                closing_line_point REAL,
                price_clv_pct REAL,
                line_clv_points REAL,
                closing_observed_at TEXT,
                closing_minutes_before_start REAL,
                close_quality TEXT,
                result TEXT,
                pnl_units REAL,
                commission_rate_pct REAL,
                commission_units REAL,
                net_pnl_units REAL,
                settled_at TEXT,
                settlement_quality TEXT,
                settlement_provenance TEXT,
                app_version TEXT,
                experiment_version TEXT,
                config_hash TEXT,
                FOREIGN KEY(event_id) REFERENCES multisport_events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS multisport_line_price_observations (
                id {id_col},
                line_bet_id INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                source_snapshot_at TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                line_point REAL NOT NULL,
                price REAL NOT NULL,
                price_move_pct REAL,
                line_move_points REAL,
                UNIQUE(line_bet_id,source_snapshot_at,line_point),
                FOREIGN KEY(line_bet_id) REFERENCES multisport_line_bets(id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive_training_matches (
                id {id_col},
                match_key TEXT NOT NULL UNIQUE,
                sport_key TEXT NOT NULL,
                played_at TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                home_goals INTEGER NOT NULL,
                away_goals INTEGER NOT NULL,
                source TEXT NOT NULL,
                source_ref TEXT,
                imported_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS football_predictive_source_state (
                source_key TEXT PRIMARY KEY,
                last_attempt_at TEXT,
                last_success_at TEXT,
                rows_imported INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive_predictions (
                id {id_col},
                event_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                league TEXT NOT NULL,
                commence_time TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                mapped_home_team TEXT NOT NULL,
                mapped_away_team TEXT NOT NULL,
                home_mapping_score REAL NOT NULL,
                away_mapping_score REAL NOT NULL,
                expected_home_goals REAL NOT NULL,
                expected_away_goals REAL NOT NULL,
                home_probability REAL NOT NULL,
                draw_probability REAL NOT NULL,
                away_probability REAL NOT NULL,
                home_fair_odds REAL NOT NULL,
                draw_fair_odds REAL NOT NULL,
                away_fair_odds REAL NOT NULL,
                league_training_matches INTEGER NOT NULL,
                home_effective_matches REAL NOT NULL,
                away_effective_matches REAL NOT NULL,
                model_version TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                actual_outcome TEXT,
                brier_score REAL,
                log_loss REAL,
                closing_home_probability REAL,
                closing_draw_probability REAL,
                closing_away_probability REAL,
                closing_market_observed_at TEXT,
                closing_market_quality TEXT,
                closing_market_brier REAL,
                model_brier_advantage REAL,
                settled_at TEXT,
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive_evaluations (
                id {id_col},
                prediction_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                selection TEXT NOT NULL,
                bookmaker_key TEXT,
                bookmaker_title TEXT,
                best_executable_odds REAL,
                model_probability REAL NOT NULL,
                model_fair_odds REAL NOT NULL,
                min_required_odds REAL NOT NULL,
                edge_pct REAL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                strong_candidate INTEGER NOT NULL DEFAULT 0,
                UNIQUE(prediction_id,evaluated_at,selection),
                FOREIGN KEY(prediction_id) REFERENCES football_predictive_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive_bets (
                id {id_col},
                predictive_key TEXT NOT NULL UNIQUE,
                prediction_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                selection TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL,
                offered_odds REAL NOT NULL,
                model_probability REAL NOT NULL,
                model_fair_odds REAL NOT NULL,
                edge_pct REAL NOT NULL,
                min_odds REAL NOT NULL,
                strong_candidate INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'OPEN',
                closing_odds REAL,
                clv_pct REAL,
                closing_observed_at TEXT,
                closing_minutes_before_kickoff REAL,
                clv_quality TEXT,
                result TEXT,
                pnl_units REAL,
                commission_rate_pct REAL,
                commission_units REAL,
                net_pnl_units REAL,
                settled_at TEXT,
                app_version TEXT NOT NULL,
                experiment_version TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                FOREIGN KEY(prediction_id) REFERENCES football_predictive_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive_price_observations (
                id {id_col},
                predictive_bet_id INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                source_snapshot_at TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                price REAL NOT NULL,
                move_vs_entry_pct REAL NOT NULL,
                UNIQUE(predictive_bet_id,source_snapshot_at),
                FOREIGN KEY(predictive_bet_id) REFERENCES football_predictive_bets(id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive_market_predictions (
                id {id_col},
                prediction_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                point REAL,
                line_key TEXT NOT NULL,
                probability REAL NOT NULL,
                fair_odds REAL NOT NULL,
                actual_hit INTEGER,
                brier_score REAL,
                settled_at TEXT,
                UNIQUE(prediction_id,market_key,selection,line_key),
                FOREIGN KEY(prediction_id) REFERENCES football_predictive_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive_market_evaluations (
                id {id_col},
                market_prediction_id INTEGER NOT NULL,
                prediction_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                point REAL,
                line_key TEXT NOT NULL,
                bookmaker_key TEXT,
                bookmaker_title TEXT,
                best_executable_odds REAL,
                model_probability REAL NOT NULL,
                model_fair_odds REAL NOT NULL,
                min_required_odds REAL NOT NULL,
                edge_pct REAL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                strong_candidate INTEGER NOT NULL DEFAULT 0,
                UNIQUE(market_prediction_id,evaluated_at),
                FOREIGN KEY(market_prediction_id) REFERENCES football_predictive_market_predictions(id),
                FOREIGN KEY(prediction_id) REFERENCES football_predictive_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive_market_bets (
                id {id_col},
                predictive_key TEXT NOT NULL UNIQUE,
                market_prediction_id INTEGER NOT NULL,
                prediction_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                point REAL,
                line_key TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL,
                offered_odds REAL NOT NULL,
                model_probability REAL NOT NULL,
                model_fair_odds REAL NOT NULL,
                edge_pct REAL NOT NULL,
                min_odds REAL NOT NULL,
                strong_candidate INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'OPEN',
                closing_odds REAL,
                clv_pct REAL,
                closing_observed_at TEXT,
                closing_minutes_before_kickoff REAL,
                clv_quality TEXT,
                result TEXT,
                pnl_units REAL,
                commission_rate_pct REAL,
                commission_units REAL,
                net_pnl_units REAL,
                settled_at TEXT,
                app_version TEXT NOT NULL,
                experiment_version TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                FOREIGN KEY(market_prediction_id) REFERENCES football_predictive_market_predictions(id),
                FOREIGN KEY(prediction_id) REFERENCES football_predictive_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive_market_price_observations (
                id {id_col},
                predictive_bet_id INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                source_snapshot_at TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                price REAL NOT NULL,
                move_vs_entry_pct REAL NOT NULL,
                UNIQUE(predictive_bet_id,source_snapshot_at),
                FOREIGN KEY(predictive_bet_id) REFERENCES football_predictive_market_bets(id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive2_predictions (
                id {id_col},
                event_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                league TEXT NOT NULL,
                commence_time TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                mapped_home_team TEXT NOT NULL,
                mapped_away_team TEXT NOT NULL,
                home_mapping_score REAL NOT NULL,
                away_mapping_score REAL NOT NULL,
                expected_home_goals REAL NOT NULL,
                expected_away_goals REAL NOT NULL,
                dixon_coles_rho REAL NOT NULL,
                home_probability REAL NOT NULL,
                draw_probability REAL NOT NULL,
                away_probability REAL NOT NULL,
                home_fair_odds REAL NOT NULL,
                draw_fair_odds REAL NOT NULL,
                away_fair_odds REAL NOT NULL,
                league_training_matches INTEGER NOT NULL,
                home_effective_matches REAL NOT NULL,
                away_effective_matches REAL NOT NULL,
                model_version TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                actual_outcome TEXT,
                brier_score REAL,
                log_loss REAL,
                closing_home_probability REAL,
                closing_draw_probability REAL,
                closing_away_probability REAL,
                closing_market_observed_at TEXT,
                closing_market_quality TEXT,
                closing_market_brier REAL,
                model_brier_advantage REAL,
                settled_at TEXT,
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive2_evaluations (
                id {id_col},
                prediction_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                selection TEXT NOT NULL,
                bookmaker_key TEXT,
                bookmaker_title TEXT,
                best_executable_odds REAL,
                model_probability REAL NOT NULL,
                model_fair_odds REAL NOT NULL,
                min_required_odds REAL NOT NULL,
                edge_pct REAL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                strong_candidate INTEGER NOT NULL DEFAULT 0,
                UNIQUE(prediction_id,evaluated_at,selection),
                FOREIGN KEY(prediction_id) REFERENCES football_predictive2_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive2_bets (
                id {id_col},
                predictive_key TEXT NOT NULL UNIQUE,
                prediction_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                selection TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL,
                offered_odds REAL NOT NULL,
                model_probability REAL NOT NULL,
                model_fair_odds REAL NOT NULL,
                edge_pct REAL NOT NULL,
                min_odds REAL NOT NULL,
                strong_candidate INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'OPEN',
                closing_odds REAL,
                clv_pct REAL,
                closing_observed_at TEXT,
                closing_minutes_before_kickoff REAL,
                clv_quality TEXT,
                result TEXT,
                pnl_units REAL,
                commission_rate_pct REAL,
                commission_units REAL,
                net_pnl_units REAL,
                settled_at TEXT,
                app_version TEXT NOT NULL,
                experiment_version TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                FOREIGN KEY(prediction_id) REFERENCES football_predictive2_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive2_price_observations (
                id {id_col},
                predictive_bet_id INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                source_snapshot_at TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                price REAL NOT NULL,
                move_vs_entry_pct REAL NOT NULL,
                UNIQUE(predictive_bet_id,source_snapshot_at),
                FOREIGN KEY(predictive_bet_id) REFERENCES football_predictive2_bets(id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive2_market_predictions (
                id {id_col},
                prediction_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                point REAL,
                line_key TEXT NOT NULL,
                probability REAL NOT NULL,
                fair_odds REAL NOT NULL,
                actual_hit INTEGER,
                brier_score REAL,
                settled_at TEXT,
                UNIQUE(prediction_id,market_key,selection,line_key),
                FOREIGN KEY(prediction_id) REFERENCES football_predictive2_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive2_market_evaluations (
                id {id_col},
                market_prediction_id INTEGER NOT NULL,
                prediction_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                point REAL,
                line_key TEXT NOT NULL,
                bookmaker_key TEXT,
                bookmaker_title TEXT,
                best_executable_odds REAL,
                model_probability REAL NOT NULL,
                model_fair_odds REAL NOT NULL,
                min_required_odds REAL NOT NULL,
                edge_pct REAL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                strong_candidate INTEGER NOT NULL DEFAULT 0,
                UNIQUE(market_prediction_id,evaluated_at),
                FOREIGN KEY(market_prediction_id) REFERENCES football_predictive2_market_predictions(id),
                FOREIGN KEY(prediction_id) REFERENCES football_predictive2_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive2_market_bets (
                id {id_col},
                predictive_key TEXT NOT NULL UNIQUE,
                market_prediction_id INTEGER NOT NULL,
                prediction_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                point REAL,
                line_key TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL,
                offered_odds REAL NOT NULL,
                model_probability REAL NOT NULL,
                model_fair_odds REAL NOT NULL,
                edge_pct REAL NOT NULL,
                min_odds REAL NOT NULL,
                strong_candidate INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'OPEN',
                closing_odds REAL,
                clv_pct REAL,
                closing_observed_at TEXT,
                closing_minutes_before_kickoff REAL,
                clv_quality TEXT,
                result TEXT,
                pnl_units REAL,
                commission_rate_pct REAL,
                commission_units REAL,
                net_pnl_units REAL,
                settled_at TEXT,
                app_version TEXT NOT NULL,
                experiment_version TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                FOREIGN KEY(market_prediction_id) REFERENCES football_predictive2_market_predictions(id),
                FOREIGN KEY(prediction_id) REFERENCES football_predictive2_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive2_market_price_observations (
                id {id_col},
                predictive_bet_id INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                source_snapshot_at TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                price REAL NOT NULL,
                move_vs_entry_pct REAL NOT NULL,
                UNIQUE(predictive_bet_id,source_snapshot_at),
                FOREIGN KEY(predictive_bet_id) REFERENCES football_predictive2_market_bets(id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive3_training_matches (
                id {id_col},
                statsbomb_match_id TEXT NOT NULL UNIQUE,
                sport_key TEXT NOT NULL,
                competition_name TEXT NOT NULL,
                season_name TEXT,
                played_at TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                home_goals INTEGER NOT NULL,
                away_goals INTEGER NOT NULL,
                home_xg REAL NOT NULL,
                away_xg REAL NOT NULL,
                home_npxg REAL NOT NULL,
                away_npxg REAL NOT NULL,
                home_shots INTEGER NOT NULL,
                away_shots INTEGER NOT NULL,
                home_pressures INTEGER NOT NULL,
                away_pressures INTEGER NOT NULL,
                source_ref TEXT,
                imported_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS football_predictive3_source_state (
                source_key TEXT PRIMARY KEY,
                last_attempt_at TEXT,
                last_success_at TEXT,
                rows_imported INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS football_predictive3_statsbomb_manifest (
                match_id TEXT PRIMARY KEY,
                competition_id INTEGER NOT NULL,
                season_id INTEGER NOT NULL,
                competition_name TEXT NOT NULL,
                season_name TEXT,
                sport_key TEXT NOT NULL,
                played_at TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                home_score INTEGER,
                away_score INTEGER,
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive3_predictions (
                id {id_col},
                event_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                league TEXT NOT NULL,
                commence_time TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                mapped_home_team TEXT NOT NULL,
                mapped_away_team TEXT NOT NULL,
                home_mapping_score REAL NOT NULL,
                away_mapping_score REAL NOT NULL,
                expected_home_goals REAL NOT NULL,
                expected_away_goals REAL NOT NULL,
                home_probability REAL NOT NULL,
                draw_probability REAL NOT NULL,
                away_probability REAL NOT NULL,
                home_fair_odds REAL NOT NULL,
                draw_fair_odds REAL NOT NULL,
                away_fair_odds REAL NOT NULL,
                league_training_matches INTEGER NOT NULL,
                home_effective_matches REAL NOT NULL,
                away_effective_matches REAL NOT NULL,
                xg_data_age_days REAL NOT NULL,
                home_latest_statsbomb_at TEXT,
                away_latest_statsbomb_at TEXT,
                model_version TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                actual_outcome TEXT,
                brier_score REAL,
                log_loss REAL,
                closing_home_probability REAL,
                closing_draw_probability REAL,
                closing_away_probability REAL,
                closing_market_observed_at TEXT,
                closing_market_quality TEXT,
                closing_market_brier REAL,
                model_brier_advantage REAL,
                settled_at TEXT,
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive3_evaluations (
                id {id_col}, prediction_id INTEGER NOT NULL, event_id TEXT NOT NULL,
                evaluated_at TEXT NOT NULL, selection TEXT NOT NULL, bookmaker_key TEXT,
                bookmaker_title TEXT, best_executable_odds REAL, model_probability REAL NOT NULL,
                model_fair_odds REAL NOT NULL, min_required_odds REAL NOT NULL, edge_pct REAL,
                decision TEXT NOT NULL, reason TEXT NOT NULL, strong_candidate INTEGER NOT NULL DEFAULT 0,
                UNIQUE(prediction_id,evaluated_at,selection),
                FOREIGN KEY(prediction_id) REFERENCES football_predictive3_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive3_bets (
                id {id_col}, predictive_key TEXT NOT NULL UNIQUE, prediction_id INTEGER NOT NULL,
                event_id TEXT NOT NULL, created_at TEXT NOT NULL, selection TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL, bookmaker_title TEXT NOT NULL, offered_odds REAL NOT NULL,
                model_probability REAL NOT NULL, model_fair_odds REAL NOT NULL, edge_pct REAL NOT NULL,
                min_odds REAL NOT NULL, strong_candidate INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'OPEN', closing_odds REAL, clv_pct REAL,
                closing_observed_at TEXT, closing_minutes_before_kickoff REAL, clv_quality TEXT,
                result TEXT, pnl_units REAL, commission_rate_pct REAL, commission_units REAL,
                net_pnl_units REAL, settled_at TEXT, app_version TEXT NOT NULL,
                experiment_version TEXT NOT NULL, config_hash TEXT NOT NULL,
                FOREIGN KEY(prediction_id) REFERENCES football_predictive3_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive3_price_observations (
                id {id_col}, predictive_bet_id INTEGER NOT NULL, observed_at TEXT NOT NULL,
                source_snapshot_at TEXT NOT NULL, bookmaker_key TEXT NOT NULL, price REAL NOT NULL,
                move_vs_entry_pct REAL NOT NULL, UNIQUE(predictive_bet_id,source_snapshot_at),
                FOREIGN KEY(predictive_bet_id) REFERENCES football_predictive3_bets(id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive3_market_predictions (
                id {id_col}, prediction_id INTEGER NOT NULL, event_id TEXT NOT NULL, created_at TEXT NOT NULL,
                market_key TEXT NOT NULL, selection TEXT NOT NULL, point REAL, line_key TEXT NOT NULL,
                probability REAL NOT NULL, fair_odds REAL NOT NULL, actual_hit INTEGER, brier_score REAL,
                settled_at TEXT, UNIQUE(prediction_id,market_key,selection,line_key),
                FOREIGN KEY(prediction_id) REFERENCES football_predictive3_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive3_market_evaluations (
                id {id_col}, market_prediction_id INTEGER NOT NULL, prediction_id INTEGER NOT NULL,
                event_id TEXT NOT NULL, evaluated_at TEXT NOT NULL, market_key TEXT NOT NULL,
                selection TEXT NOT NULL, point REAL, line_key TEXT NOT NULL, bookmaker_key TEXT,
                bookmaker_title TEXT, best_executable_odds REAL, model_probability REAL NOT NULL,
                model_fair_odds REAL NOT NULL, min_required_odds REAL NOT NULL, edge_pct REAL,
                decision TEXT NOT NULL, reason TEXT NOT NULL, strong_candidate INTEGER NOT NULL DEFAULT 0,
                UNIQUE(market_prediction_id,evaluated_at),
                FOREIGN KEY(market_prediction_id) REFERENCES football_predictive3_market_predictions(id),
                FOREIGN KEY(prediction_id) REFERENCES football_predictive3_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive3_market_bets (
                id {id_col}, predictive_key TEXT NOT NULL UNIQUE, market_prediction_id INTEGER NOT NULL,
                prediction_id INTEGER NOT NULL, event_id TEXT NOT NULL, created_at TEXT NOT NULL,
                market_key TEXT NOT NULL, selection TEXT NOT NULL, point REAL, line_key TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL, bookmaker_title TEXT NOT NULL, offered_odds REAL NOT NULL,
                model_probability REAL NOT NULL, model_fair_odds REAL NOT NULL, edge_pct REAL NOT NULL,
                min_odds REAL NOT NULL, strong_candidate INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'OPEN', closing_odds REAL, clv_pct REAL,
                closing_observed_at TEXT, closing_minutes_before_kickoff REAL, clv_quality TEXT,
                result TEXT, pnl_units REAL, commission_rate_pct REAL, commission_units REAL,
                net_pnl_units REAL, settled_at TEXT, app_version TEXT NOT NULL,
                experiment_version TEXT NOT NULL, config_hash TEXT NOT NULL,
                FOREIGN KEY(market_prediction_id) REFERENCES football_predictive3_market_predictions(id),
                FOREIGN KEY(prediction_id) REFERENCES football_predictive3_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive3_market_price_observations (
                id {id_col}, predictive_bet_id INTEGER NOT NULL, observed_at TEXT NOT NULL,
                source_snapshot_at TEXT NOT NULL, bookmaker_key TEXT NOT NULL, price REAL NOT NULL,
                move_vs_entry_pct REAL NOT NULL, UNIQUE(predictive_bet_id,source_snapshot_at),
                FOREIGN KEY(predictive_bet_id) REFERENCES football_predictive3_market_bets(id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive4_predictions (
                id {id_col},
                event_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                league TEXT NOT NULL,
                commence_time TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                mapped_home_team TEXT NOT NULL,
                mapped_away_team TEXT NOT NULL,
                home_mapping_score REAL NOT NULL,
                away_mapping_score REAL NOT NULL,
                expected_home_goals REAL NOT NULL,
                expected_away_goals REAL NOT NULL,
                home_probability REAL NOT NULL,
                draw_probability REAL NOT NULL,
                away_probability REAL NOT NULL,
                home_fair_odds REAL NOT NULL,
                draw_fair_odds REAL NOT NULL,
                away_fair_odds REAL NOT NULL,
                league_training_matches INTEGER NOT NULL,
                home_effective_matches REAL NOT NULL,
                away_effective_matches REAL NOT NULL,
                pxg_data_age_days REAL NOT NULL,
                home_latest_pxg_at TEXT,
                away_latest_pxg_at TEXT,
                model_version TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                actual_outcome TEXT,
                brier_score REAL,
                log_loss REAL,
                closing_home_probability REAL,
                closing_draw_probability REAL,
                closing_away_probability REAL,
                closing_market_observed_at TEXT,
                closing_market_quality TEXT,
                closing_market_brier REAL,
                model_brier_advantage REAL,
                settled_at TEXT,
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive4_evaluations (
                id {id_col}, prediction_id INTEGER NOT NULL, event_id TEXT NOT NULL,
                evaluated_at TEXT NOT NULL, selection TEXT NOT NULL, bookmaker_key TEXT,
                bookmaker_title TEXT, best_executable_odds REAL, model_probability REAL NOT NULL,
                model_fair_odds REAL NOT NULL, min_required_odds REAL NOT NULL, edge_pct REAL,
                decision TEXT NOT NULL, reason TEXT NOT NULL, strong_candidate INTEGER NOT NULL DEFAULT 0,
                UNIQUE(prediction_id,evaluated_at,selection),
                FOREIGN KEY(prediction_id) REFERENCES football_predictive4_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive4_bets (
                id {id_col}, predictive_key TEXT NOT NULL UNIQUE, prediction_id INTEGER NOT NULL,
                event_id TEXT NOT NULL, created_at TEXT NOT NULL, selection TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL, bookmaker_title TEXT NOT NULL, offered_odds REAL NOT NULL,
                model_probability REAL NOT NULL, model_fair_odds REAL NOT NULL, edge_pct REAL NOT NULL,
                min_odds REAL NOT NULL, strong_candidate INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'OPEN', closing_odds REAL, clv_pct REAL,
                closing_observed_at TEXT, closing_minutes_before_kickoff REAL, clv_quality TEXT,
                result TEXT, pnl_units REAL, commission_rate_pct REAL, commission_units REAL,
                net_pnl_units REAL, settled_at TEXT, app_version TEXT NOT NULL,
                experiment_version TEXT NOT NULL, config_hash TEXT NOT NULL,
                FOREIGN KEY(prediction_id) REFERENCES football_predictive4_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive4_price_observations (
                id {id_col}, predictive_bet_id INTEGER NOT NULL, observed_at TEXT NOT NULL,
                source_snapshot_at TEXT NOT NULL, bookmaker_key TEXT NOT NULL, price REAL NOT NULL,
                move_vs_entry_pct REAL NOT NULL, UNIQUE(predictive_bet_id,source_snapshot_at),
                FOREIGN KEY(predictive_bet_id) REFERENCES football_predictive4_bets(id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive4_market_predictions (
                id {id_col}, prediction_id INTEGER NOT NULL, event_id TEXT NOT NULL, created_at TEXT NOT NULL,
                market_key TEXT NOT NULL, selection TEXT NOT NULL, point REAL, line_key TEXT NOT NULL,
                probability REAL NOT NULL, fair_odds REAL NOT NULL, actual_hit INTEGER, brier_score REAL,
                settled_at TEXT, UNIQUE(prediction_id,market_key,selection,line_key),
                FOREIGN KEY(prediction_id) REFERENCES football_predictive4_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive4_market_evaluations (
                id {id_col}, market_prediction_id INTEGER NOT NULL, prediction_id INTEGER NOT NULL,
                event_id TEXT NOT NULL, evaluated_at TEXT NOT NULL, market_key TEXT NOT NULL,
                selection TEXT NOT NULL, point REAL, line_key TEXT NOT NULL, bookmaker_key TEXT,
                bookmaker_title TEXT, best_executable_odds REAL, model_probability REAL NOT NULL,
                model_fair_odds REAL NOT NULL, min_required_odds REAL NOT NULL, edge_pct REAL,
                decision TEXT NOT NULL, reason TEXT NOT NULL, strong_candidate INTEGER NOT NULL DEFAULT 0,
                UNIQUE(market_prediction_id,evaluated_at),
                FOREIGN KEY(market_prediction_id) REFERENCES football_predictive4_market_predictions(id),
                FOREIGN KEY(prediction_id) REFERENCES football_predictive4_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive4_market_bets (
                id {id_col}, predictive_key TEXT NOT NULL UNIQUE, market_prediction_id INTEGER NOT NULL,
                prediction_id INTEGER NOT NULL, event_id TEXT NOT NULL, created_at TEXT NOT NULL,
                market_key TEXT NOT NULL, selection TEXT NOT NULL, point REAL, line_key TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL, bookmaker_title TEXT NOT NULL, offered_odds REAL NOT NULL,
                model_probability REAL NOT NULL, model_fair_odds REAL NOT NULL, edge_pct REAL NOT NULL,
                min_odds REAL NOT NULL, strong_candidate INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'OPEN', closing_odds REAL, clv_pct REAL,
                closing_observed_at TEXT, closing_minutes_before_kickoff REAL, clv_quality TEXT,
                result TEXT, pnl_units REAL, commission_rate_pct REAL, commission_units REAL,
                net_pnl_units REAL, settled_at TEXT, app_version TEXT NOT NULL,
                experiment_version TEXT NOT NULL, config_hash TEXT NOT NULL,
                FOREIGN KEY(market_prediction_id) REFERENCES football_predictive4_market_predictions(id),
                FOREIGN KEY(prediction_id) REFERENCES football_predictive4_predictions(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive4_market_price_observations (
                id {id_col}, predictive_bet_id INTEGER NOT NULL, observed_at TEXT NOT NULL,
                source_snapshot_at TEXT NOT NULL, bookmaker_key TEXT NOT NULL, price REAL NOT NULL,
                move_vs_entry_pct REAL NOT NULL, UNIQUE(predictive_bet_id,source_snapshot_at),
                FOREIGN KEY(predictive_bet_id) REFERENCES football_predictive4_market_bets(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS football_pxg_statsbomb_manifest (
                match_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_pxg_statsbomb_samples (
                id {id_col},
                statsbomb_match_id TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                competition_name TEXT NOT NULL,
                season_name TEXT,
                played_at TEXT NOT NULL,
                team TEXT NOT NULL,
                opponent TEXT NOT NULL,
                is_home INTEGER NOT NULL,
                target_xg REAL NOT NULL,
                target_npxg REAL NOT NULL,
                total_shots REAL NOT NULL,
                shots_on_target REAL NOT NULL,
                shots_off_target REAL NOT NULL,
                blocked_shots REAL NOT NULL,
                shots_inside_box REAL NOT NULL,
                shots_outside_box REAL NOT NULL,
                corners REAL NOT NULL,
                red_cards REAL NOT NULL,
                feature_version TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                UNIQUE(statsbomb_match_id,team)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_pxg_models (
                id {id_col},
                trained_at TEXT NOT NULL,
                model_version TEXT NOT NULL,
                feature_version TEXT NOT NULL,
                status TEXT NOT NULL,
                sample_count INTEGER NOT NULL,
                training_count INTEGER NOT NULL,
                holdout_count INTEGER NOT NULL,
                training_cutoff TEXT,
                coefficients_json TEXT NOT NULL,
                holdout_mae REAL NOT NULL,
                holdout_rmse REAL NOT NULL,
                baseline_mae REAL NOT NULL,
                baseline_rmse REAL NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS football_pxg_api_discovery_days (
                day TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                fixtures_seen INTEGER NOT NULL DEFAULT 0,
                relevant_found INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS football_pxg_api_manifest (
                fixture_id TEXT PRIMARY KEY,
                sport_key TEXT NOT NULL,
                league_name TEXT NOT NULL,
                country TEXT,
                played_at TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_pxg_current_matches (
                id {id_col},
                fixture_id TEXT NOT NULL UNIQUE,
                sport_key TEXT NOT NULL,
                league_name TEXT NOT NULL,
                country TEXT,
                played_at TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                home_goals INTEGER,
                away_goals INTEGER,
                home_features_json TEXT NOT NULL,
                away_features_json TEXT NOT NULL,
                home_proxy_xg REAL,
                away_proxy_xg REAL,
                pxg_model_id INTEGER,
                source TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                scored_at TEXT,
                FOREIGN KEY(pxg_model_id) REFERENCES football_pxg_models(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS football_pxg_api_usage (
                usage_date TEXT PRIMARY KEY,
                calls INTEGER NOT NULL DEFAULT 0,
                provider_remaining INTEGER,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS cohort_system_shadow_state (
                singleton_id INTEGER PRIMARY KEY,
                started_at TEXT NOT NULL,
                algorithm_version TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS cohort_system_shadow_bets (
                id {id_col},
                system_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                algorithm_version TEXT NOT NULL,
                cohort_key TEXT NOT NULL,
                system_type TEXT NOT NULL,
                leg_count INTEGER NOT NULL,
                line_count INTEGER NOT NULL,
                pricing_mode TEXT NOT NULL,
                total_stake_units REAL NOT NULL DEFAULT 1,
                line_stake_units REAL NOT NULL,
                singles_control_stake_units REAL NOT NULL,
                first_kickoff TEXT NOT NULL,
                last_kickoff TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                result TEXT,
                winning_legs INTEGER,
                push_legs INTEGER,
                system_return_units REAL,
                system_pnl_units REAL,
                system_roi_pct REAL,
                singles_return_units REAL,
                singles_pnl_units REAL,
                singles_roi_pct REAL,
                avg_leg_clv_pct REAL,
                ab_clv_samples INTEGER NOT NULL DEFAULT 0,
                settled_at TEXT,
                app_version TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS cohort_system_shadow_legs (
                id {id_col},
                system_bet_id INTEGER NOT NULL,
                leg_order INTEGER NOT NULL,
                selection_key TEXT NOT NULL,
                source_engine TEXT NOT NULL,
                source_table TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                point REAL,
                entry_odds REAL NOT NULL,
                fair_probability REAL NOT NULL,
                min_odds REAL NOT NULL,
                bookmaker_key TEXT,
                source_created_at TEXT NOT NULL,
                entry_quote_observed_at TEXT NOT NULL,
                entry_quote_age_minutes REAL NOT NULL,
                closing_odds REAL,
                clv_pct REAL,
                clv_quality TEXT,
                result TEXT,
                UNIQUE(system_bet_id,leg_order),
                FOREIGN KEY(system_bet_id) REFERENCES cohort_system_shadow_bets(id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS cohort_system_shadow_lines (
                id {id_col},
                system_bet_id INTEGER NOT NULL,
                line_order INTEGER NOT NULL,
                line_size INTEGER NOT NULL,
                leg_orders_json TEXT NOT NULL,
                entry_odds REAL NOT NULL,
                stake_units REAL NOT NULL,
                result TEXT,
                return_units REAL,
                pnl_units REAL,
                UNIQUE(system_bet_id,line_order),
                FOREIGN KEY(system_bet_id) REFERENCES cohort_system_shadow_bets(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS outcome_edge_watch_cohorts (
                cohort_key TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                definition TEXT NOT NULL,
                frozen_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS meta_edge_samples (
                id {id_col},
                sample_key TEXT NOT NULL UNIQUE,
                feature_version TEXT NOT NULL,
                source_model TEXT NOT NULL,
                source_bet_table TEXT NOT NULL,
                source_bet_id INTEGER NOT NULL,
                source_prediction_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                entry_snapshot_at TEXT,
                commence_time TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                league TEXT NOT NULL,
                market_key TEXT NOT NULL,
                selection TEXT NOT NULL,
                point REAL,
                line_key TEXT,
                bookmaker_key TEXT NOT NULL,
                offered_odds REAL NOT NULL,
                model_probability REAL NOT NULL,
                model_fair_odds REAL NOT NULL,
                edge_pct REAL NOT NULL,
                strong_candidate INTEGER NOT NULL DEFAULT 0,
                hours_to_kickoff REAL NOT NULL,
                odds_band TEXT NOT NULL,
                edge_band TEXT NOT NULL,
                bookmaker_count INTEGER NOT NULL DEFAULT 0,
                median_market_odds REAL,
                best_market_odds REAL,
                price_dispersion_pct REAL,
                chosen_vs_median_pct REAL,
                mean_overround_pct REAL,
                paired_model TEXT,
                paired_probability REAL,
                paired_probability_gap_pp REAL,
                agreement_band TEXT NOT NULL,
                paired_bet_exists INTEGER NOT NULL DEFAULT 0,
                same_top_outcome INTEGER,
                uncertainty_score REAL NOT NULL,
                uncertainty_band TEXT NOT NULL,
                expected_total_goals REAL,
                league_training_matches INTEGER NOT NULL DEFAULT 0,
                training_support_matches REAL NOT NULL DEFAULT 0,
                archetype TEXT NOT NULL,
                label_status TEXT NOT NULL DEFAULT 'PENDING',
                clv_pct REAL,
                clv_quality TEXT,
                beat_close INTEGER,
                closing_odds REAL,
                result TEXT,
                net_pnl_units REAL,
                labeled_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS meta_edge_model_runs (
                id {id_col},
                model_version TEXT NOT NULL,
                feature_version TEXT NOT NULL,
                status TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                frozen_at TEXT NOT NULL,
                clean_labels INTEGER NOT NULL,
                train_labels INTEGER NOT NULL,
                holdout_labels INTEGER NOT NULL,
                trained_through_sample_id INTEGER NOT NULL,
                trained_through_captured_at TEXT NOT NULL,
                holdout_start_at TEXT NOT NULL,
                holdout_sample_ids_json TEXT NOT NULL,
                model_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS meta_edge_model_scores (
                id {id_col},
                model_run_id INTEGER NOT NULL,
                sample_id INTEGER NOT NULL,
                phase TEXT NOT NULL,
                prob_beat_close REAL NOT NULL,
                expected_clv_pct REAL NOT NULL,
                trust_band TEXT NOT NULL,
                scored_at TEXT NOT NULL,
                UNIQUE(model_run_id,sample_id),
                FOREIGN KEY(model_run_id) REFERENCES meta_edge_model_runs(id),
                FOREIGN KEY(sample_id) REFERENCES meta_edge_samples(id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive_historical_validations (
                id {id_col},
                match_key TEXT NOT NULL UNIQUE,
                provider_event_id TEXT,
                sport_key TEXT NOT NULL,
                training_played_at TEXT NOT NULL,
                commence_time TEXT,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                home_goals INTEGER NOT NULL,
                away_goals INTEGER NOT NULL,
                actual_outcome TEXT,
                pred1_expected_home_goals REAL,
                pred1_expected_away_goals REAL,
                pred1_home_probability REAL,
                pred1_draw_probability REAL,
                pred1_away_probability REAL,
                pred1_brier_score REAL,
                pred1_log_loss REAL,
                pred1_config_hash TEXT,
                pred2_expected_home_goals REAL,
                pred2_expected_away_goals REAL,
                pred2_rho REAL,
                pred2_home_probability REAL,
                pred2_draw_probability REAL,
                pred2_away_probability REAL,
                pred2_brier_score REAL,
                pred2_log_loss REAL,
                pred2_config_hash TEXT,
                closing_home_probability REAL,
                closing_draw_probability REAL,
                closing_away_probability REAL,
                closing_brier_score REAL,
                closing_snapshot_at TEXT,
                closing_book_count INTEGER,
                status TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive_historical_odds (
                id {id_col},
                validation_id INTEGER NOT NULL,
                requested_offset_minutes INTEGER NOT NULL,
                requested_at TEXT NOT NULL,
                snapshot_at TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT,
                selection TEXT NOT NULL,
                price REAL NOT NULL,
                approved_execution INTEGER NOT NULL DEFAULT 0,
                UNIQUE(validation_id,requested_offset_minutes,bookmaker_key,selection),
                FOREIGN KEY(validation_id) REFERENCES football_predictive_historical_validations(id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS football_predictive_historical_bets (
                id {id_col},
                validation_id INTEGER NOT NULL,
                model TEXT NOT NULL,
                selection TEXT NOT NULL,
                entry_offset_minutes INTEGER NOT NULL,
                entry_snapshot_at TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT,
                offered_odds REAL NOT NULL,
                model_probability REAL NOT NULL,
                model_fair_odds REAL NOT NULL,
                min_odds REAL NOT NULL,
                edge_pct REAL NOT NULL,
                closing_odds REAL,
                clv_pct REAL,
                result TEXT,
                gross_pnl_units REAL,
                commission_rate_pct REAL,
                commission_units REAL,
                net_pnl_units REAL,
                UNIQUE(validation_id,model,selection),
                FOREIGN KEY(validation_id) REFERENCES football_predictive_historical_validations(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS research_reports (
                report_key TEXT PRIMARY KEY,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                report_type TEXT NOT NULL,
                canonical_bets INTEGER NOT NULL DEFAULT 0,
                settled_bets INTEGER NOT NULL DEFAULT 0,
                pnl_units REAL NOT NULL DEFAULT 0,
                roi_pct REAL,
                commission_units REAL NOT NULL DEFAULT 0,
                net_pnl_units REAL NOT NULL DEFAULT 0,
                net_roi_pct REAL,
                avg_clv_pct REAL,
                beat_close_pct REAL,
                sample_status TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_odds_event_time
            ON odds_snapshots(event_id, captured_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_signals_event
            ON signals(event_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_candidate_eval_event_time
            ON candidate_evaluations(event_id, evaluated_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_price_obs_signal_time
            ON signal_price_observations(signal_id, source_snapshot_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_canonical_event_time
            ON canonical_bets(event_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_execution_event_time
            ON execution_shadow_bets(event_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_execution_eval_event_time
            ON execution_evaluations(event_id, evaluated_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_execution_obs_bet_time
            ON execution_price_observations(execution_bet_id, source_snapshot_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_multiple_shadow_created
            ON multiple_shadow_bets(created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_multiple_shadow_status
            ON multiple_shadow_bets(status, settled_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_multiple_shadow_leg_parent
            ON multiple_shadow_legs(multiple_bet_id, leg_order)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_multiple_shadow_leg_event
            ON multiple_shadow_legs(event_id, execution_bet_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_manual_system_created
            ON manual_system_shadow_bets(created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_manual_system_status
            ON manual_system_shadow_bets(status, settled_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_manual_system_book_type
            ON manual_system_shadow_bets(bookmaker_key, system_type, kickoff_date)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_manual_system_leg_parent
            ON manual_system_shadow_legs(system_bet_id, leg_order)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_manual_system_leg_event
            ON manual_system_shadow_legs(event_id, market_key)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_manual_system_line_parent
            ON manual_system_shadow_lines(system_bet_id, line_order)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_tennis_events_start
            ON tennis_events(status, commence_time)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_tennis_odds_event_time
            ON tennis_odds_snapshots(event_id, captured_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_tennis_consensus_event_time
            ON tennis_consensus_snapshots(event_id, captured_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_tennis_exec_event_time
            ON tennis_execution_bets(event_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_tennis_eval_event_time
            ON tennis_execution_evaluations(event_id, evaluated_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_multisport_events_start
            ON multisport_events(status, commence_time)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_multisport_odds_event_time
            ON multisport_odds_snapshots(event_id, captured_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_multisport_consensus_event_time
            ON multisport_consensus_snapshots(event_id, captured_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_multisport_exec_event_time
            ON multisport_execution_bets(event_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_multisport_eval_event_time
            ON multisport_execution_evaluations(event_id, evaluated_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_multisport_line_odds_event_time
            ON multisport_line_odds_snapshots(event_id, captured_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_multisport_line_consensus_event
            ON multisport_line_consensus_snapshots(event_id, market_key, captured_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_multisport_line_bets_event
            ON multisport_line_bets(event_id, market_key, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_multisport_line_eval_event
            ON multisport_line_evaluations(event_id, market_key, evaluated_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_pred_training_league_date
            ON football_predictive_training_matches(sport_key, played_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_pred_predictions_start
            ON football_predictive_predictions(sport_key, commence_time)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_pred_eval_event_time
            ON football_predictive_evaluations(event_id, evaluated_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_pred_bets_event_time
            ON football_predictive_bets(event_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_pred_market_predictions_event
            ON football_predictive_market_predictions(event_id,market_key,point)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_pred_market_eval_event_time
            ON football_predictive_market_evaluations(event_id,market_key,evaluated_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_pred_market_bets_event_time
            ON football_predictive_market_bets(event_id,market_key,created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_meta_edge_source
            ON meta_edge_samples(source_model,market_key,captured_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_meta_edge_label
            ON meta_edge_samples(label_status,clv_quality)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_meta_edge_event
            ON meta_edge_samples(event_id,source_model)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_meta_model_active
            ON meta_edge_model_runs(active,frozen_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_meta_model_score_phase
            ON meta_edge_model_scores(model_run_id,phase,trust_band)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_meta_model_score_sample
            ON meta_edge_model_scores(sample_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_predictive_hist_validation_time
            ON football_predictive_historical_validations(sport_key,training_played_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_predictive_hist_odds_validation
            ON football_predictive_historical_odds(validation_id,requested_offset_minutes)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_predictive_hist_bets_model
            ON football_predictive_historical_bets(model,validation_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_research_reports_period
            ON research_reports(period_start, period_end)
            """,
        ]
        with self.connection() as conn:
            cur = conn.cursor()
            for stmt in ddl:
                cur.execute(self._sql(stmt))
            # Ensure singleton quota row exists.
            if self.is_postgres:
                cur.execute(
                    """
                    INSERT INTO quota_state(singleton_id, paid_polling_paused)
                    VALUES (1, 0)
                    ON CONFLICT (singleton_id) DO NOTHING
                    """
                )
            else:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO quota_state(singleton_id, paid_polling_paused)
                    VALUES (1, 0)
                    """
                )

            # v0.19.2 MS3 arm -> refresh -> confirm migration. Existing cards and
            # the original forward-test start timestamp are preserved.
            for column, definition in (
                ("armed_at", "TEXT"),
                ("confirmed_at", "TEXT"),
                ("rejected_at", "TEXT"),
                ("rejection_reason", "TEXT"),
            ):
                self._ensure_column(conn, "cohort_system_shadow_bets", column, definition)

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cohort_system_shadow_arm_status
                ON cohort_system_shadow_bets(status, armed_at, first_kickoff)
                """
            )

            # v0.6.8 Multiples Shadow starts forward-only on first deployment.
            if self.is_postgres:
                cur.execute(
                    """
                    INSERT INTO multiple_shadow_state(singleton_id, started_at, algorithm_version)
                    VALUES (1, %s, 'MS1')
                    ON CONFLICT (singleton_id) DO NOTHING
                    """,
                    (utc_now_iso(),),
                )
            else:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO multiple_shadow_state(singleton_id, started_at, algorithm_version)
                    VALUES (1, ?, 'MS1')
                    """,
                    (utc_now_iso(),),
                )

            # v0.6.6 additive measurement/accounting migration.
            for column, definition in (
                ("closing_observed_at", "TEXT"),
                ("closing_minutes_before_kickoff", "REAL"),
                ("clv_quality", "TEXT"),
                ("commission_rate_pct", "REAL"),
                ("commission_units", "REAL"),
                ("net_pnl_units", "REAL"),
            ):
                self._ensure_column(conn, "execution_shadow_bets", column, definition)

            for column, definition in (
                ("commission_units", "REAL NOT NULL DEFAULT 0"),
                ("net_pnl_units", "REAL NOT NULL DEFAULT 0"),
                ("net_roi_pct", "REAL"),
            ):
                self._ensure_column(conn, "research_reports", column, definition)

            # v0.6.10 collection hardening: non-destructive odds quarantine.
            for column, definition in (
                ("odds_quarantine_until", "TEXT"),
                ("odds_failure_count", "INTEGER NOT NULL DEFAULT 0"),
                ("odds_last_failure_code", "INTEGER"),
                ("odds_last_failure_at", "TEXT"),
            ):
                self._ensure_column(conn, "events", column, definition)

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_events_poll_priority
                ON events(status, commence_time, odds_quarantine_until)
                """
            )

            # v0.6.9 additive research instrumentation migration.
            for column, definition in (
                ("app_version", "TEXT"),
                ("experiment_version", "TEXT"),
                ("strategy_version", "TEXT"),
                ("config_hash", "TEXT"),
                ("entry_consensus_bookmaker_count", "INTEGER"),
                ("entry_consensus_median_odds", "REAL"),
                ("entry_consensus_best_odds", "REAL"),
                ("entry_price_dispersion_pct", "REAL"),
                ("entry_chosen_vs_median_pct", "REAL"),
                ("entry_mean_overround_pct", "REAL"),
                ("closing_consensus_median_odds", "REAL"),
                ("closing_reference_best_odds", "REAL"),
                ("closing_reference_observed_at", "TEXT"),
                ("closing_reference_minutes_before_kickoff", "REAL"),
                ("closing_reference_quality", "TEXT"),
                ("closing_reference_status", "TEXT"),
                ("clv_vs_consensus_pct", "REAL"),
                ("clv_vs_best_reference_pct", "REAL"),
            ):
                self._ensure_column(conn, "execution_shadow_bets", column, definition)

            for column, definition in (
                ("app_version", "TEXT"),
                ("experiment_version", "TEXT"),
                ("config_hash", "TEXT"),
                ("common_bookmaker_count", "INTEGER"),
                ("common_bookmakers_json", "TEXT"),
                ("entry_quote_time_spread_minutes", "REAL"),
                ("source_pool_size", "INTEGER"),
                ("automation_eligible", "INTEGER NOT NULL DEFAULT 0"),
                ("venue_policy", "TEXT"),
                ("allowed_api_bookmakers_json", "TEXT"),
                ("closing_consensus_combined_odds", "REAL"),
                ("closing_best_common_book_odds", "REAL"),
                ("closing_best_common_bookmaker_key", "TEXT"),
                ("closing_common_bookmaker_count", "INTEGER"),
                ("closing_reference_quality", "TEXT"),
                ("clv_vs_closing_consensus_pct", "REAL"),
                ("clv_vs_closing_best_common_pct", "REAL"),
            ):
                self._ensure_column(conn, "multiple_shadow_bets", column, definition)

            for column, definition in (
                ("settlement_quality", "TEXT"),
                ("settlement_provenance", "TEXT"),
            ):
                self._ensure_column(conn, "event_results", column, definition)

            # v0.8.1 Multi-Sport settlement mechanics hardening.
            for column, definition in (
                ("settlement_quality", "TEXT"),
                ("settlement_provenance", "TEXT"),
            ):
                self._ensure_column(
                    conn, "multisport_execution_bets", column, definition
                )

            # Normalize historical league display names from stable sport keys.
            league_titles = {
                "soccer_epl": "Premier League",
                "soccer_efl_champ": "Championship",
                "soccer_england_league1": "League One",
                "soccer_england_league2": "League Two",
                "soccer_spl": "Scottish Premiership",
                "soccer_spain_la_liga": "La Liga",
                "soccer_germany_bundesliga": "Bundesliga",
                "soccer_italy_serie_a": "Serie A",
                "soccer_france_ligue_one": "Ligue 1",
                "soccer_netherlands_eredivisie": "Eredivisie",
                "soccer_portugal_primeira_liga": "Primeira Liga",
                "soccer_germany_bundesliga2": "2. Bundesliga",
                "soccer_italy_serie_b": "Serie B",
                "soccer_spain_segunda_division": "Segunda División",
                "soccer_france_ligue_two": "Ligue 2",
                "soccer_belgium_first_div": "Belgian Pro League",
                "soccer_austria_bundesliga": "Austrian Bundesliga",
                "soccer_denmark_superliga": "Danish Superliga",
                "soccer_switzerland_superleague": "Swiss Super League",
                "soccer_germany_liga3": "3. Liga",
                "soccer_norway_eliteserien": "Eliteserien",
                "soccer_sweden_allsvenskan": "Allsvenskan",
                "soccer_sweden_superettan": "Superettan",
                "soccer_poland_ekstraklasa": "Ekstraklasa",
                "soccer_greece_super_league": "Greek Super League",
                "soccer_turkey_super_league": "Turkish Süper Lig",
                "soccer_finland_veikkausliiga": "Veikkausliiga",
                "soccer_league_of_ireland": "League of Ireland",
                "soccer_uefa_champs_league": "UEFA Champions League",
                "soccer_uefa_europa_league": "UEFA Europa League",
                "soccer_uefa_europa_conference_league": "UEFA Conference League",
                "soccer_uefa_nations_league": "UEFA Nations League",
            }
            for sport_key, league in league_titles.items():
                cur.execute(
                    self._sql("UPDATE events SET league=? WHERE sport_key=? AND league<>?"),
                    (league, sport_key, league),
                )

            # Remove credentials leaked by historical HTTP exception URLs.
            cur.execute(
                self._sql(
                    "SELECT id,detail FROM collector_runs WHERE detail IS NOT NULL "
                    "AND (detail LIKE ? OR detail LIKE ? OR detail LIKE ?)"
                ),
                ("%apiKey=%", "%api_key=%", "%ODDS_API_KEY%"),
            )
            for run_id, detail in cur.fetchall():
                clean = sanitize_sensitive_text(detail)
                if clean != str(detail):
                    cur.execute(
                        self._sql("UPDATE collector_runs SET detail=? WHERE id=?"),
                        (clean, run_id),
                    )

    def record_collector_run(
        self,
        run_type: str,
        ok: bool,
        *,
        event_id: Optional[str] = None,
        sport_key: Optional[str] = None,
        requested_markets: Optional[str] = None,
        estimated_cost: int = 0,
        actual_cost: int = 0,
        detail: str = "",
    ) -> None:
        now = utc_now_iso()
        self.execute(
            """
            INSERT INTO collector_runs(
                started_at, finished_at, run_type, event_id, sport_key,
                requested_markets, estimated_cost, actual_cost, ok, detail
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                now, now, run_type, event_id, sport_key, requested_markets,
                int(estimated_cost), int(actual_cost), 1 if ok else 0,
                sanitize_sensitive_text(detail)[:2000]
            ),
        )
