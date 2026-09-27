from __future__ import annotations

from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from db import Database, sanitize_sensitive_text, utc_now_iso
from fair_value import consensus_probabilities, clv_pct
from execution_shadow import commission_adjusted_pnl
from quota import provider_actual_cost
from predictive_football import (
    PredictiveFootballEngine,
    _normalize_text,
)
from predictive_football_pred2 import PredictiveFootballPred2Engine


HISTORICAL_EXPERIMENT_VERSION = "PRED_HISTORICAL_FROZEN_V1"


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _actual_outcome(home: str, away: str, hg: int, ag: int) -> str:
    if hg > ag:
        return home
    if ag > hg:
        return away
    return "Draw"


def _brier(probs: Mapping[str, float], outcome: str) -> float:
    return sum((float(p) - (1.0 if k == outcome else 0.0)) ** 2 for k, p in probs.items())


def _logloss(probs: Mapping[str, float], outcome: str) -> float:
    return -math.log(max(1e-12, float(probs[outcome])))


def _fair(probability: float) -> float:
    return 1.0 / max(1e-12, float(probability))


def _edge(probability: float, odds: float) -> float:
    return (float(probability) * float(odds) - 1.0) * 100.0


def _min_odds(probability: float, edge_pct: float) -> float:
    return (1.0 + float(edge_pct) / 100.0) / max(1e-12, float(probability))


