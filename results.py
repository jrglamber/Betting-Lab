from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional

from db import Database, utc_now_iso
from quota import QuotaGuard, provider_actual_cost
from closing import settle_signal


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def grade_signal(
    signal: Mapping[str, Any],
    *,
    home_team: str,
    away_team: str,
    home_score: int,
    away_score: int,
) -> str:
    market = signal["market_key"]
    selection = str(signal["selection"])
    total = int(home_score) + int(away_score)

    if market == "h2h":
        if home_score > away_score:
            winner = home_team
        elif away_score > home_score:
            winner = away_team
        else:
            winner = "Draw"
        return "WIN" if selection == winner else "LOSS"

    if market == "totals":
        point = signal.get("point")
        if point is None:
            return "VOID"
        point = float(point)
        if total == point:
            return "PUSH"
        if selection.lower() == "over":
            return "WIN" if total > point else "LOSS"
        if selection.lower() == "under":
            return "WIN" if total < point else "LOSS"
        return "VOID"

    if market == "btts":
        yes = home_score > 0 and away_score > 0
        if selection.lower() == "yes":
            return "WIN" if yes else "LOSS"
        if selection.lower() == "no":
            return "WIN" if not yes else "LOSS"
        return "VOID"

    if market == "draw_no_bet":
        if home_score == away_score:
            return "PUSH"
        winner = home_team if home_score > away_score else away_team
        return "WIN" if selection == winner else "LOSS"

    return "VOID"


def settle_event_signals(
    db: Database,
    event_id: str,
    *,
    home_score: int,
    away_score: int,
) -> Dict[str, int]:
    event = db.fetchone("SELECT * FROM events WHERE event_id=?", (event_id,))
    if not event:
        return {"settled": 0, "wins": 0, "losses": 0, "pushes": 0, "voids": 0}

    signals = db.fetchall(
        "SELECT * FROM signals WHERE event_id=? AND status='OPEN' ORDER BY id ASC",
        (event_id,),
    )
    counts = {"settled": 0, "wins": 0, "losses": 0, "pushes": 0, "voids": 0}
    for sig in signals:
        result = grade_signal(
            sig,
            home_team=event["home_team"],
            away_team=event["away_team"],
            home_score=int(home_score),
            away_score=int(away_score),
        )
        settle_signal(db, int(sig["id"]), result)
        counts["settled"] += 1
        counts[{
            "WIN": "wins",
            "LOSS": "losses",
            "PUSH": "pushes",
            "VOID": "voids",
        }[result]] += 1
    canonical = db.fetchall(
        "SELECT * FROM canonical_bets WHERE event_id=? AND status='OPEN' ORDER BY id ASC",
        (event_id,),
    )
    for bet in canonical:
        result = grade_signal(
            bet,
            home_team=event["home_team"],
            away_team=event["away_team"],
            home_score=int(home_score),
            away_score=int(away_score),
        )
        if result == "WIN":
            pnl = float(bet["offered_odds"]) - 1.0
        elif result == "LOSS":
            pnl = -1.0
        else:
            pnl = 0.0
        db.execute(
            """
            UPDATE canonical_bets
            SET result=?,pnl_units=?,status='SETTLED'
            WHERE id=?
            """,
            (result, pnl, bet["id"]),
        )

    from execution_shadow import settle_execution_event
    settle_execution_event(db,event_id,home_score=int(home_score),away_score=int(away_score))

    return counts


