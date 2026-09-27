from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from db import Database, utc_now_iso


def provider_actual_cost(last_cost, fallback_cost: int) -> int:
    """Preserve a genuine provider-reported zero cost."""
    if last_cost is None:
        return max(0, int(fallback_cost))
    return max(0, int(last_cost))


@dataclass
class QuotaDecision:
    allowed: bool
    reason: str
    remaining: Optional[int]
    estimated_cost: int


class QuotaGuard:
    def __init__(self, db: Database, reserve: int = 50, daily_budget: int = 12):
        self.db = db
        self.reserve = int(reserve)
        self.daily_budget = int(daily_budget)

    def update(
        self,
        *,
        remaining: Optional[int],
        used: Optional[int],
        last_cost: Optional[int],
    ) -> None:
        self.db.execute(
            """
            UPDATE quota_state
            SET credits_remaining=?, credits_used=?, last_cost=?,
                last_checked_at=?
            WHERE singleton_id=1
            """,
            (remaining, used, last_cost, utc_now_iso()),
        )

    def state(self):
        return self.db.fetchone(
            "SELECT * FROM quota_state WHERE singleton_id=1"
        ) or {}

    def today_paid_cost(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        row = self.db.fetchone(
            """
            SELECT COALESCE(SUM(actual_cost),0) AS total
            FROM collector_runs
            WHERE run_type IN ('ODDS','ODDS_CONVERGENCE','PREDICTIVE_CLOSE_BROAD','MANUAL_SYSTEM_QUOTES','RESULTS')
              AND actual_cost>0
              AND substr(started_at,1,10)=?
            """,
            (today,),
        )
        return int((row or {}).get("total") or 0)

    def decide(self, estimated_cost: int) -> QuotaDecision:
        estimated_cost = max(1, int(estimated_cost))
        state = self.state()
        remaining = state.get("credits_remaining")

        if self.today_paid_cost() + estimated_cost > self.daily_budget:
            return QuotaDecision(
                False, "daily_paid_credit_budget", remaining, estimated_cost
            )

        if remaining is not None and remaining - estimated_cost < self.reserve:
            return QuotaDecision(
                False, "protected_quota_reserve", int(remaining), estimated_cost
            )

        return QuotaDecision(True, "ok", remaining, estimated_cost)

    def set_paused(self, paused: bool, reason: str = "") -> None:
        self.db.execute(
            """
            UPDATE quota_state
            SET paid_polling_paused=?, pause_reason=?
            WHERE singleton_id=1
            """,
            (1 if paused else 0, reason or None),
        )