def _match_score(target: str, candidate: str) -> float:
    a = _normalize_text(target)
    b = _normalize_text(candidate)
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _find_provider_event(
    target_home: str,
    target_away: str,
    events: Sequence[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    best = None
    best_score = -1.0
    for event in events:
        hs = _match_score(target_home, str(event.get("home_team") or ""))
        aw = _match_score(target_away, str(event.get("away_team") or ""))
        swapped_h = _match_score(target_home, str(event.get("away_team") or ""))
        swapped_a = _match_score(target_away, str(event.get("home_team") or ""))
        direct = (hs + aw) / 2.0
        swapped = (swapped_h + swapped_a) / 2.0
        # Never silently flip home/away. A high swapped score is evidence that
        # this is the wrong provider event for the historical score row.
        if direct < 0.78 or direct <= swapped:
            continue
        if direct > best_score:
            best = event
            best_score = direct
    return best


def _flatten_h2h(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for book in payload.get("bookmakers") or []:
        book_key = str(book.get("key") or "")
        if not book_key:
            continue
        title = str(book.get("title") or book_key)
        for market in book.get("markets") or []:
            if str(market.get("key") or "") != "h2h":
                continue
            for outcome in market.get("outcomes") or []:
                try:
                    price = float(outcome.get("price"))
                except Exception:
                    continue
                selection = str(outcome.get("name") or "")
                if not selection or price <= 1.0:
                    continue
                rows.append({
                    "bookmaker_key": book_key,
                    "bookmaker_title": title,
                    "selection": selection,
                    "price": price,
                })
    return rows


def _three_way_books(rows: Sequence[Mapping[str, Any]], required: Sequence[str]) -> Dict[str, Dict[str, Mapping[str, Any]]]:
    req = set(str(x) for x in required)
    grouped: Dict[str, Dict[str, Mapping[str, Any]]] = {}
    for row in rows:
        book = str(row.get("bookmaker_key") or "")
        sel = str(row.get("selection") or "")
        if sel not in req:
            continue
        grouped.setdefault(book, {})[sel] = row
    return {b: sels for b, sels in grouped.items() if set(sels) == req}


class PredictiveHistoricalValidator:
    """Frozen retrospective corroboration lane for PRED1 and PRED2.

    This lane is deliberately separate from the forward tables. It reconstructs
    each selected past fixture using only score rows strictly before that
    fixture's pre-match information cutoff, then requests historical h2h prices
    at fixed checkpoints. It never mutates PRED1/PRED2 live predictions.
    """

    def __init__(
        self,
        db: Database,
        api,
        settings,
        pred1: PredictiveFootballEngine,
        pred2: PredictiveFootballPred2Engine,
        *,
        execution_bookmaker_keys: Sequence[str],
    ):
        self.db = db
        self.api = api
        self.settings = settings
        self.pred1 = pred1
        self.pred2 = pred2
        self.execution_books = tuple(str(x) for x in execution_bookmaker_keys if str(x))

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "predictive_football_historical_enabled", False))

    def _last_cycle_at(self) -> Optional[datetime]:
        row = self.db.fetchone(
            """SELECT MAX(started_at) AS at FROM collector_runs
               WHERE run_type='PREDICTIVE_HISTORICAL_FIXTURE'"""
        )
        raw = (row or {}).get("at")
        if not raw:
            return None
        try:
            return parse_iso(str(raw))
        except Exception:
            return None

    def _today_cost(self) -> int:
        day = datetime.now(timezone.utc).date().isoformat()
        row = self.db.fetchone(
            """SELECT COALESCE(SUM(actual_cost),0) AS n
               FROM collector_runs
               WHERE run_type IN ('PREDICTIVE_HISTORICAL_EVENTS','PREDICTIVE_HISTORICAL_ODDS')
                 AND actual_cost>0 AND substr(started_at,1,10)=?""",
            (day,),
        )
        return int((row or {}).get("n") or 0)

    def today_paid_cost(self) -> int:
        """Public status helper for the historical lane's isolated daily spend."""
        return self._today_cost()

    def _budget_allows(self, estimated: int) -> Tuple[bool, str]:
        budget = max(0, int(getattr(self.settings, "predictive_football_historical_daily_credit_budget", 0)))
        if budget <= 0:
            return False, "historical_daily_budget_disabled"
        if self._today_cost() + int(estimated) > budget:
            return False, "historical_daily_credit_budget"
        state = self.db.fetchone("SELECT * FROM quota_state WHERE singleton_id=1") or {}
        remaining = state.get("credits_remaining")
        reserve = int(getattr(self.settings, "quota_reserve_credits", 0))
        if remaining is not None and int(remaining) - int(estimated) < reserve:
            return False, "protected_quota_reserve"
        return True, "ok"

    def _update_quota(self, result) -> None:
        self.db.execute(
            """UPDATE quota_state
               SET credits_remaining=?,credits_used=?,last_cost=?,last_checked_at=?
               WHERE singleton_id=1""",
            (result.remaining, result.used, result.last_cost, utc_now_iso()),
        )

    def _record_api(self, run_type: str, ok: bool, *, sport_key: str, event_id: Optional[str], estimated: int, actual: int, detail: str) -> None:
        self.db.record_collector_run(
            run_type, ok, event_id=event_id, sport_key=sport_key,
            requested_markets="h2h" if run_type.endswith("ODDS") else None,
            estimated_cost=estimated, actual_cost=actual,
            detail=detail,
        )

    def _candidate_rows(self) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc) - timedelta(days=2)
        limit = max(20, int(getattr(self.settings, "predictive_football_historical_max_candidates_scan", 120)))
        return self.db.fetchall(
            """
            SELECT t.*
            FROM football_predictive_training_matches t
            LEFT JOIN football_predictive_historical_validations v ON v.match_key=t.match_key
            WHERE v.id IS NULL AND t.played_at<?
            ORDER BY t.played_at,t.id
            LIMIT ?
            """,
            (now.isoformat(), limit),
        )

    def _precheck_fit(self, row: Mapping[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], str]:
        played = parse_iso(str(row["played_at"]))
        # football-data dates are date-only and imported at 00:00. Use a neutral
        # afternoon kickoff for the zero-credit fit precheck; the real provider
        # kickoff is used again after historical event discovery.
        guessed = played if any((played.hour, played.minute, played.second)) else played + timedelta(hours=15)
        event = {
            "sport_key": row["sport_key"],
            "commence_time": guessed.isoformat(),
            "home_team": row["home_team"],
            "away_team": row["away_team"],
        }
        freeze_at = guessed - timedelta(hours=float(getattr(self.settings, "predictive_football_forecast_hours_before", 24)))
        p1, r1 = self.pred1.fit_event(event, now=freeze_at, training_cutoff=freeze_at)
        p2, r2 = self.pred2.fit_event(event, now=freeze_at, training_cutoff=freeze_at)
        if not p1 or not p2:
            return p1, p2, f"PRED1={r1};PRED2={r2}"
        return p1, p2, "OK"

    def _discover_event(self, row: Mapping[str, Any]) -> Tuple[Optional[Mapping[str, Any]], str]:
        played = parse_iso(str(row["played_at"]))
        day = played.date()
        starts = [
            datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc) - timedelta(days=1),
            datetime(day.year, day.month, day.day, 0, tzinfo=timezone.utc),
        ]
        from_dt = datetime(day.year, day.month, day.day, 0, tzinfo=timezone.utc)
        to_dt = from_dt + timedelta(days=2)
        last_reason = "NO_PROVIDER_EVENT"
        for snapshot in starts:
            allowed, reason = self._budget_allows(1)
            if not allowed:
                return None, reason
            actual = 0
            try:
                result = self.api.historical_events(
                    str(row["sport_key"]), snapshot.isoformat(),
                    commence_time_from=from_dt.isoformat(),
                    commence_time_to=to_dt.isoformat(),
                )
                actual = provider_actual_cost(result.last_cost, 1)
                self._update_quota(result)
                wrapper = result.data if isinstance(result.data, dict) else {}
                events = wrapper.get("data") if isinstance(wrapper, dict) else []
                if not isinstance(events, list):
                    events = []
                # Historical-events no longer accepts commenceTimeFrom/To.
                # Recreate that filter locally before fuzzy team matching so a
                # same-team fixture from a different date cannot be selected.
                window_events = []
                for candidate in events:
                    try:
                        commence = parse_iso(str(candidate.get("commence_time") or ""))
                    except Exception:
                        continue
                    if from_dt <= commence < to_dt:
                        window_events.append(candidate)
                found = _find_provider_event(
                    str(row["home_team"]), str(row["away_team"]), window_events
                )
                self._record_api(
                    "PREDICTIVE_HISTORICAL_EVENTS", True,
                    sport_key=str(row["sport_key"]), event_id=(str(found.get("id")) if found else None),
                    estimated=1, actual=actual,
                    detail=(
                        f"snapshot={snapshot.isoformat()}; events={len(events)}; "
                        f"window_events={len(window_events)}; matched={bool(found)}"
                    ),
                )
                if found:
                    return found, "OK"
                last_reason = "NO_PROVIDER_EVENT"
            except Exception as exc:
                self._record_api(
                    "PREDICTIVE_HISTORICAL_EVENTS", False,
                    sport_key=str(row["sport_key"]), event_id=None,
                    estimated=1, actual=actual,
                    detail=f"snapshot={snapshot.isoformat()}; error={sanitize_sensitive_text(exc)}",
                )
                last_reason = f"EVENT_DISCOVERY_ERROR:{sanitize_sensitive_text(exc)}"
        return None, last_reason

    def _insert_validation(
        self,
        row: Mapping[str, Any],
        event: Mapping[str, Any],
        p1: Mapping[str, Any],
        p2: Mapping[str, Any],
    ) -> int:
        home = str(event["home_team"])
        away = str(event["away_team"])
        hg = int(row["home_goals"]); ag = int(row["away_goals"])
        outcome = _actual_outcome(home, away, hg, ag)
        p1_probs = {home: float(p1["home_probability"]), "Draw": float(p1["draw_probability"]), away: float(p1["away_probability"])}
        p2_probs = {home: float(p2["home_probability"]), "Draw": float(p2["draw_probability"]), away: float(p2["away_probability"])}
        now = utc_now_iso()
        self.db.execute(
            """INSERT INTO football_predictive_historical_validations(
                match_key,provider_event_id,sport_key,training_played_at,commence_time,
                home_team,away_team,home_goals,away_goals,actual_outcome,
                pred1_expected_home_goals,pred1_expected_away_goals,
                pred1_home_probability,pred1_draw_probability,pred1_away_probability,
                pred1_brier_score,pred1_log_loss,pred1_config_hash,
                pred2_expected_home_goals,pred2_expected_away_goals,pred2_rho,
                pred2_home_probability,pred2_draw_probability,pred2_away_probability,
                pred2_brier_score,pred2_log_loss,pred2_config_hash,
                status,reason,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["match_key"], event.get("id"), row["sport_key"], row["played_at"], event["commence_time"],
                home, away, hg, ag, outcome,
                p1["expected_home_goals"], p1["expected_away_goals"],
                p1_probs[home], p1_probs["Draw"], p1_probs[away],
                _brier(p1_probs, outcome), _logloss(p1_probs, outcome), self.pred1.config_hash,
                p2["expected_home_goals"], p2["expected_away_goals"], p2.get("dixon_coles_rho"),
                p2_probs[home], p2_probs["Draw"], p2_probs[away],
                _brier(p2_probs, outcome), _logloss(p2_probs, outcome), self.pred2.config_hash,
                "COLLECTING", HISTORICAL_EXPERIMENT_VERSION, now, now,
            ),
        )
        created = self.db.fetchone(
            "SELECT id FROM football_predictive_historical_validations WHERE match_key=?",
            (row["match_key"],),
        )
        return int(created["id"])

    def _store_snapshot(self, validation_id: int, offset: int, requested_at: datetime, wrapper: Mapping[str, Any]) -> int:
        payload = wrapper.get("data") if isinstance(wrapper, dict) else None
        if not isinstance(payload, dict):
            return 0
        snapshot_at = str(wrapper.get("timestamp") or requested_at.isoformat())
        approved = set(self.execution_books)
        written = 0
        for q in _flatten_h2h(payload):
            exists = self.db.fetchone(
                """SELECT id FROM football_predictive_historical_odds
                   WHERE validation_id=? AND requested_offset_minutes=?
                     AND bookmaker_key=? AND selection=?""",
                (validation_id, int(offset), q["bookmaker_key"], q["selection"]),
            )
            if exists:
                continue
            self.db.execute(
                """INSERT INTO football_predictive_historical_odds(
                    validation_id,requested_offset_minutes,requested_at,snapshot_at,
                    bookmaker_key,bookmaker_title,selection,price,approved_execution
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    validation_id, int(offset), requested_at.isoformat(), snapshot_at,
                    q["bookmaker_key"], q["bookmaker_title"], q["selection"], q["price"],
                    1 if q["bookmaker_key"] in approved else 0,
                ),
            )
            written += 1
        return written

    def _collect_odds(self, validation_id: int, sport_key: str, provider_event_id: str, kickoff: datetime) -> Tuple[int, int]:
        calls = rows = 0
        offsets = sorted({int(x) for x in getattr(self.settings, "predictive_football_historical_snapshot_minutes", (1440, 360, 60, 5)) if int(x) > 0}, reverse=True)
        region = str(getattr(self.settings, "predictive_football_historical_region", "uk"))
        for offset in offsets:
            exists = self.db.fetchone(
                """SELECT id FROM football_predictive_historical_odds
                   WHERE validation_id=? AND requested_offset_minutes=? LIMIT 1""",
                (validation_id, offset),
            )
            if exists:
                continue
            allowed, reason = self._budget_allows(10)
            if not allowed:
                raise RuntimeError(reason)
            requested_at = kickoff - timedelta(minutes=offset)
            actual = 0
            try:
                result = self.api.historical_event_odds(
                    sport_key, provider_event_id, region, ("h2h",), requested_at.isoformat()
                )
                actual = provider_actual_cost(result.last_cost, 10)
                self._update_quota(result)
                wrapper = result.data if isinstance(result.data, dict) else {}
                added = self._store_snapshot(validation_id, offset, requested_at, wrapper)
                self._record_api(
                    "PREDICTIVE_HISTORICAL_ODDS", True, sport_key=sport_key,
                    event_id=provider_event_id, estimated=10, actual=actual,
                    detail=f"validation_id={validation_id}; offset_minutes={offset}; rows={added}",
                )
                calls += 1; rows += added
            except Exception as exc:
                self._record_api(
                    "PREDICTIVE_HISTORICAL_ODDS", False, sport_key=sport_key,
                    event_id=provider_event_id, estimated=10, actual=actual,
                    detail=f"validation_id={validation_id}; offset_minutes={offset}; error={sanitize_sensitive_text(exc)}",
                )
        return calls, rows

    def _snapshot_rows(self, validation_id: int, offset: int) -> List[Dict[str, Any]]:
        return self.db.fetchall(
            """SELECT * FROM football_predictive_historical_odds
               WHERE validation_id=? AND requested_offset_minutes=?
               ORDER BY bookmaker_key,selection""",
            (validation_id, int(offset)),
        )

    def _finalize(self, validation_id: int) -> Dict[str, Any]:
        v = self.db.fetchone("SELECT * FROM football_predictive_historical_validations WHERE id=?", (validation_id,))
        if not v:
            return {"complete": False}
        offsets = sorted({int(x) for x in getattr(self.settings, "predictive_football_historical_snapshot_minutes", (1440, 360, 60, 5)) if int(x) > 0}, reverse=True)
        available = [x for x in offsets if self._snapshot_rows(validation_id, x)]
        if not available:
            self.db.execute(
                "UPDATE football_predictive_historical_validations SET status='PARTIAL',reason=?,updated_at=? WHERE id=?",
                ("NO_HISTORICAL_ODDS", utc_now_iso(), validation_id),
            )
            return {"complete": False}

        # Do not silently substitute an earlier checkpoint for the configured close.
        # A 60m/24h snapshot can still be useful entry-path evidence, but it must not
        # be labelled the closing market if the intended near-kickoff checkpoint
        # (5m by default) was unavailable.
        required_close_offset = min(offsets)
        if required_close_offset not in available:
            self.db.execute(
                "UPDATE football_predictive_historical_validations SET status='PARTIAL',reason=?,updated_at=? WHERE id=?",
                (f"MISSING_CLOSE_CHECKPOINT_{required_close_offset}M", utc_now_iso(), validation_id),
            )
            return {
                "complete": False,
                "reason": f"missing_close_checkpoint_{required_close_offset}m",
                "available_offsets": available,
                "bets_created": 0,
            }

        close_offset = required_close_offset
        close_rows = self._snapshot_rows(validation_id, close_offset)
        required = [v["home_team"], "Draw", v["away_team"]]
        close_books = _three_way_books(close_rows, required)
        book_prices = {
            book: {sel: float(row["price"]) for sel, row in sels.items()}
            for book, sels in close_books.items()
        }
        consensus = consensus_probabilities(
            book_prices,
            min_books=int(getattr(self.settings, "min_consensus_books", 3)),
        )
        close_brier = None
        close_at = None
        if consensus and all(sel in consensus for sel in required):
            outcome = str(v["actual_outcome"])
            close_brier = _brier(consensus, outcome)
            close_at = str(close_rows[0]["snapshot_at"])
            self.db.execute(
                """UPDATE football_predictive_historical_validations
                   SET closing_home_probability=?,closing_draw_probability=?,closing_away_probability=?,
                       closing_brier_score=?,closing_snapshot_at=?,closing_book_count=?,
                       status='COMPLETE',reason=?,updated_at=? WHERE id=?""",
                (
                    consensus[v["home_team"]], consensus["Draw"], consensus[v["away_team"]],
                    close_brier, close_at, len(book_prices), HISTORICAL_EXPERIMENT_VERSION,
                    utc_now_iso(), validation_id,
                ),
            )
        else:
            self.db.execute(
                """UPDATE football_predictive_historical_validations
                   SET status='PARTIAL',reason=?,closing_snapshot_at=?,closing_book_count=?,updated_at=? WHERE id=?""",
                ("INSUFFICIENT_CLOSE_CONSENSUS", str(close_rows[0]["snapshot_at"]), len(book_prices), utc_now_iso(), validation_id),
            )

        # Approximate historical executable shadows at the fixed checkpoints.
        # They are explicitly labelled sampled-checkpoint evidence, not exact
        # first-acceptable historical execution.
        min_edge = float(getattr(self.settings, "predictive_football_min_edge_pct", 3.0))
        approved = set(self.execution_books)
        model_probs = {
            "PRED1": {
                v["home_team"]: float(v["pred1_home_probability"]),
                "Draw": float(v["pred1_draw_probability"]),
                v["away_team"]: float(v["pred1_away_probability"]),
            },
            "PRED2": {
                v["home_team"]: float(v["pred2_home_probability"]),
                "Draw": float(v["pred2_draw_probability"]),
                v["away_team"]: float(v["pred2_away_probability"]),
            },
        }
        close_by_book = _three_way_books(close_rows, required)
        created_bets = 0
        for model, probs in model_probs.items():
            for selection, prob in probs.items():
                exists = self.db.fetchone(
                    """SELECT id FROM football_predictive_historical_bets
                       WHERE validation_id=? AND model=? AND selection=?""",
                    (validation_id, model, selection),
                )
                if exists:
                    continue
                accepted = None
                for offset in offsets:  # 24h -> 6h -> 1h -> 5m
                    rows = self._snapshot_rows(validation_id, offset)
                    valid = _three_way_books(rows, required)
                    eligible = []
                    for book, sels in valid.items():
                        if book not in approved or selection not in sels:
                            continue
                        q = sels[selection]
                        odds = float(q["price"])
                        if odds >= _min_odds(prob, min_edge):
                            eligible.append(q)
                    if eligible:
                        accepted = (offset, max(eligible, key=lambda r: float(r["price"])))
                        break
                if not accepted:
                    continue
                offset, q = accepted
                offered = float(q["price"])
                close_q = (close_by_book.get(str(q["bookmaker_key"])) or {}).get(selection)
                closing = float(close_q["price"]) if close_q else None
                clv = clv_pct(offered, closing) if closing and closing > 1.0 else None
                result = "WIN" if selection == v["actual_outcome"] else "LOSS"
                gross = offered - 1.0 if result == "WIN" else -1.0
                rate, commission, net = commission_adjusted_pnl(gross, str(q["bookmaker_key"]))
                self.db.execute(
                    """INSERT INTO football_predictive_historical_bets(
                        validation_id,model,selection,entry_offset_minutes,entry_snapshot_at,
                        bookmaker_key,bookmaker_title,offered_odds,model_probability,
                        model_fair_odds,min_odds,edge_pct,closing_odds,clv_pct,result,
                        gross_pnl_units,commission_rate_pct,commission_units,net_pnl_units
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        validation_id, model, selection, int(offset), str(q["snapshot_at"]),
                        q["bookmaker_key"], q.get("bookmaker_title"), offered, prob,
                        _fair(prob), _min_odds(prob, min_edge), _edge(prob, offered),
                        closing, clv, result, gross, rate, commission, net,
                    ),
                )
                created_bets += 1
        return {"complete": bool(consensus), "close_offset_minutes": close_offset, "bets_created": created_bets}

    def process_one(self) -> Dict[str, Any]:
        candidates = self._candidate_rows()
        if not candidates:
            return {"processed": 0, "reason": "no_eligible_training_matches"}

        # Find the first model-eligible historical fixture without spending API
        # credits on rows that the frozen models could not have forecast.
        target = None
        for row in candidates:
            p1, p2, reason = self._precheck_fit(row)
            if p1 and p2:
                target = row
                break
            # Permanently mark only model-ineligible rows. This is zero-cost and
            # prevents the scan from getting stuck on early-season fixtures.
            now = utc_now_iso()
            self.db.execute(
                """INSERT INTO football_predictive_historical_validations(
                    match_key,sport_key,training_played_at,home_team,away_team,
                    home_goals,away_goals,status,reason,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["match_key"], row["sport_key"], row["played_at"], row["home_team"], row["away_team"],
                    int(row["home_goals"]), int(row["away_goals"]), "SKIPPED_MODEL", reason, now, now,
                ),
            )
        if target is None:
            return {"processed": 0, "reason": "no_model_eligible_candidate_in_scan"}

        estimated_total = 2 + 10 * len(tuple(getattr(self.settings, "predictive_football_historical_snapshot_minutes", (1440, 360, 60, 5))))
        allowed, reason = self._budget_allows(estimated_total)
        if not allowed:
            return {"processed": 0, "reason": reason}

        event, reason = self._discover_event(target)
        if not event:
            # Pace transient provider failures too: otherwise an API failure could
            # be retried on every worker tick instead of respecting the historical
            # lane interval. Genuine no-match rows are still permanently skipped.
            self.db.record_collector_run(
                "PREDICTIVE_HISTORICAL_FIXTURE", False,
                sport_key=str(target["sport_key"]),
                detail=f"event_discovery_failed:{sanitize_sensitive_text(reason)}",
            )
            # Do not permanently poison the target on a provider/API error. A
            # genuine no-match is recorded as a skip; transient errors retry.
            if reason == "NO_PROVIDER_EVENT":
                now = utc_now_iso()
                self.db.execute(
                    """INSERT INTO football_predictive_historical_validations(
                        match_key,sport_key,training_played_at,home_team,away_team,
                        home_goals,away_goals,status,reason,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        target["match_key"], target["sport_key"], target["played_at"], target["home_team"], target["away_team"],
                        int(target["home_goals"]), int(target["away_goals"]), "SKIPPED_NO_PROVIDER_EVENT", reason, now, now,
                    ),
                )
            return {"processed": 0, "reason": reason}

        kickoff = parse_iso(str(event["commence_time"]))
        freeze_at = kickoff - timedelta(hours=float(getattr(self.settings, "predictive_football_forecast_hours_before", 24)))
        model_event = {
            "sport_key": target["sport_key"], "commence_time": kickoff.isoformat(),
            "home_team": event["home_team"], "away_team": event["away_team"],
        }
        p1, r1 = self.pred1.fit_event(model_event, now=freeze_at, training_cutoff=freeze_at)
        p2, r2 = self.pred2.fit_event(model_event, now=freeze_at, training_cutoff=freeze_at)
        if not p1 or not p2:
            return {"processed": 0, "reason": f"post_discovery_fit_failed:PRED1={r1};PRED2={r2}"}

        validation_id = self._insert_validation(target, event, p1, p2)
        calls, odds_rows = self._collect_odds(
            validation_id, str(target["sport_key"]), str(event["id"]), kickoff
        )
        final = self._finalize(validation_id)
        self.db.record_collector_run(
            "PREDICTIVE_HISTORICAL_FIXTURE", True,
            event_id=str(event["id"]), sport_key=str(target["sport_key"]),
            detail=(
                f"validation_id={validation_id}; calls={calls}; odds_rows={odds_rows}; "
                f"complete={final.get('complete')}; sampled_bets={final.get('bets_created',0)}"
            ),
        )
        return {
            "processed": 1, "validation_id": validation_id,
            "odds_calls": calls, "odds_rows": odds_rows, **final,
        }

    def one_cycle(self, *, force: bool = False) -> Dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "processed": 0}
        if not force:
            last = self._last_cycle_at()
            interval = max(60, int(getattr(self.settings, "predictive_football_historical_interval_seconds", 900)))
            if last and (datetime.now(timezone.utc) - last).total_seconds() < interval:
                return {"enabled": True, "processed": 0, "reason": "interval_not_due"}
        try:
            out = self.process_one()
            out["enabled"] = True
            return out
        except Exception as exc:
            self.db.record_collector_run(
                "PREDICTIVE_HISTORICAL_FIXTURE", False,
                detail=sanitize_sensitive_text(exc),
            )
            return {"enabled": True, "processed": 0, "reason": sanitize_sensitive_text(exc)}


def historical_validation_summary(db: Database) -> Dict[str, Any]:
    rows = db.fetchall(
        """SELECT * FROM football_predictive_historical_validations
           WHERE status IN ('COMPLETE','PARTIAL') ORDER BY training_played_at,id"""
    )
    complete = [r for r in rows if r.get("status") == "COMPLETE"]
    def avg(field: str, source: Sequence[Mapping[str, Any]]) -> Optional[float]:
        vals = [float(r[field]) for r in source if r.get(field) is not None]
        return sum(vals) / len(vals) if vals else None

    p1_wins = p2_wins = ties = 0
    for r in complete:
        a = r.get("pred1_brier_score"); b = r.get("pred2_brier_score")
        if a is None or b is None:
            continue
        if float(a) < float(b) - 1e-12:
            p1_wins += 1
        elif float(b) < float(a) - 1e-12:
            p2_wins += 1
        else:
            ties += 1

    bets = db.fetchall("SELECT * FROM football_predictive_historical_bets ORDER BY id")
    bet_summary: Dict[str, Any] = {}
    for model in ("PRED1", "PRED2"):
        subset = [b for b in bets if str(b.get("model")) == model]
        net = sum(float(b.get("net_pnl_units") or 0.0) for b in subset)
        clvs = [float(b["clv_pct"]) for b in subset if b.get("clv_pct") is not None]
        bet_summary[model] = {
            "sampled_bets": len(subset),
            "net_pnl_units": net,
            "net_roi_pct": (net / len(subset) * 100.0) if subset else None,
            "avg_clv_pct": (sum(clvs) / len(clvs)) if clvs else None,
            "clv_samples": len(clvs),
        }

    skipped = db.fetchall(
        """SELECT status,COUNT(*) AS n FROM football_predictive_historical_validations
           WHERE status LIKE ? GROUP BY status ORDER BY status""",
        ("SKIPPED_%",),
    )
    return {
        "experiment_version": HISTORICAL_EXPERIMENT_VERSION,
        "complete_fixtures": len(complete),
        "partial_fixtures": len(rows) - len(complete),
        "pred1_avg_brier": avg("pred1_brier_score", complete),
        "pred2_avg_brier": avg("pred2_brier_score", complete),
        "closing_market_avg_brier": avg("closing_brier_score", complete),
        "pred1_fixture_wins": p1_wins,
        "pred2_fixture_wins": p2_wins,
        "ties": ties,
        "sampled_execution": bet_summary,
        "skipped": skipped,
    }