class ResultCollector:
    """Quota-aware football result collection and automatic shadow settlement."""

    def __init__(
        self,
        db: Database,
        api,
        quota: QuotaGuard,
        *,
        enabled: bool = True,
        min_minutes_after_kickoff: int = 135,
        min_poll_interval_seconds: int = 21600,
    ):
        self.db = db
        self.api = api
        self.quota = quota
        self.enabled = bool(enabled)
        self.min_minutes_after_kickoff = max(90, int(min_minutes_after_kickoff))
        self.min_poll_interval_seconds = max(3600, int(min_poll_interval_seconds))

    def _due_events(self, now: datetime) -> List[Dict[str, Any]]:
        cutoff = now - timedelta(minutes=self.min_minutes_after_kickoff)
        return self.db.fetchall(
            """
            SELECT DISTINCT e.*
            FROM events e
            LEFT JOIN event_results r ON r.event_id=e.event_id
            WHERE r.event_id IS NULL
              AND e.commence_time<=?
              AND (
                    EXISTS (
                        SELECT 1 FROM signals s
                        WHERE s.event_id=e.event_id AND s.status='OPEN'
                    )
                    OR EXISTS (
                        SELECT 1 FROM football_predictive_predictions p
                        WHERE p.event_id=e.event_id
                          AND p.actual_outcome IS NULL
                    )
                    OR EXISTS (
                        SELECT 1 FROM football_predictive2_predictions p2
                        WHERE p2.event_id=e.event_id
                          AND p2.actual_outcome IS NULL
                    )
                    OR EXISTS (
                        SELECT 1 FROM football_predictive3_predictions p3
                        WHERE p3.event_id=e.event_id
                          AND p3.actual_outcome IS NULL
                    )
                  )
            ORDER BY e.commence_time ASC
            """,
            (cutoff.isoformat(),),
        )

    def _last_result_poll_at(self, sport_key: str) -> Optional[datetime]:
        row = self.db.fetchone(
            """
            SELECT started_at FROM collector_runs
            WHERE run_type='RESULTS' AND sport_key=?
            ORDER BY id DESC LIMIT 1
            """,
            (sport_key,),
        )
        if not row:
            return None
        return parse_iso(row["started_at"])

    def _sport_due(self, sport_key: str, now: datetime) -> bool:
        last = self._last_result_poll_at(sport_key)
        if not last:
            return True
        return (now - last).total_seconds() >= self.min_poll_interval_seconds

    @staticmethod
    def _score_map(item: Mapping[str, Any]) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for row in item.get("scores") or []:
            try:
                out[str(row["name"])] = int(row["score"])
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def collect(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        if not self.enabled:
            return {"checked": 0, "settled": 0, "reason": "disabled"}

        now = now or datetime.now(timezone.utc)
        due = self._due_events(now)
        if not due:
            return {"checked": 0, "settled": 0, "reason": "no_due_results"}

        by_sport: Dict[str, List[Dict[str, Any]]] = {}
        for event in due:
            by_sport.setdefault(event["sport_key"], []).append(event)

        checked = settled = charged = 0
        for sport_key, events in by_sport.items():
            if not self._sport_due(sport_key, now):
                continue

            # Completed-score request with daysFrom=1 costs two provider credits.
            decision = self.quota.decide(2)
            if not decision.allowed:
                self.db.record_collector_run(
                    "RESULTS", False, sport_key=sport_key,
                    estimated_cost=2, actual_cost=0,
                    detail=f"quota_block:{decision.reason}",
                )
                continue

            actual_cost = 0
            try:
                result = self.api.scores(
                    sport_key,
                    event_ids=[e["event_id"] for e in events],
                    days_from=1,
                )
                actual_cost = provider_actual_cost(result.last_cost, 2)
                charged += actual_cost
                self.quota.update(
                    remaining=result.remaining,
                    used=result.used,
                    last_cost=result.last_cost,
                )
                payload = result.data if isinstance(result.data, list) else []
                wanted = {e["event_id"]: e for e in events}

                found = 0
                settled_here = 0
                for item in payload:
                    event_id = item.get("id")
                    if event_id not in wanted or not item.get("completed"):
                        continue

                    event = wanted[event_id]
                    scores = self._score_map(item)
                    if event["home_team"] not in scores or event["away_team"] not in scores:
                        continue

                    hs = int(scores[event["home_team"]])
                    aws = int(scores[event["away_team"]])
                    raw = json.dumps(item, separators=(",", ":"))
                    fetched = utc_now_iso()

                    self.db.execute(
                        """
                        INSERT INTO event_results(
                            event_id,fetched_at,completed_at,home_score,away_score,source,raw_json
                        ) VALUES(?,?,?,?,?,?,?)
                        ON CONFLICT(event_id) DO UPDATE SET
                            fetched_at=excluded.fetched_at,
                            completed_at=excluded.completed_at,
                            home_score=excluded.home_score,
                            away_score=excluded.away_score,
                            source=excluded.source,
                            raw_json=excluded.raw_json
                        """,
                        (
                            event_id, fetched, item.get("commence_time"),
                            hs, aws, "the_odds_api", raw,
                        ),
                    )
                    self.db.execute(
                        "UPDATE events SET status='COMPLETED' WHERE event_id=?",
                        (event_id,),
                    )
                    result_counts = settle_event_signals(
                        self.db, event_id, home_score=hs, away_score=aws
                    )
                    settled_here += result_counts["settled"]
                    found += 1

                checked += len(events)
                settled += settled_here
                self.db.record_collector_run(
                    "RESULTS", True, sport_key=sport_key,
                    requested_markets="scores",
                    estimated_cost=2, actual_cost=actual_cost,
                    detail=f"events_requested={len(events)}; completed_found={found}; signals_settled={settled_here}",
                )
            except Exception as exc:
                self.db.record_collector_run(
                    "RESULTS", False, sport_key=sport_key,
                    requested_markets="scores",
                    estimated_cost=2, actual_cost=actual_cost,
                    detail=str(exc),
                )

        return {
            "checked": checked,
            "settled": settled,
            "charged": charged,
            "reason": "ok",
        }
