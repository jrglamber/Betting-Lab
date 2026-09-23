from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from db import Database, sanitize_sensitive_text, utc_now_iso
from quota import QuotaGuard, provider_actual_cost
from the_odds_api import TheOddsApi


LEAGUE_TITLES = {
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



def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def minutes_to_start(commence_time: str, now: Optional[datetime] = None) -> float:
    now = now or datetime.now(timezone.utc)
    return (parse_iso(commence_time) - now).total_seconds() / 60.0


def desired_poll_interval_minutes(commence_time: str, now: Optional[datetime] = None) -> int:
    mins = minutes_to_start(commence_time, now)
    if mins <= 0:
        return 10**9
    if mins <= 90:
        return 45
    if mins <= 360:
        return 120
    if mins <= 1440:
        return 360
    return 1440




def predictive_convergence_interval_minutes(commence_time: str, now: Optional[datetime] = None) -> int:
    """High-resolution PRED1/PRED2 approved-venue path without changing entry rules."""
    mins = minutes_to_start(commence_time, now)
    if mins <= 0:
        return 10**9
    if mins <= 15:
        return 5
    if mins <= 30:
        return 10
    if mins <= 60:
        return 15
    if mins <= 180:
        return 30
    if mins <= 360:
        return 60
    if mins <= 720:
        return 120
    if mins <= 1440:
        return 180
    return 360


def predictive_broad_close_interval_minutes(commence_time: str, now: Optional[datetime] = None) -> int:
    """Broad h2h consensus capture near kickoff for market-Brier benchmarking."""
    mins = minutes_to_start(commence_time, now)
    if mins <= 0:
        return 10**9
    if mins <= 15:
        return 5
    if mins <= 30:
        return 10
    if mins <= 60:
        return 15
    return 30

def event_is_due(event: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if minutes_to_start(event["commence_time"], now) <= 0:
        return False
    quarantine_until = event.get("odds_quarantine_until")
    if quarantine_until:
        try:
            if parse_iso(str(quarantine_until)) > now:
                return False
        except Exception:
            pass
    last = event.get("last_odds_poll_at")
    if not last:
        return True
    elapsed = (now - parse_iso(last)).total_seconds() / 60.0
    return elapsed >= desired_poll_interval_minutes(event["commence_time"], now)


def _http_status(exc: Exception) -> Optional[int]:
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _proximity_bucket(commence_time: str, now: datetime) -> int:
    mins = max(0.0, minutes_to_start(commence_time, now))
    if mins <= 90:
        return 0
    if mins <= 360:
        return 1
    if mins <= 1440:
        return 2
    if mins <= 4320:
        return 3
    return 4


# v0.7.2 operational freshness guard.
# Inside the final 24h, a breadth event at >=2x its normal refresh interval
# is considered severely overdue and can jump the daily pacing queue.
BREADTH_URGENT_HORIZON_MINUTES = 1440.0
BREADTH_SEVERE_DEBT_RATIO = 2.0


def _freshness_debt(event: Dict[str, Any], now: datetime, *, last_field: str) -> float:
    last = event.get(last_field)
    if not last:
        return 10.0
    try:
        elapsed = max(0.0, (now - parse_iso(str(last))).total_seconds() / 60.0)
    except Exception:
        return 10.0
    interval = max(1.0, float(desired_poll_interval_minutes(event["commence_time"], now)))
    return elapsed / interval


def breadth_is_urgent(event: Dict[str, Any], now: datetime) -> bool:
    """Urgent breadth: always <=6h, or severely stale inside the final 24h."""
    mins = max(0.0, minutes_to_start(event["commence_time"], now))
    if mins <= 360.0:
        return True
    if mins > BREADTH_URGENT_HORIZON_MINUTES:
        return False
    return (
        _freshness_debt(event, now, last_field="last_odds_poll_at")
        >= BREADTH_SEVERE_DEBT_RATIO
    )


class Collector:
    """
    Betting Lab v0.6.3 collection model:

      BREADTH
        - all bookmakers from the configured region
        - all active discovery markets
        - used for fair-price / strategy discovery

      CONVERGENCE
        - approved automation-capable venues only
        - one relevant market per API call
        - used only to track executable shadow prices / CLV

    Strategy generation is run only after BREADTH snapshots.
    """

    def __init__(
        self,
        db: Database,
        api: TheOddsApi,
        quota: QuotaGuard,
        *,
        sport_keys: Iterable[str],
        region: str,
        markets: Iterable[str],
        max_events_per_cycle: int = 1,
        breadth_polls_per_day: int = 2,
        execution_bookmaker_keys: Iterable[str] = (),
        predictive_high_res_price_path_enabled: bool = True,
        predictive_broad_close_enabled: bool = True,
        predictive_broad_close_hours_before: float = 3.0,
    ):
        self.db = db
        self.api = api
        self.quota = quota
        self.sport_keys = tuple(sport_keys)
        self.region = region
        self.markets = tuple(markets)
        self.predictive_high_res_price_path_enabled = bool(predictive_high_res_price_path_enabled)
        self.predictive_broad_close_enabled = bool(predictive_broad_close_enabled)
        self.predictive_broad_close_hours_before = max(0.25, float(predictive_broad_close_hours_before))
        self.execution_bookmaker_keys = tuple(
            str(x) for x in execution_bookmaker_keys if str(x)
        )
        self.max_events_per_cycle = max(1, int(max_events_per_cycle))
        self.breadth_polls_per_day = max(0, int(breadth_polls_per_day))

    @property
    def broad_cost(self) -> int:
        return max(1, len(self.markets))

    @property
    def convergence_cost(self) -> int:
        # Every group of <=10 explicitly requested bookmakers is one region
        # equivalent. We request exactly one market per convergence call.
        book_groups = max(1, ceil(max(1, len(self.execution_bookmaker_keys)) / 10))
        return book_groups

    def refresh_quota(self) -> Dict[str, Any]:
        result = self.api.quota_probe()
        self.quota.update(
            remaining=result.remaining,
            used=result.used,
            last_cost=result.last_cost,
        )
        return self.quota.state()

    def discover(self) -> int:
        count = 0
        now = utc_now_iso()
        for sport_key in self.sport_keys:
            try:
                result = self.api.events(sport_key)
                self.quota.update(
                    remaining=result.remaining,
                    used=result.used,
                    last_cost=result.last_cost,
                )
                events = result.data if isinstance(result.data, list) else []
                for event in events:
                    self.db.execute(
                        """
                        INSERT INTO events(
                            event_id, sport_key, league, commence_time, home_team,
                            away_team, first_seen_at, last_seen_at, status
                        ) VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(event_id) DO UPDATE SET
                            sport_key=excluded.sport_key,
                            league=excluded.league,
                            commence_time=excluded.commence_time,
                            home_team=excluded.home_team,
                            away_team=excluded.away_team,
                            last_seen_at=excluded.last_seen_at
                        """,
                        (
                            event["id"],
                            sport_key,
                            LEAGUE_TITLES.get(sport_key, sport_key),
                            event["commence_time"],
                            event["home_team"],
                            event["away_team"],
                            now,
                            now,
                            "UPCOMING",
                        ),
                    )
                    count += 1
                self.db.record_collector_run(
                    "DISCOVERY", True, sport_key=sport_key,
                    actual_cost=provider_actual_cost(result.last_cost, 0),
                    detail=f"{len(events)} events"
                )
            except Exception as exc:
                self.db.record_collector_run(
                    "DISCOVERY", False, sport_key=sport_key, detail=str(exc)
                )
        return count

    def broad_polls_today(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        today = now.date().isoformat()
        row = self.db.fetchone(
            """
            SELECT COUNT(*) AS n
            FROM collector_runs
            WHERE run_type='ODDS' AND ok=1
              AND substr(started_at,1,10)=?
            """,
            (today,),
        )
        return int((row or {}).get("n") or 0)

    def breadth_target_by_now(self, now: Optional[datetime] = None) -> int:
        """Pace the configured daily breadth target across the UTC day."""
        now = now or datetime.now(timezone.utc)
        target = max(0, int(self.breadth_polls_per_day))
        if target <= 0:
            return 0
        seconds = now.hour * 3600 + now.minute * 60 + now.second
        fraction = min(1.0, max(0.0, seconds / 86400.0))
        # ceil means the first poll becomes due shortly after midnight and the
        # final target is reached near day-end rather than before breakfast.
        return min(target, int(ceil(target * fraction)))

    def _last_paid_mode(self) -> Optional[str]:
        row = self.db.fetchone(
            """
            SELECT run_type FROM collector_runs
            WHERE ok=1 AND run_type IN ('ODDS','ODDS_CONVERGENCE','PREDICTIVE_CLOSE_BROAD')
            ORDER BY id DESC LIMIT 1
            """
        )
        run_type = str((row or {}).get("run_type") or "")
        if run_type == "ODDS":
            return "breadth"
        if run_type == "ODDS_CONVERGENCE":
            return "convergence"
        if run_type == "PREDICTIVE_CLOSE_BROAD":
            return "predictive_close"
        return None

    def candidate_events(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT e.*
            FROM events e
            WHERE e.status='UPCOMING'
            ORDER BY e.commence_time ASC
            """
        )
        now = now or datetime.now(timezone.utc)
        due = [row for row in rows if event_is_due(row, now)]

        def key(row):
            mins = max(0.0, minutes_to_start(row["commence_time"], now))
            return (
                _proximity_bucket(row["commence_time"], now),
                -_freshness_debt(row, now, last_field="last_odds_poll_at"),
                mins,
            )

        return sorted(due, key=key)

    def _last_convergence_poll(
        self,
        event_id: str,
        market_key: str,
    ) -> Optional[str]:
        row = self.db.fetchone(
            """
            SELECT MAX(started_at) AS at
            FROM collector_runs
            WHERE run_type='ODDS_CONVERGENCE'
              AND event_id=? AND requested_markets=? AND ok=1
            """,
            (event_id, market_key),
        )
        return (row or {}).get("at")

    def convergence_candidates(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        if not self.execution_bookmaker_keys:
            return []

        rows = self.db.fetchall(
            """
            SELECT x.event_id,x.market_key,e.sport_key,e.commence_time,
                   e.odds_quarantine_until,
                   MAX(x.edge_pct) AS max_edge_pct,
                   COUNT(*) AS execution_bets,
                   0 AS predictive_priority
            FROM execution_shadow_bets x
            JOIN events e ON e.event_id=x.event_id
            WHERE x.status='OPEN' AND e.status='UPCOMING'
            GROUP BY x.event_id,x.market_key,e.sport_key,e.commence_time,e.odds_quarantine_until
            ORDER BY e.commence_time ASC
            """
        )
        # v0.11.0: predictive-football shadows also need fresh approved
        # venue quotes so their CLV path is measured to the same standard.
        predictive_rows = self.db.fetchall(
            """
            SELECT b.event_id,'h2h' AS market_key,e.sport_key,e.commence_time,
                   e.odds_quarantine_until,
                   MAX(b.edge_pct) AS max_edge_pct,
                   COUNT(*) AS execution_bets ,
                   1 AS predictive_priority
            FROM football_predictive_bets b
            JOIN events e ON e.event_id=b.event_id
            WHERE b.status='OPEN' AND e.status='UPCOMING'
            GROUP BY b.event_id,e.sport_key,e.commence_time,e.odds_quarantine_until
            """
        )
        predictive_market_rows = self.db.fetchall(
            """
            SELECT b.event_id,b.market_key,e.sport_key,e.commence_time,
                   e.odds_quarantine_until,
                   MAX(b.edge_pct) AS max_edge_pct,
                   COUNT(*) AS execution_bets ,
                   1 AS predictive_priority
            FROM football_predictive_market_bets b
            JOIN events e ON e.event_id=b.event_id
            WHERE b.status='OPEN' AND e.status='UPCOMING'
            GROUP BY b.event_id,b.market_key,e.sport_key,e.commence_time,
                     e.odds_quarantine_until
            """
        )

        # v0.11.5: PRED1 must receive approved-venue quote waves BEFORE a
        # shadow exists. Otherwise there is a circular dependency:
        #   no shadow -> no convergence polling -> no quote -> no shadow.
        # Frozen 1X2 forecasts and BTTS/totals cases are therefore themselves
        # convergence candidates until kickoff.
        predictive_forecast_rows = self.db.fetchall(
            """
            SELECT p.event_id,'h2h' AS market_key,e.sport_key,e.commence_time,
                   e.odds_quarantine_until,
                   0.0 AS max_edge_pct,
                   0 AS execution_bets,
                   1 AS predictive_priority
            FROM football_predictive_predictions p
            JOIN events e ON e.event_id=p.event_id
            WHERE e.status='UPCOMING'
            GROUP BY p.event_id,e.sport_key,e.commence_time,
                     e.odds_quarantine_until
            """
        )
        predictive_case_rows = self.db.fetchall(
            """
            SELECT mp.event_id,mp.market_key,e.sport_key,e.commence_time,
                   e.odds_quarantine_until,
                   0.0 AS max_edge_pct,
                   0 AS execution_bets,
                   1 AS predictive_priority
            FROM football_predictive_market_predictions mp
            JOIN events e ON e.event_id=mp.event_id
            WHERE e.status='UPCOMING'
            GROUP BY mp.event_id,mp.market_key,e.sport_key,e.commence_time,
                     e.odds_quarantine_until
            """
        )

        # v0.12.0 PRED2 reuses the same approved-venue waves. Because the
        # candidate set is deduplicated by (event, market), paired PRED1/PRED2
        # forecasts do not double the provider call for the same market.
        predictive2_rows = self.db.fetchall(
            """
            SELECT b.event_id,'h2h' AS market_key,e.sport_key,e.commence_time,
                   e.odds_quarantine_until,MAX(b.edge_pct) AS max_edge_pct,
                   COUNT(*) AS execution_bets ,
                   1 AS predictive_priority
            FROM football_predictive2_bets b
            JOIN events e ON e.event_id=b.event_id
            WHERE b.status='OPEN' AND e.status='UPCOMING'
            GROUP BY b.event_id,e.sport_key,e.commence_time,e.odds_quarantine_until
            """
        )
        predictive2_market_rows = self.db.fetchall(
            """
            SELECT b.event_id,b.market_key,e.sport_key,e.commence_time,
                   e.odds_quarantine_until,MAX(b.edge_pct) AS max_edge_pct,
                   COUNT(*) AS execution_bets ,
                   1 AS predictive_priority
            FROM football_predictive2_market_bets b
            JOIN events e ON e.event_id=b.event_id
            WHERE b.status='OPEN' AND e.status='UPCOMING'
            GROUP BY b.event_id,b.market_key,e.sport_key,e.commence_time,e.odds_quarantine_until
            """
        )
        predictive2_forecast_rows = self.db.fetchall(
            """
            SELECT p.event_id,'h2h' AS market_key,e.sport_key,e.commence_time,
                   e.odds_quarantine_until,0.0 AS max_edge_pct,0 AS execution_bets,
                   1 AS predictive_priority
            FROM football_predictive2_predictions p
            JOIN events e ON e.event_id=p.event_id
            WHERE e.status='UPCOMING'
            GROUP BY p.event_id,e.sport_key,e.commence_time,e.odds_quarantine_until
            """
        )
        predictive2_case_rows = self.db.fetchall(
            """
            SELECT mp.event_id,mp.market_key,e.sport_key,e.commence_time,
                   e.odds_quarantine_until,0.0 AS max_edge_pct,0 AS execution_bets,
                   1 AS predictive_priority
            FROM football_predictive2_market_predictions mp
            JOIN events e ON e.event_id=mp.event_id
            WHERE e.status='UPCOMING'
            GROUP BY mp.event_id,mp.market_key,e.sport_key,e.commence_time,e.odds_quarantine_until
            """
        )

        # v0.16.0 PRED3 StatsBomb xG challenger. Same stored approved-venue
        # waves are reused and deduplicated by (event, market), so PRED3 adds
        # no duplicate odds request when PRED1/PRED2 already need the quote.
        predictive3_rows = self.db.fetchall(
            """
            SELECT b.event_id,'h2h' AS market_key,e.sport_key,e.commence_time,
                   e.odds_quarantine_until,MAX(b.edge_pct) AS max_edge_pct,
                   COUNT(*) AS execution_bets,1 AS predictive_priority
            FROM football_predictive3_bets b
            JOIN events e ON e.event_id=b.event_id
            WHERE b.status='OPEN' AND e.status='UPCOMING'
            GROUP BY b.event_id,e.sport_key,e.commence_time,e.odds_quarantine_until
            """
        )
        predictive3_market_rows = self.db.fetchall(
            """
            SELECT b.event_id,b.market_key,e.sport_key,e.commence_time,
                   e.odds_quarantine_until,MAX(b.edge_pct) AS max_edge_pct,
                   COUNT(*) AS execution_bets,1 AS predictive_priority
            FROM football_predictive3_market_bets b
            JOIN events e ON e.event_id=b.event_id
            WHERE b.status='OPEN' AND e.status='UPCOMING'
            GROUP BY b.event_id,b.market_key,e.sport_key,e.commence_time,e.odds_quarantine_until
            """
        )
        predictive3_forecast_rows = self.db.fetchall(
            """
            SELECT p.event_id,'h2h' AS market_key,e.sport_key,e.commence_time,
                   e.odds_quarantine_until,0.0 AS max_edge_pct,0 AS execution_bets,
                   1 AS predictive_priority
            FROM football_predictive3_predictions p
            JOIN events e ON e.event_id=p.event_id
            WHERE e.status='UPCOMING'
            GROUP BY p.event_id,e.sport_key,e.commence_time,e.odds_quarantine_until
            """
        )
        predictive3_case_rows = self.db.fetchall(
            """
            SELECT mp.event_id,mp.market_key,e.sport_key,e.commence_time,
                   e.odds_quarantine_until,0.0 AS max_edge_pct,0 AS execution_bets,
                   1 AS predictive_priority
            FROM football_predictive3_market_predictions mp
            JOIN events e ON e.event_id=mp.event_id
            WHERE e.status='UPCOMING'
            GROUP BY mp.event_id,mp.market_key,e.sport_key,e.commence_time,e.odds_quarantine_until
            """
        )

        # v0.19.2: an MS3 card first arms from the recent stored candidate pool.
        # Its exact event/market legs then receive one priority approved-venue
        # convergence refresh after the arm timestamp. This is a real provider
        # confirmation wave, not a stale-price relaxation. Rows are deduplicated
        # below with the normal convergence universe.
        try:
            ms3_armed_rows = self.db.fetchall(
                """
                SELECT l.event_id,l.market_key,e.sport_key,e.commence_time,
                       e.odds_quarantine_until,0.0 AS max_edge_pct,0 AS execution_bets,
                       1 AS predictive_priority,1 AS ms3_priority,
                       MIN(COALESCE(b.armed_at,b.created_at)) AS ms3_armed_at
                FROM cohort_system_shadow_bets b
                JOIN cohort_system_shadow_legs l ON l.system_bet_id=b.id
                JOIN events e ON e.event_id=l.event_id
                WHERE b.status='ARMED' AND e.status='UPCOMING'
                GROUP BY l.event_id,l.market_key,e.sport_key,e.commence_time,e.odds_quarantine_until
                """
            )
        except Exception:
            ms3_armed_rows = []

        merged: Dict[Tuple[str,str], Dict[str,Any]] = {}
        for source in (
            list(rows)+list(predictive_rows)+list(predictive_market_rows)
            +list(predictive_forecast_rows)+list(predictive_case_rows)
            +list(predictive2_rows)+list(predictive2_market_rows)
            +list(predictive2_forecast_rows)+list(predictive2_case_rows)
            +list(predictive3_rows)+list(predictive3_market_rows)
            +list(predictive3_forecast_rows)+list(predictive3_case_rows)
            +list(ms3_armed_rows)
        ):
            row=dict(source)
            row.setdefault("predictive_priority", 0)
            row.setdefault("ms3_priority", 0)
            row.setdefault("ms3_armed_at", None)
            key=(str(row["event_id"]),str(row["market_key"]))
            if key not in merged:
                merged[key]=row
            else:
                merged[key]["max_edge_pct"]=max(
                    float(merged[key].get("max_edge_pct") or 0.0),
                    float(row.get("max_edge_pct") or 0.0),
                )
                merged[key]["execution_bets"]=(
                    int(merged[key].get("execution_bets") or 0)
                    + int(row.get("execution_bets") or 0)
                )
                merged[key]["predictive_priority"] = max(
                    int(merged[key].get("predictive_priority") or 0),
                    int(row.get("predictive_priority") or 0),
                )
                merged[key]["ms3_priority"] = max(
                    int(merged[key].get("ms3_priority") or 0),
                    int(row.get("ms3_priority") or 0),
                )
                if row.get("ms3_armed_at"):
                    existing = merged[key].get("ms3_armed_at")
                    if not existing or str(row["ms3_armed_at"]) < str(existing):
                        merged[key]["ms3_armed_at"] = row["ms3_armed_at"]
        rows=list(merged.values())

        now = now or datetime.now(timezone.utc)
        due = []
        for row in rows:
            mins = minutes_to_start(row["commence_time"], now)
            if mins <= 0:
                continue
            quarantine_until = row.get("odds_quarantine_until")
            if quarantine_until:
                try:
                    if parse_iso(str(quarantine_until)) > now:
                        continue
                except Exception:
                    pass
            last = self._last_convergence_poll(row["event_id"], row["market_key"])
            is_predictive = bool(int(row.get("predictive_priority") or 0))
            is_ms3 = bool(int(row.get("ms3_priority") or 0))
            if is_ms3:
                # Exactly one successful refresh after this arm is required.
                # Once that market has been refreshed, let the other armed legs
                # consume the limited convergence slots rather than re-polling it.
                armed_at = row.get("ms3_armed_at")
                if last and armed_at:
                    try:
                        if parse_iso(str(last)) > parse_iso(str(armed_at)):
                            continue
                    except Exception:
                        pass
                interval = 0.0
            else:
                interval = (
                    predictive_convergence_interval_minutes(row["commence_time"], now)
                    if getattr(self,"predictive_high_res_price_path_enabled",True) and is_predictive
                    else desired_poll_interval_minutes(row["commence_time"], now)
                )
                if last:
                    elapsed = (now - parse_iso(last)).total_seconds() / 60.0
                    if elapsed < interval:
                        continue
            row = dict(row)
            row["last_convergence_poll_at"] = last
            row["desired_interval_minutes"] = interval
            due.append(row)

        # Imminent bets outrank far-future never-observed bets. Within each
        # proximity bucket, refresh the most overdue price path first.
        return sorted(
            due,
            key=lambda x: (
                0 if int(x.get("ms3_priority") or 0) else 1,
                _proximity_bucket(x["commence_time"], now),
                -(
                    10.0 if not x.get("last_convergence_poll_at") else
                    max(0.0,(now-parse_iso(str(x["last_convergence_poll_at"]))).total_seconds()/60.0)
                    / max(1.0,float(x.get("desired_interval_minutes") or 1.0))
                ),
                max(0.0, minutes_to_start(x["commence_time"], now)),
                -float(x.get("max_edge_pct") or 0.0),
            ),
        )

    def _last_predictive_close_poll(self, event_id: str) -> Optional[str]:
        row = self.db.fetchone(
            """SELECT MAX(started_at) AS at FROM collector_runs
               WHERE run_type='PREDICTIVE_CLOSE_BROAD' AND event_id=? AND ok=1""",
            (event_id,),
        )
        return (row or {}).get("at")

    def predictive_close_candidates(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        if not getattr(self,"predictive_broad_close_enabled",True):
            return []
        now = now or datetime.now(timezone.utc)
        horizon = now + timedelta(hours=float(getattr(self,"predictive_broad_close_hours_before",3.0)))
        rows = self.db.fetchall(
            """
            SELECT DISTINCT e.event_id,e.sport_key,e.commence_time,e.odds_quarantine_until
            FROM events e
            WHERE e.status='UPCOMING' AND e.commence_time>? AND e.commence_time<=?
              AND (
                EXISTS(SELECT 1 FROM football_predictive_predictions p WHERE p.event_id=e.event_id)
                OR EXISTS(SELECT 1 FROM football_predictive2_predictions p2 WHERE p2.event_id=e.event_id)
                OR EXISTS(SELECT 1 FROM football_predictive3_predictions p3 WHERE p3.event_id=e.event_id)
              )
            ORDER BY e.commence_time,e.event_id
            """,
            (now.isoformat(),horizon.isoformat()),
        )
        due=[]
        for row0 in rows:
            row=dict(row0)
            quarantine_until=row.get("odds_quarantine_until")
            if quarantine_until:
                try:
                    if parse_iso(str(quarantine_until))>now:
                        continue
                except Exception:
                    pass
            last=self._last_predictive_close_poll(str(row["event_id"]))
            interval=predictive_broad_close_interval_minutes(str(row["commence_time"]),now)
            if last:
                elapsed=(now-parse_iso(str(last))).total_seconds()/60.0
                if elapsed < interval:
                    continue
            row["last_predictive_close_poll_at"]=last
            row["desired_interval_minutes"]=interval
            due.append(row)
        return due

    def _poll_predictive_close(self, targets: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        polled=inserted=0
        event_ids: List[str]=[]
        blocked_reason: Optional[str]=None
        for target in targets[: self.max_events_per_cycle]:
            decision=self.quota.decide(1)
            if not decision.allowed:
                blocked_reason=decision.reason
                self.quota.set_paused(True,decision.reason)
                break
            actual_cost=0
            try:
                result=self.api.event_odds(
                    target["sport_key"],target["event_id"],self.region,("h2h",),
                )
                actual_cost=provider_actual_cost(result.last_cost,1)
                self.quota.update(remaining=result.remaining,used=result.used,last_cost=result.last_cost)
                payload=result.data if isinstance(result.data,dict) else {}
                added=self._insert_odds_payload(target["event_id"],payload)
                inserted += added
                self._clear_event_odds_failure(target["event_id"])
                self.db.record_collector_run(
                    "PREDICTIVE_CLOSE_BROAD",True,event_id=target["event_id"],
                    sport_key=target["sport_key"],requested_markets="h2h",
                    estimated_cost=1,actual_cost=actual_cost,
                    detail=f"mode=predictive_close_broad; quote_rows={added}",
                )
                polled += 1; event_ids.append(str(target["event_id"]))
            except Exception as exc:
                status_code=_http_status(exc); quarantine=None
                if status_code in {404,410}:
                    quarantine=self._quarantine_event(target["event_id"],status_code)
                detail=f"mode=predictive_close_broad; status={status_code}; error={sanitize_sensitive_text(exc)}"
                if quarantine:
                    detail += f"; quarantined_until={quarantine}"
                self.db.record_collector_run(
                    "PREDICTIVE_CLOSE_BROAD",False,event_id=target["event_id"],
                    sport_key=target["sport_key"],requested_markets="h2h",
                    estimated_cost=1,actual_cost=actual_cost,detail=detail,
                )
        if polled:
            self.quota.set_paused(False,"")
        return {
            "polled":polled,"inserted":inserted,
            "reason":blocked_reason or ("ok" if polled else "no_due_predictive_close"),
            "mode":"predictive_close_broad","event_ids":event_ids,
            "requested_markets":["h2h"],
        }

    def _clear_event_odds_failure(self, event_id: str) -> None:
        self.db.execute(
            """
            UPDATE events
            SET odds_quarantine_until=NULL,odds_failure_count=0,
                odds_last_failure_code=NULL,odds_last_failure_at=NULL
            WHERE event_id=?
            """,
            (event_id,),
        )

    def _quarantine_event(self, event_id: str, status_code: int) -> str:
        row = self.db.fetchone(
            "SELECT odds_failure_count FROM events WHERE event_id=?", (event_id,)
        ) or {}
        failures = int(row.get("odds_failure_count") or 0) + 1
        # A disappeared/invalid event should not steal one of a small number of
        # collection slots every worker tick. Escalate 2h -> 6h -> 12h -> 24h.
        hours = (2, 6, 12, 24)[min(failures - 1, 3)]
        now = datetime.now(timezone.utc)
        until = now + timedelta(hours=hours)
        self.db.execute(
            """
            UPDATE events
            SET odds_quarantine_until=?,odds_failure_count=?,
                odds_last_failure_code=?,odds_last_failure_at=?
            WHERE event_id=?
            """,
            (until.isoformat(), failures, int(status_code), now.isoformat(), event_id),
        )
        return until.isoformat()

    def _insert_odds_payload(self, event_id: str, payload: Dict[str, Any]) -> int:
        captured_at = utc_now_iso()
        rows = []
        for book in payload.get("bookmakers", []) or []:
            for market in book.get("markets", []) or []:
                for outcome in market.get("outcomes", []) or []:
                    price = outcome.get("price")
                    try:
                        price = float(price)
                    except (TypeError, ValueError):
                        continue
                    if price <= 1.0:
                        continue
                    rows.append(
                        (
                            event_id,
                            captured_at,
                            book.get("key", ""),
                            book.get("title", book.get("key", "")),
                            book.get("last_update"),
                            market.get("key", ""),
                            outcome.get("name", ""),
                            outcome.get("description"),
                            outcome.get("point"),
                            price,
                        )
                    )
        if rows:
            self.db.executemany(
                """
                INSERT INTO odds_snapshots(
                    event_id, captured_at, bookmaker_key, bookmaker_title,
                    bookmaker_last_update, market_key, outcome_name,
                    outcome_description, point, price
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
        return len(rows)

    def _poll_breadth(self, events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        polled = inserted = 0
        event_ids: List[str] = []
        blocked_reason: Optional[str] = None

        for event in events[: self.max_events_per_cycle]:
            # Re-check before EVERY paid call so a multi-event worker cycle
            # cannot overshoot the daily budget.
            decision = self.quota.decide(self.broad_cost)
            if not decision.allowed:
                blocked_reason = decision.reason
                self.quota.set_paused(True, decision.reason)
                break

            actual_cost = 0
            try:
                result = self.api.event_odds(
                    event["sport_key"],
                    event["event_id"],
                    self.region,
                    self.markets,
                )
                actual_cost = provider_actual_cost(result.last_cost, self.broad_cost)
                self.quota.update(
                    remaining=result.remaining,
                    used=result.used,
                    last_cost=result.last_cost,
                )
                payload = result.data if isinstance(result.data, dict) else {}
                added = self._insert_odds_payload(event["event_id"], payload)
                inserted += added
                self.db.execute(
                    "UPDATE events SET last_odds_poll_at=? WHERE event_id=?",
                    (utc_now_iso(), event["event_id"]),
                )
                self._clear_event_odds_failure(event["event_id"])
                self.db.record_collector_run(
                    "ODDS", True,
                    event_id=event["event_id"],
                    sport_key=event["sport_key"],
                    requested_markets=",".join(self.markets),
                    estimated_cost=self.broad_cost,
                    actual_cost=actual_cost,
                    detail=f"mode=breadth; quote_rows={added}"
                )
                polled += 1
                event_ids.append(event["event_id"])
            except Exception as exc:
                status_code = _http_status(exc)
                quarantine = None
                if status_code in {404, 410}:
                    quarantine = self._quarantine_event(event["event_id"], status_code)
                clean_error = sanitize_sensitive_text(exc)
                detail = f"mode=breadth; status={status_code}; error={clean_error}"
                if quarantine:
                    detail += f"; quarantined_until={quarantine}"
                self.db.record_collector_run(
                    "ODDS", False,
                    event_id=event["event_id"],
                    sport_key=event["sport_key"],
                    requested_markets=",".join(self.markets),
                    estimated_cost=self.broad_cost,
                    actual_cost=actual_cost,
                    detail=detail,
                )

        if polled:
            self.quota.set_paused(False, "")
        return {
            "polled": polled,
            "inserted": inserted,
            "reason": blocked_reason or ("ok" if polled else "no_due_events"),
            "mode": "breadth",
            "event_ids": event_ids,
            "requested_markets": list(self.markets),
        }

    def _poll_convergence(self, targets: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        polled = inserted = 0
        event_ids: List[str] = []
        blocked_reason: Optional[str] = None

        for target in targets[: self.max_events_per_cycle]:
            market = str(target["market_key"])
            decision = self.quota.decide(self.convergence_cost)
            if not decision.allowed:
                blocked_reason = decision.reason
                self.quota.set_paused(True, decision.reason)
                break

            actual_cost = 0
            try:
                result = self.api.event_odds(
                    target["sport_key"],
                    target["event_id"],
                    self.region,
                    (market,),
                    bookmaker_keys=self.execution_bookmaker_keys,
                )
                actual_cost = provider_actual_cost(result.last_cost, self.convergence_cost)
                self.quota.update(
                    remaining=result.remaining,
                    used=result.used,
                    last_cost=result.last_cost,
                )
                payload = result.data if isinstance(result.data, dict) else {}
                added = self._insert_odds_payload(target["event_id"], payload)
                inserted += added
                self._clear_event_odds_failure(target["event_id"])
                self.db.record_collector_run(
                    "ODDS_CONVERGENCE", True,
                    event_id=target["event_id"],
                    sport_key=target["sport_key"],
                    requested_markets=market,
                    estimated_cost=self.convergence_cost,
                    actual_cost=actual_cost,
                    detail=(
                        "mode=convergence; "
                        f"bookmakers={','.join(self.execution_bookmaker_keys)}; "
                        f"quote_rows={added}; ms3_priority={int(target.get('ms3_priority') or 0)}"
                    ),
                )
                polled += 1
                event_ids.append(target["event_id"])
            except Exception as exc:
                status_code = _http_status(exc)
                quarantine = None
                if status_code in {404, 410}:
                    quarantine = self._quarantine_event(target["event_id"], status_code)
                clean_error = sanitize_sensitive_text(exc)
                detail = f"mode=convergence; status={status_code}; error={clean_error}"
                if quarantine:
                    detail += f"; quarantined_until={quarantine}"
                self.db.record_collector_run(
                    "ODDS_CONVERGENCE", False,
                    event_id=target["event_id"],
                    sport_key=target["sport_key"],
                    requested_markets=market,
                    estimated_cost=self.convergence_cost,
                    actual_cost=actual_cost,
                    detail=detail,
                )

        if polled:
            self.quota.set_paused(False, "")
        return {
            "polled": polled,
            "inserted": inserted,
            "reason": blocked_reason or ("ok" if polled else "no_due_convergence"),
            "mode": "convergence",
            "event_ids": event_ids,
            "requested_markets": sorted({str(x["market_key"]) for x in targets[: self.max_events_per_cycle]}),
        }

    def poll_one_cycle(self) -> Dict[str, Any]:
        try:
            self.refresh_quota()
        except Exception:
            # Persisted quota state remains authoritative if the free probe fails.
            pass

        now = datetime.now(timezone.utc)
        broad = self.candidate_events(now) if self.breadth_polls_per_day > 0 else []
        convergence = self.convergence_candidates(now)
        predictive_close = self.predictive_close_candidates(now)

        broad_count = self.broad_polls_today(now)
        broad_target_now = self.breadth_target_by_now(now)
        broad_behind_pace = broad_count < broad_target_now
        broad_critical = bool(
            broad and breadth_is_urgent(broad[0], now)
        )
        convergence_critical = bool(
            convergence and minutes_to_start(convergence[0]["commence_time"], now) <= 360
        )
        last_mode = self._last_paid_mode()

        # v0.19.2: an armed MS3 card is waiting for a real post-arm quote wave.
        # Give those exact event/market refreshes first priority until each has
        # one successful convergence poll. With the default three-event cycle,
        # a six-leg Heinz is normally refreshed in two worker ticks.
        ms3_confirmation_due = bool(
            convergence and int(convergence[0].get("ms3_priority") or 0)
        )
        if ms3_confirmation_due:
            return self._poll_convergence(convergence)

        # v0.13.0: inside the final hour, alternate the two predictive evidence
        # lanes so we capture both executable-venue CLV and a broad consensus
        # benchmark close. This is measurement only; model forecasts/thresholds
        # are unchanged.
        predictive_close_critical = bool(
            predictive_close and minutes_to_start(predictive_close[0]["commence_time"], now) <= 60
        )
        if convergence_critical and predictive_close_critical:
            if last_mode == "predictive_close":
                return self._poll_convergence(convergence)
            return self._poll_predictive_close(predictive_close)
        if predictive_close_critical:
            return self._poll_predictive_close(predictive_close)

        # v0.7.2 scheduler:
        # - pace the configured breadth target across the whole UTC day;
        # - always prioritise <=6h breadth;
        # - also treat severely overdue breadth inside 24h as urgent;
        # - when breadth and convergence are both urgent, alternate so neither
        #   starves the other.
        if broad and convergence:
            if broad_critical and convergence_critical:
                if last_mode == "breadth":
                    return self._poll_convergence(convergence)
                return self._poll_breadth(broad)
            if broad_critical:
                return self._poll_breadth(broad)
            # Inside the configured broad-close window, alternate close-consensus
            # and approved-venue convergence when both are due. Normal breadth
            # still wins when genuinely urgent.
            if predictive_close:
                if last_mode == "convergence":
                    return self._poll_predictive_close(predictive_close)
                if last_mode == "predictive_close":
                    return self._poll_convergence(convergence)
            if convergence_critical:
                return self._poll_convergence(convergence)
            if broad_behind_pace:
                return self._poll_breadth(broad)
            return self._poll_convergence(convergence)

        if convergence and predictive_close:
            if last_mode == "convergence":
                return self._poll_predictive_close(predictive_close)
            return self._poll_convergence(convergence)

        if convergence:
            return self._poll_convergence(convergence)

        if predictive_close:
            return self._poll_predictive_close(predictive_close)

        if broad:
            if broad_critical or broad_behind_pace:
                return self._poll_breadth(broad)
            return {
                "polled": 0,
                "inserted": 0,
                "reason": "breadth_paced_until_later",
                "mode": "idle",
                "event_ids": [],
                "requested_markets": [],
                "breadth_polls_today": broad_count,
                "breadth_target_by_now": broad_target_now,
            }

        return {
            "polled": 0,
            "inserted": 0,
            "reason": "no_due_events",
            "mode": "idle",
            "event_ids": [],
            "requested_markets": [],
            "breadth_polls_today": broad_count,
            "breadth_target_by_now": broad_target_now,
        }

