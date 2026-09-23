from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from db import Database, sanitize_sensitive_text, utc_now_iso
from execution_shadow import (
    clv_quality, commission_adjusted_pnl, is_headline_clv_quality,
)
from fair_value import clv_pct, devig_prices, edge_pct, fair_odds, min_odds_for_probability
from quota import provider_actual_cost


APP_VERSION = "0.7.0"
EXPERIMENT_VERSION = "TS1"
GRAND_SLAM_TOKENS = (
    "aus_open_singles", "french_open", "us_open", "wimbledon",
)


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _tour(sport_key: str) -> str:
    key = str(sport_key)
    if key.startswith("tennis_atp_"):
        return "ATP"
    if key.startswith("tennis_wta_"):
        return "WTA"
    return "TENNIS"


def _level(sport_key: str) -> str:
    key = str(sport_key).lower()
    return "GRAND_SLAM" if any(x in key for x in GRAND_SLAM_TOKENS) else "TOUR_EVENT"


def _safe_hash(settings, execution_bookmaker_keys: Sequence[str]) -> str:
    payload = {
        "experiment": EXPERIMENT_VERSION,
        "market": str(settings.tennis_market),
        "min_consensus_books": int(settings.tennis_min_consensus_books),
        "min_edge_pct": float(settings.tennis_min_edge_pct),
        "max_consensus_age_minutes": int(settings.tennis_max_consensus_age_minutes),
        "execution_bookmaker_keys": list(execution_bookmaker_keys),
        "region": str(settings.odds_region),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


class TennisQuotaGuard:
    """Separate daily tennis lane sharing only the provider's real remaining balance."""

    RUN_TYPES = ("TENNIS_ODDS", "TENNIS_CONVERGENCE", "TENNIS_RESULTS")

    def __init__(self, db: Database, *, daily_budget: int, reserve: int):
        self.db = db
        self.daily_budget = max(0, int(daily_budget))
        self.reserve = max(0, int(reserve))

    def update(self, *, remaining, used, last_cost) -> None:
        self.db.execute(
            """
            UPDATE quota_state
            SET credits_remaining=?,credits_used=?,last_cost=?,last_checked_at=?
            WHERE singleton_id=1
            """,
            (remaining, used, last_cost, utc_now_iso()),
        )

    def today_paid_cost(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        row = self.db.fetchone(
            """
            SELECT COALESCE(SUM(actual_cost),0) AS total
            FROM collector_runs
            WHERE run_type IN ('TENNIS_ODDS','TENNIS_CONVERGENCE','TENNIS_RESULTS')
              AND actual_cost>0 AND substr(started_at,1,10)=?
            """,
            (now.date().isoformat(),),
        )
        return int((row or {}).get("total") or 0)

    def decide(self, estimated_cost: int) -> Tuple[bool, str]:
        estimated_cost = max(1, int(estimated_cost))
        if self.today_paid_cost() + estimated_cost > self.daily_budget:
            return False, "tennis_daily_paid_credit_budget"
        state = self.db.fetchone(
            "SELECT credits_remaining FROM quota_state WHERE singleton_id=1"
        ) or {}
        remaining = state.get("credits_remaining")
        if remaining is not None and int(remaining) - estimated_cost < self.reserve:
            return False, "protected_provider_reserve"
        return True, "ok"


def _market_metrics(
    book_prices: Mapping[str, Mapping[str, float]],
    players: Sequence[str],
) -> Optional[Dict[str, Any]]:
    players = tuple(str(x) for x in players)
    per_player_probs: Dict[str, List[float]] = {p: [] for p in players}
    per_player_odds: Dict[str, List[float]] = {p: [] for p in players}
    overrounds: List[float] = []
    used = 0

    for book, prices in book_prices.items():
        if not all(p in prices and float(prices[p]) > 1.0 for p in players):
            continue
        exact = {p: float(prices[p]) for p in players}
        try:
            probs = devig_prices(exact)
        except Exception:
            continue
        used += 1
        raw_overround = sum(1.0 / exact[p] for p in players) - 1.0
        overrounds.append(raw_overround * 100.0)
        for p in players:
            per_player_probs[p].append(float(probs[p]))
            per_player_odds[p].append(float(exact[p]))

    if not used:
        return None

    consensus = {p: mean(per_player_probs[p]) for p in players}
    total = sum(consensus.values())
    if total <= 0:
        return None
    consensus = {p: consensus[p] / total for p in players}

    metrics: Dict[str, Any] = {
        "num_books": used,
        "mean_overround_pct": mean(overrounds) if overrounds else None,
        "players": {},
    }
    for p in players:
        odds = per_player_odds[p]
        med = median(odds) if odds else None
        metrics["players"][p] = {
            "fair_probability": consensus[p],
            "fair_odds": fair_odds(consensus[p]),
            "median_reference_odds": med,
            "best_reference_odds": max(odds) if odds else None,
            "price_dispersion_pct": (
                (max(odds) - min(odds)) / med * 100.0
                if odds and med and med > 0 else None
            ),
        }
    return metrics


def _next_event_minutes(db: Database, sport_key: str, now: datetime) -> Optional[float]:
    rows = db.fetchall(
        """
        SELECT commence_time FROM tennis_events
        WHERE sport_key=? AND status='UPCOMING'
        ORDER BY commence_time ASC
        """,
        (sport_key,),
    )
    future = []
    for r in rows:
        try:
            mins = (parse_iso(r["commence_time"]) - now).total_seconds() / 60.0
        except Exception:
            continue
        if mins > 0:
            future.append(mins)
    return min(future) if future else None


def _broad_interval_minutes(next_mins: Optional[float]) -> int:
    if next_mins is None:
        return 360
    if next_mins <= 30:
        return 10
    if next_mins <= 90:
        return 20
    if next_mins <= 360:
        return 60
    if next_mins <= 1440:
        return 180
    return 360


def _convergence_interval_minutes(next_mins: Optional[float]) -> int:
    if next_mins is None:
        return 180
    if next_mins <= 30:
        return 5
    if next_mins <= 90:
        return 10
    if next_mins <= 360:
        return 20
    if next_mins <= 1440:
        return 60
    return 180


def _is_due(last_at: Optional[str], interval_minutes: int, now: datetime) -> bool:
    if not last_at:
        return True
    try:
        elapsed = (now - parse_iso(last_at)).total_seconds() / 60.0
    except Exception:
        return True
    return elapsed >= max(1, int(interval_minutes))


def _lateness(last_at: Optional[str], interval_minutes: int, now: datetime) -> float:
    if not last_at:
        return 10.0
    try:
        elapsed = max(0.0, (now - parse_iso(last_at)).total_seconds() / 60.0)
    except Exception:
        return 10.0
    return elapsed / max(1.0, float(interval_minutes))


def _insert_payload(
    db: Database,
    *,
    sport_key: str,
    tournament_title: str,
    payload: Sequence[Mapping[str, Any]],
    capture_mode: str,
    captured_at: str,
) -> Tuple[int, List[str]]:
    tour = _tour(sport_key)
    level = _level(sport_key)
    rows = []
    event_ids: List[str] = []

    for event in payload:
        event_id = str(event.get("id") or "")
        commence = str(event.get("commence_time") or "")
        p1 = str(event.get("home_team") or "")
        p2 = str(event.get("away_team") or "")
        if not event_id or not commence or not p1 or not p2:
            continue
        try:
            if parse_iso(commence) <= parse_iso(captured_at):
                # We still retain subsequent convergence quotes for already-known
                # events through direct payload insertion below only when event exists.
                existing = db.fetchone(
                    "SELECT event_id FROM tennis_events WHERE event_id=?", (event_id,)
                )
                if not existing:
                    continue
        except Exception:
            continue

        db.execute(
            """
            INSERT INTO tennis_events(
                event_id,sport_key,tournament_title,tour,tournament_level,
                commence_time,player_one,player_two,first_seen_at,last_seen_at,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET
                sport_key=excluded.sport_key,
                tournament_title=excluded.tournament_title,
                tour=excluded.tour,
                tournament_level=excluded.tournament_level,
                commence_time=excluded.commence_time,
                player_one=excluded.player_one,
                player_two=excluded.player_two,
                last_seen_at=excluded.last_seen_at
            """,
            (
                event_id,sport_key,tournament_title,tour,level,commence,p1,p2,
                captured_at,captured_at,"UPCOMING",
            ),
        )
        event_ids.append(event_id)
        for book in event.get("bookmakers") or []:
            for market in book.get("markets") or []:
                if str(market.get("key") or "") != "h2h":
                    continue
                for outcome in market.get("outcomes") or []:
                    try:
                        price = float(outcome.get("price"))
                    except Exception:
                        continue
                    selection = str(outcome.get("name") or "")
                    if price <= 1.0 or not selection:
                        continue
                    rows.append(
                        (
                            event_id,sport_key,captured_at,capture_mode,
                            str(book.get("key") or ""),
                            str(book.get("title") or book.get("key") or ""),
                            book.get("last_update"),
                            "h2h",selection,price,
                        )
                    )
    if rows:
        db.executemany(
            """
            INSERT INTO tennis_odds_snapshots(
                event_id,sport_key,captured_at,capture_mode,bookmaker_key,
                bookmaker_title,bookmaker_last_update,market_key,selection,price
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
    return len(rows), event_ids


def write_tennis_consensus(
    db: Database,
    event_id: str,
    captured_at: str,
    *,
    min_books: int,
    excluded_books: Sequence[str],
) -> int:
    event = db.fetchone("SELECT * FROM tennis_events WHERE event_id=?", (event_id,))
    if not event:
        return 0
    rows = db.fetchall(
        """
        SELECT * FROM tennis_odds_snapshots
        WHERE event_id=? AND captured_at=? AND capture_mode='BREADTH' AND market_key='h2h'
        ORDER BY id
        """,
        (event_id,captured_at),
    )
    excluded = set(str(x) for x in excluded_books)
    grouped: Dict[str, Dict[str, float]] = defaultdict(dict)
    for r in rows:
        book = str(r["bookmaker_key"])
        if book in excluded or "_ex_" in book:
            continue
        grouped[book][str(r["selection"])] = float(r["price"])

    metrics = _market_metrics(
        grouped, (event["player_one"], event["player_two"])
    )
    if not metrics or int(metrics["num_books"]) < int(min_books):
        return 0

    written = 0
    for selection in (event["player_one"], event["player_two"]):
        m = metrics["players"][selection]
        db.execute(
            """
            INSERT INTO tennis_consensus_snapshots(
                event_id,sport_key,captured_at,selection,fair_probability,fair_odds,
                num_books,median_reference_odds,best_reference_odds,
                price_dispersion_pct,mean_overround_pct,model
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id,captured_at,selection) DO NOTHING
            """,
            (
                event_id,event["sport_key"],captured_at,selection,
                m["fair_probability"],m["fair_odds"],metrics["num_books"],
                m["median_reference_odds"],m["best_reference_odds"],
                m["price_dispersion_pct"],metrics["mean_overround_pct"],
                "two_way_bookmaker_consensus",
            ),
        )
        written += 1
    return written


def _latest_consensus(
    db: Database,
    event_id: str,
    selection: str,
    evaluated_at: datetime,
) -> Optional[Dict[str, Any]]:
    row = db.fetchone(
        """
        SELECT * FROM tennis_consensus_snapshots
        WHERE event_id=? AND selection=? AND captured_at<=?
        ORDER BY captured_at DESC,id DESC LIMIT 1
        """,
        (event_id,selection,evaluated_at.isoformat()),
    )
    return row


def _record_eval(
    db: Database,
    *,
    evaluated_at: str,
    event_id: str,
    sport_key: str,
    selection: str,
    approved_books_seen: int,
    best_executable_odds: Optional[float],
    min_required_odds: Optional[float],
    fair_probability: Optional[float],
    fair_odds_value: Optional[float],
    edge: Optional[float],
    consensus_captured_at: Optional[str],
    consensus_age_minutes: Optional[float],
    decision: str,
    reason: str,
    metadata: Optional[Mapping[str, Any]] = None,
) -> None:
    # One audit row per event/selection/evaluation wave/reason.
    exists = db.fetchone(
        """
        SELECT id FROM tennis_execution_evaluations
        WHERE evaluated_at=? AND event_id=? AND selection=? AND decision=? AND reason=?
        LIMIT 1
        """,
        (evaluated_at,event_id,selection,decision,reason),
    )
    if exists:
        return
    db.execute(
        """
        INSERT INTO tennis_execution_evaluations(
            evaluated_at,event_id,sport_key,selection,approved_books_seen,
            best_executable_odds,min_required_odds,fair_probability,fair_odds,
            edge_pct,consensus_captured_at,consensus_age_minutes,decision,reason,
            metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            evaluated_at,event_id,sport_key,selection,approved_books_seen,
            best_executable_odds,min_required_odds,fair_probability,fair_odds_value,
            edge,consensus_captured_at,consensus_age_minutes,decision,reason,
            json.dumps(dict(metadata or {}), separators=(",", ":")),
        ),
    )


def evaluate_tennis_convergence_wave(
    db: Database,
    *,
    sport_key: str,
    captured_at: str,
    execution_bookmaker_keys: Sequence[str],
    min_edge_pct: float,
    max_consensus_age_minutes: int,
    config_hash: str,
) -> int:
    evaluated_at = parse_iso(captured_at)
    events = db.fetchall(
        """
        SELECT DISTINCT e.* FROM tennis_events e
        JOIN tennis_odds_snapshots o ON o.event_id=e.event_id
        WHERE o.sport_key=? AND o.captured_at=? AND o.capture_mode='CONVERGENCE'
          AND e.status='UPCOMING'
        ORDER BY e.commence_time
        """,
        (sport_key,captured_at),
    )
    approved = set(str(x) for x in execution_bookmaker_keys)
    created = 0

    for event in events:
        if parse_iso(event["commence_time"]) <= evaluated_at:
            continue
        quote_rows = db.fetchall(
            """
            SELECT * FROM tennis_odds_snapshots
            WHERE event_id=? AND captured_at=? AND capture_mode='CONVERGENCE'
              AND market_key='h2h'
            ORDER BY id
            """,
            (event["event_id"],captured_at),
        )
        by_selection: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for q in quote_rows:
            if str(q["bookmaker_key"]) in approved:
                by_selection[str(q["selection"])].append(q)

        for selection in (event["player_one"],event["player_two"]):
            consensus = _latest_consensus(
                db,event["event_id"],selection,evaluated_at
            )
            quotes = by_selection.get(str(selection), [])
            if not consensus:
                _record_eval(
                    db,evaluated_at=captured_at,event_id=event["event_id"],
                    sport_key=sport_key,selection=selection,
                    approved_books_seen=len({q["bookmaker_key"] for q in quotes}),
                    best_executable_odds=max([float(q["price"]) for q in quotes],default=None),
                    min_required_odds=None,fair_probability=None,fair_odds_value=None,
                    edge=None,consensus_captured_at=None,consensus_age_minutes=None,
                    decision="REJECT",reason="NO_REFERENCE_CONSENSUS",
                )
                continue

            consensus_age = max(
                0.0,(evaluated_at-parse_iso(consensus["captured_at"])).total_seconds()/60.0
            )
            prob = float(consensus["fair_probability"])
            f_odds = float(consensus["fair_odds"])
            min_odds = min_odds_for_probability(prob,min_edge_pct)

            if consensus_age > float(max_consensus_age_minutes):
                _record_eval(
                    db,evaluated_at=captured_at,event_id=event["event_id"],
                    sport_key=sport_key,selection=selection,
                    approved_books_seen=len({q["bookmaker_key"] for q in quotes}),
                    best_executable_odds=max([float(q["price"]) for q in quotes],default=None),
                    min_required_odds=min_odds,fair_probability=prob,fair_odds_value=f_odds,
                    edge=None,consensus_captured_at=consensus["captured_at"],
                    consensus_age_minutes=consensus_age,
                    decision="REJECT",reason="STALE_REFERENCE_CONSENSUS",
                )
                continue

            if not quotes:
                _record_eval(
                    db,evaluated_at=captured_at,event_id=event["event_id"],
                    sport_key=sport_key,selection=selection,
                    approved_books_seen=0,best_executable_odds=None,
                    min_required_odds=min_odds,fair_probability=prob,fair_odds_value=f_odds,
                    edge=None,consensus_captured_at=consensus["captured_at"],
                    consensus_age_minutes=consensus_age,
                    decision="REJECT",reason="NO_APPROVED_VENUE_QUOTE",
                )
                continue

            best = max(quotes,key=lambda q: (float(q["price"]),str(q["bookmaker_key"])))
            best_odds = float(best["price"])
            e = edge_pct(prob,best_odds)
            existing = db.fetchone(
                "SELECT * FROM tennis_execution_bets WHERE execution_key=?",
                (f"{event['event_id']}|h2h|{selection}",),
            )
            if existing:
                _record_eval(
                    db,evaluated_at=captured_at,event_id=event["event_id"],
                    sport_key=sport_key,selection=selection,
                    approved_books_seen=len({q["bookmaker_key"] for q in quotes}),
                    best_executable_odds=best_odds,min_required_odds=min_odds,
                    fair_probability=prob,fair_odds_value=f_odds,edge=e,
                    consensus_captured_at=consensus["captured_at"],
                    consensus_age_minutes=consensus_age,
                    decision="TRACK",reason="EXECUTION_ALREADY_FROZEN",
                )
                continue

            if best_odds + 1e-12 < min_odds:
                _record_eval(
                    db,evaluated_at=captured_at,event_id=event["event_id"],
                    sport_key=sport_key,selection=selection,
                    approved_books_seen=len({q["bookmaker_key"] for q in quotes}),
                    best_executable_odds=best_odds,min_required_odds=min_odds,
                    fair_probability=prob,fair_odds_value=f_odds,edge=e,
                    consensus_captured_at=consensus["captured_at"],
                    consensus_age_minutes=consensus_age,
                    decision="REJECT",reason="EXECUTABLE_PRICE_BELOW_MIN",
                )
                continue

            db.execute(
                """
                INSERT INTO tennis_execution_bets(
                    execution_key,created_at,event_id,sport_key,selection,
                    bookmaker_key,bookmaker_title,offered_odds,fair_probability,
                    fair_odds,edge_pct,min_odds,approved_books_seen,
                    consensus_captured_at,consensus_age_minutes,reference_book_count,
                    reference_median_odds,reference_best_odds,
                    reference_dispersion_pct,reference_mean_overround_pct,
                    status,app_version,experiment_version,config_hash
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"{event['event_id']}|h2h|{selection}",captured_at,
                    event["event_id"],sport_key,selection,best["bookmaker_key"],
                    best["bookmaker_title"],best_odds,prob,f_odds,e,min_odds,
                    len({q["bookmaker_key"] for q in quotes}),
                    consensus["captured_at"],consensus_age,int(consensus["num_books"]),
                    consensus.get("median_reference_odds"),
                    consensus.get("best_reference_odds"),
                    consensus.get("price_dispersion_pct"),
                    consensus.get("mean_overround_pct"),
                    "OPEN",APP_VERSION,EXPERIMENT_VERSION,config_hash,
                ),
            )
            _record_eval(
                db,evaluated_at=captured_at,event_id=event["event_id"],
                sport_key=sport_key,selection=selection,
                approved_books_seen=len({q["bookmaker_key"] for q in quotes}),
                best_executable_odds=best_odds,min_required_odds=min_odds,
                fair_probability=prob,fair_odds_value=f_odds,edge=e,
                consensus_captured_at=consensus["captured_at"],
                consensus_age_minutes=consensus_age,
                decision="ACCEPT",reason="FIRST_ACCEPTABLE_APPROVED_PRICE",
                metadata={"bookmaker_key":best["bookmaker_key"]},
            )
            created += 1
    return created


def track_tennis_prices(db: Database, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    bets = db.fetchall(
        """
        SELECT b.*,e.commence_time FROM tennis_execution_bets b
        JOIN tennis_events e ON e.event_id=b.event_id ORDER BY b.id
        """
    )
    inserted = 0
    for bet in bets:
        start = parse_iso(bet["commence_time"])
        rows = db.fetchall(
            """
            SELECT * FROM tennis_odds_snapshots
            WHERE event_id=? AND capture_mode='CONVERGENCE'
              AND bookmaker_key=? AND selection=? AND captured_at>?
            ORDER BY captured_at,id
            """,
            (bet["event_id"],bet["bookmaker_key"],bet["selection"],bet["created_at"]),
        )
        for row in rows:
            ts = parse_iso(row["captured_at"])
            if ts > min(now,start):
                continue
            exists = db.fetchone(
                """
                SELECT id FROM tennis_price_observations
                WHERE tennis_bet_id=? AND source_snapshot_at=?
                """,
                (bet["id"],row["captured_at"]),
            )
            if exists:
                continue
            move = (float(bet["offered_odds"])/float(row["price"])-1.0)*100.0
            db.execute(
                """
                INSERT INTO tennis_price_observations(
                    tennis_bet_id,observed_at,source_snapshot_at,bookmaker_key,
                    price,move_vs_entry_pct
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    bet["id"],utc_now_iso(),row["captured_at"],
                    bet["bookmaker_key"],row["price"],move,
                ),
            )
            inserted += 1
    return inserted


def finalize_tennis_clv(db: Database, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    bets = db.fetchall(
        """
        SELECT b.*,e.commence_time FROM tennis_execution_bets b
        JOIN tennis_events e ON e.event_id=b.event_id
        WHERE b.clv_pct IS NULL OR b.clv_quality IS NULL
        ORDER BY b.id
        """
    )
    updated = 0
    for bet in bets:
        start = parse_iso(bet["commence_time"])
        if start > now:
            continue
        rows = db.fetchall(
            """
            SELECT * FROM tennis_odds_snapshots
            WHERE event_id=? AND capture_mode='CONVERGENCE'
              AND bookmaker_key=? AND selection=? AND captured_at<=?
            ORDER BY captured_at DESC,id DESC
            """,
            (bet["event_id"],bet["bookmaker_key"],bet["selection"],start.isoformat()),
        )
        if not rows:
            continue
        close = rows[0]
        close_at = parse_iso(close["captured_at"])
        mins = max(0.0,(start-close_at).total_seconds()/60.0)
        closing = float(close["price"])

        consensus = db.fetchone(
            """
            SELECT * FROM tennis_consensus_snapshots
            WHERE event_id=? AND selection=? AND captured_at<=?
            ORDER BY captured_at DESC,id DESC LIMIT 1
            """,
            (bet["event_id"],bet["selection"],start.isoformat()),
        )
        c_odds=c_at=c_mins=c_quality=c_clv=None
        if consensus:
            c_odds=float(consensus["fair_odds"])
            c_at=consensus["captured_at"]
            c_mins=max(0.0,(start-parse_iso(c_at)).total_seconds()/60.0)
            c_quality=clv_quality(c_mins)
            c_clv=clv_pct(float(bet["offered_odds"]),c_odds)

        db.execute(
            """
            UPDATE tennis_execution_bets
            SET closing_odds=?,clv_pct=?,closing_observed_at=?,
                closing_minutes_before_start=?,clv_quality=?,
                closing_consensus_fair_odds=?,closing_consensus_observed_at=?,
                closing_consensus_minutes_before_start=?,
                closing_consensus_quality=?,clv_vs_consensus_pct=?
            WHERE id=?
            """,
            (
                closing,clv_pct(float(bet["offered_odds"]),closing),
                close["captured_at"],mins,clv_quality(mins),
                c_odds,c_at,c_mins,c_quality,c_clv,bet["id"],
            ),
        )
        updated += 1
    return updated


def settle_tennis_event(db: Database, event_id: str, winner: str) -> int:
    bets = db.fetchall(
        """
        SELECT * FROM tennis_execution_bets
        WHERE event_id=? AND status='OPEN' ORDER BY id
        """,
        (event_id,),
    )
    settled = 0
    for bet in bets:
        result = "WIN" if str(bet["selection"]) == str(winner) else "LOSS"
        gross = float(bet["offered_odds"])-1.0 if result=="WIN" else -1.0
        rate,commission,net=commission_adjusted_pnl(gross,str(bet["bookmaker_key"]))
        db.execute(
            """
            UPDATE tennis_execution_bets
            SET status='SETTLED',result=?,pnl_units=?,commission_rate_pct=?,
                commission_units=?,net_pnl_units=?,settled_at=?
            WHERE id=?
            """,
            (result,gross,rate,commission,net,utc_now_iso(),bet["id"]),
        )
        settled += 1
    return settled


def _probability_calibration(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    usable=[]
    for r in rows:
        if r.get("result") not in {"WIN","LOSS"}:
            continue
        p=float(r["fair_probability"])
        if not 0 < p < 1:
            continue
        y=1.0 if r["result"]=="WIN" else 0.0
        usable.append((p,y))
    if not usable:
        return {
            "samples":0,"expected_win_pct":None,"observed_win_pct":None,
            "calibration_gap_pp":None,"brier_score":None,"log_loss":None,
        }
    eps=1e-12
    exp=mean(p for p,_ in usable)
    obs=mean(y for _,y in usable)
    return {
        "samples":len(usable),
        "expected_win_pct":exp*100.0,
        "observed_win_pct":obs*100.0,
        "calibration_gap_pp":(obs-exp)*100.0,
        "brier_score":mean((p-y)**2 for p,y in usable),
        "log_loss":-mean(
            y*math.log(max(eps,min(1-eps,p)))+
            (1-y)*math.log(max(eps,min(1-eps,1-p)))
            for p,y in usable
        ),
    }


def _segment_rows(rows: Sequence[Mapping[str, Any]], label_fn) -> List[Dict[str, Any]]:
    groups: Dict[str,List[Mapping[str,Any]]] = defaultdict(list)
    for r in rows:
        groups[str(label_fn(r))].append(r)
    out=[]
    for label,items in sorted(groups.items()):
        settled=[x for x in items if x.get("pnl_units") is not None]
        headline=[x for x in items if x.get("clv_pct") is not None and is_headline_clv_quality(x.get("clv_quality"))]
        clvs=[float(x["clv_pct"]) for x in headline]
        net=sum(float(x.get("net_pnl_units") if x.get("net_pnl_units") is not None else x.get("pnl_units") or 0.0) for x in settled)
        out.append({
            "label":label,
            "bets":len(items),
            "settled":len(settled),
            "net_pnl_units":net,
            "net_roi_pct":(net/len(settled)*100.0) if settled else None,
            "clv_samples":len(clvs),
            "avg_clv_pct":mean(clvs) if clvs else None,
            "median_clv_pct":median(clvs) if clvs else None,
            "beat_close_pct":sum(1 for x in clvs if x>0)/len(clvs)*100.0 if clvs else None,
        })
    return out


def tennis_scoreboard(db: Database) -> Dict[str, Any]:
    rows=db.fetchall(
        """
        SELECT b.*,e.tour,e.tournament_title,e.tournament_level,e.player_one,
               e.player_two,e.commence_time
        FROM tennis_execution_bets b JOIN tennis_events e ON e.event_id=b.event_id
        ORDER BY b.created_at,b.id
        """
    )
    settled=[r for r in rows if r.get("pnl_units") is not None]
    headline=[r for r in rows if r.get("clv_pct") is not None and is_headline_clv_quality(r.get("clv_quality"))]
    clvs=[float(r["clv_pct"]) for r in headline]
    all_clv=[float(r["clv_pct"]) for r in rows if r.get("clv_pct") is not None]
    gross=sum(float(r.get("pnl_units") or 0.0) for r in settled)
    net=sum(float(r.get("net_pnl_units") if r.get("net_pnl_units") is not None else r.get("pnl_units") or 0.0) for r in settled)
    commission=sum(float(r.get("commission_units") or 0.0) for r in settled)
    wins=sum(1 for r in settled if r.get("result")=="WIN")
    quality_counts={q:0 for q in ("A","B","C","STALE")}
    for r in rows:
        if r.get("clv_quality") in quality_counts and r.get("clv_pct") is not None:
            quality_counts[r["clv_quality"]]+=1
    return {
        "events":int((db.fetchone("SELECT COUNT(*) AS n FROM tennis_events") or {}).get("n") or 0),
        "active_tournaments":int((db.fetchone("SELECT COUNT(*) AS n FROM tennis_tournament_state WHERE active=1") or {}).get("n") or 0),
        "consensus_rows":int((db.fetchone("SELECT COUNT(*) AS n FROM tennis_consensus_snapshots") or {}).get("n") or 0),
        "evaluations":int((db.fetchone("SELECT COUNT(*) AS n FROM tennis_execution_evaluations") or {}).get("n") or 0),
        "bets":len(rows),"settled":len(settled),"wins":wins,
        "gross_pnl_units":gross,"net_pnl_units":net,"commission_units":commission,
        "gross_roi_pct":(gross/len(settled)*100.0) if settled else None,
        "net_roi_pct":(net/len(settled)*100.0) if settled else None,
        "win_rate_pct":(wins/len(settled)*100.0) if settled else None,
        "clv_samples":len(clvs),
        "avg_clv_pct":mean(clvs) if clvs else None,
        "median_clv_pct":median(clvs) if clvs else None,
        "beat_close_pct":sum(1 for x in clvs if x>0)/len(clvs)*100.0 if clvs else None,
        "all_clv_samples":len(all_clv),
        "all_avg_clv_pct":mean(all_clv) if all_clv else None,
        "clv_quality_counts":quality_counts,
        "calibration":_probability_calibration(settled),
    }


def tennis_segments(db: Database) -> Dict[str, Any]:
    rows=db.fetchall(
        """
        SELECT b.*,e.tour,e.tournament_title,e.tournament_level,e.player_one,e.player_two
        FROM tennis_execution_bets b JOIN tennis_events e ON e.event_id=b.event_id
        ORDER BY b.id
        """
    )
    def band(r):
        o=float(r["offered_odds"])
        if o < 1.5:return "<1.5"
        if o < 2.0:return "1.5-2"
        if o < 3.0:return "2-3"
        if o < 5.0:return "3-5"
        return "5+"
    return {
        "tour":_segment_rows(rows,lambda r:r["tour"]),
        "tournament":_segment_rows(rows,lambda r:r["tournament_title"]),
        "tournament_level":_segment_rows(rows,lambda r:r["tournament_level"]),
        "odds_band":_segment_rows(rows,band),
        "side":_segment_rows(rows,lambda r:"FAVOURITE" if float(r["fair_probability"])>=0.5 else "OUTSIDER"),
        "venue":_segment_rows(rows,lambda r:r["bookmaker_key"]),
    }


def latest_tennis_bets(db: Database, limit: int = 100) -> List[Dict[str, Any]]:
    return db.fetchall(
        """
        SELECT b.*,e.tournament_title,e.tour,e.tournament_level,e.player_one,
               e.player_two,e.commence_time,
               (SELECT p.move_vs_entry_pct FROM tennis_price_observations p
                WHERE p.tennis_bet_id=b.id ORDER BY p.source_snapshot_at DESC LIMIT 1)
                AS latest_move_pct
        FROM tennis_execution_bets b JOIN tennis_events e ON e.event_id=b.event_id
        ORDER BY b.id DESC LIMIT ?
        """,
        (limit,),
    )


class TennisShadowEngine:
    def __init__(
        self,
        db: Database,
        api,
        settings,
        *,
        execution_bookmaker_keys: Sequence[str],
    ):
        self.db=db
        self.api=api
        self.settings=settings
        self.execution_bookmaker_keys=tuple(str(x) for x in execution_bookmaker_keys)
        self.quota=TennisQuotaGuard(
            db,
            daily_budget=settings.tennis_daily_paid_credit_budget,
            reserve=settings.tennis_quota_reserve_credits,
        )
        self.config_hash=_safe_hash(settings,self.execution_bookmaker_keys)
        self._last_discovery_at: Optional[datetime]=None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.tennis_shadow_enabled)

    def discover_active_tournaments(self, now: Optional[datetime]=None) -> int:
        if not self.enabled:
            return 0
        now=now or datetime.now(timezone.utc)
        result=self.api.sports()
        self.quota.update(
            remaining=result.remaining,used=result.used,last_cost=result.last_cost
        )
        sports=result.data if isinstance(result.data,list) else []
        active=[]
        stamp=now.isoformat()
        for item in sports:
            key=str(item.get("key") or "")
            if not key.startswith("tennis_") or not bool(item.get("active",True)):
                continue
            title=str(item.get("title") or key)
            tour=_tour(key); level=_level(key)
            self.db.execute(
                """
                INSERT INTO tennis_tournament_state(
                    sport_key,title,tour,tournament_level,active,first_seen_at,last_seen_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(sport_key) DO UPDATE SET
                    title=excluded.title,tour=excluded.tour,
                    tournament_level=excluded.tournament_level,
                    active=1,last_seen_at=excluded.last_seen_at
                """,
                (key,title,tour,level,1,stamp,stamp),
            )
            active.append(key)
        if active:
            placeholders=",".join("?" for _ in active)
            self.db.execute(
                f"UPDATE tennis_tournament_state SET active=0 WHERE sport_key NOT IN ({placeholders})",
                active,
            )
        else:
            self.db.execute("UPDATE tennis_tournament_state SET active=0")
        self.db.record_collector_run(
            "TENNIS_DISCOVERY",True,requested_markets="sports",
            actual_cost=provider_actual_cost(result.last_cost, 0),
            detail=f"active_tournaments={len(active)}; provider_call_cost=0",
        )
        self._last_discovery_at=now
        return len(active)

    def _maybe_discover(self, now: datetime) -> None:
        interval=max(300,int(self.settings.tennis_discovery_interval_seconds))
        if self._last_discovery_at is None:
            row=self.db.fetchone(
                """
                SELECT started_at FROM collector_runs
                WHERE run_type='TENNIS_DISCOVERY' AND ok=1
                ORDER BY id DESC LIMIT 1
                """
            )
            if row:
                try:self._last_discovery_at=parse_iso(row["started_at"])
                except Exception:self._last_discovery_at=None
        if self._last_discovery_at is None or (now-self._last_discovery_at).total_seconds()>=interval:
            try:self.discover_active_tournaments(now)
            except Exception as exc:
                self.db.record_collector_run(
                    "TENNIS_DISCOVERY",False,
                    detail=f"error={sanitize_sensitive_text(exc)}",
                )
                self._last_discovery_at=now

    def _due_lanes(self, now: datetime) -> List[Dict[str, Any]]:
        rows=self.db.fetchall(
            """
            SELECT * FROM tennis_tournament_state
            WHERE active=1 ORDER BY sport_key
            """
        )
        due=[]
        for row in rows:
            next_mins=_next_event_minutes(self.db,row["sport_key"],now)
            b_int=_broad_interval_minutes(next_mins)
            c_int=_convergence_interval_minutes(next_mins)
            if _is_due(row.get("last_broad_poll_at"),b_int,now):
                due.append({
                    "mode":"breadth","sport_key":row["sport_key"],
                    "title":row["title"],"interval":b_int,
                    "lateness":_lateness(row.get("last_broad_poll_at"),b_int,now),
                    "next_mins":next_mins,
                })
            # Convergence is useful once at least one fresh/old consensus exists.
            has_consensus=self.db.fetchone(
                """
                SELECT id FROM tennis_consensus_snapshots
                WHERE sport_key=? LIMIT 1
                """,(row["sport_key"],)
            )
            if has_consensus and _is_due(row.get("last_convergence_poll_at"),c_int,now):
                due.append({
                    "mode":"convergence","sport_key":row["sport_key"],
                    "title":row["title"],"interval":c_int,
                    "lateness":_lateness(row.get("last_convergence_poll_at"),c_int,now),
                    "next_mins":next_mins,
                })
        # Nearest match, then most overdue. If same tournament has both lanes
        # due and similarly urgent, breadth wins ties to keep fair value fresh.
        return sorted(
            due,
            key=lambda x:(
                10**9 if x["next_mins"] is None else max(0.0,float(x["next_mins"])),
                -float(x["lateness"]),
                0 if x["mode"]=="breadth" else 1,
                x["sport_key"],
            ),
        )

    def _poll_breadth(self, target: Mapping[str,Any], now: datetime) -> Dict[str,Any]:
        allowed,reason=self.quota.decide(1)
        if not allowed:
            return {"mode":"breadth","polled":0,"reason":reason}
        sport_key=str(target["sport_key"]); title=str(target["title"])
        actual=0
        try:
            result=self.api.sport_odds(
                sport_key,self.settings.odds_region,(self.settings.tennis_market,)
            )
            actual=provider_actual_cost(result.last_cost, 1)
            self.quota.update(
                remaining=result.remaining,used=result.used,last_cost=result.last_cost
            )
            captured=now.isoformat()
            payload=result.data if isinstance(result.data,list) else []
            quote_rows,event_ids=_insert_payload(
                self.db,sport_key=sport_key,tournament_title=title,
                payload=payload,capture_mode="BREADTH",captured_at=captured,
            )
            consensus=0
            for eid in event_ids:
                consensus+=write_tennis_consensus(
                    self.db,eid,captured,
                    min_books=self.settings.tennis_min_consensus_books,
                    excluded_books=self.execution_bookmaker_keys,
                )
            self.db.execute(
                "UPDATE tennis_tournament_state SET last_broad_poll_at=? WHERE sport_key=?",
                (captured,sport_key),
            )
            self.db.record_collector_run(
                "TENNIS_ODDS",True,sport_key=sport_key,requested_markets="h2h",
                estimated_cost=1,actual_cost=actual,
                detail=f"mode=breadth; events={len(event_ids)}; quote_rows={quote_rows}; consensus_rows={consensus}",
            )
            return {
                "mode":"breadth","polled":1,"sport_key":sport_key,
                "events":len(event_ids),"quote_rows":quote_rows,
                "consensus_rows":consensus,"cost":actual,
            }
        except Exception as exc:
            self.db.record_collector_run(
                "TENNIS_ODDS",False,sport_key=sport_key,requested_markets="h2h",
                estimated_cost=1,actual_cost=actual,
                detail=f"mode=breadth; error={sanitize_sensitive_text(exc)}",
            )
            return {"mode":"breadth","polled":0,"sport_key":sport_key,"reason":"error"}

    def _poll_convergence(self,target:Mapping[str,Any],now:datetime)->Dict[str,Any]:
        allowed,reason=self.quota.decide(1)
        if not allowed:
            return {"mode":"convergence","polled":0,"reason":reason}
        sport_key=str(target["sport_key"]); title=str(target["title"])
        actual=0
        try:
            result=self.api.sport_odds(
                sport_key,self.settings.odds_region,(self.settings.tennis_market,),
                bookmaker_keys=self.execution_bookmaker_keys,
            )
            actual=provider_actual_cost(result.last_cost, 1)
            self.quota.update(
                remaining=result.remaining,used=result.used,last_cost=result.last_cost
            )
            captured=now.isoformat()
            payload=result.data if isinstance(result.data,list) else []
            quote_rows,event_ids=_insert_payload(
                self.db,sport_key=sport_key,tournament_title=title,payload=payload,
                capture_mode="CONVERGENCE",captured_at=captured,
            )
            created=evaluate_tennis_convergence_wave(
                self.db,sport_key=sport_key,captured_at=captured,
                execution_bookmaker_keys=self.execution_bookmaker_keys,
                min_edge_pct=self.settings.tennis_min_edge_pct,
                max_consensus_age_minutes=self.settings.tennis_max_consensus_age_minutes,
                config_hash=self.config_hash,
            )
            self.db.execute(
                "UPDATE tennis_tournament_state SET last_convergence_poll_at=? WHERE sport_key=?",
                (captured,sport_key),
            )
            self.db.record_collector_run(
                "TENNIS_CONVERGENCE",True,sport_key=sport_key,requested_markets="h2h",
                estimated_cost=1,actual_cost=actual,
                detail=f"mode=convergence; events={len(event_ids)}; quote_rows={quote_rows}; executions_created={created}",
            )
            return {
                "mode":"convergence","polled":1,"sport_key":sport_key,
                "events":len(event_ids),"quote_rows":quote_rows,
                "executions_created":created,"cost":actual,
            }
        except Exception as exc:
            self.db.record_collector_run(
                "TENNIS_CONVERGENCE",False,sport_key=sport_key,requested_markets="h2h",
                estimated_cost=1,actual_cost=actual,
                detail=f"mode=convergence; error={sanitize_sensitive_text(exc)}",
            )
            return {"mode":"convergence","polled":0,"sport_key":sport_key,"reason":"error"}

    def collect_results(self,now:datetime)->Dict[str,Any]:
        cutoff=now-timedelta(minutes=max(30,int(self.settings.tennis_result_min_minutes_after_start)))
        due=self.db.fetchall(
            """
            SELECT DISTINCT e.* FROM tennis_events e
            JOIN tennis_execution_bets b ON b.event_id=e.event_id
            LEFT JOIN tennis_results r ON r.event_id=e.event_id
            WHERE b.status='OPEN' AND r.event_id IS NULL AND e.commence_time<=?
            ORDER BY e.commence_time
            """,
            (cutoff.isoformat(),),
        )
        if not due:return {"checked":0,"settled":0,"reason":"no_due_results"}
        grouped:Dict[str,List[Dict[str,Any]]]=defaultdict(list)
        for e in due:grouped[e["sport_key"]].append(e)
        checked=settled=0
        for sport_key,events in grouped.items():
            state=self.db.fetchone(
                "SELECT * FROM tennis_tournament_state WHERE sport_key=?",(sport_key,)
            ) or {}
            last=state.get("last_results_poll_at")
            interval=max(900,int(self.settings.tennis_result_poll_interval_seconds))
            if last:
                try:
                    if (now-parse_iso(last)).total_seconds()<interval:continue
                except Exception:pass
            allowed,reason=self.quota.decide(2)
            if not allowed:
                self.db.record_collector_run(
                    "TENNIS_RESULTS",False,sport_key=sport_key,requested_markets="scores",
                    estimated_cost=2,actual_cost=0,detail=f"quota_block:{reason}",
                )
                continue
            actual=0
            try:
                result=self.api.scores(
                    sport_key,event_ids=[e["event_id"] for e in events],days_from=1
                )
                actual=provider_actual_cost(result.last_cost, 2)
                self.quota.update(
                    remaining=result.remaining,used=result.used,last_cost=result.last_cost
                )
                payload=result.data if isinstance(result.data,list) else []
                wanted={e["event_id"]:e for e in events}
                found=0
                for item in payload:
                    eid=str(item.get("id") or "")
                    if eid not in wanted or not item.get("completed"):continue
                    score_map={}
                    for s in item.get("scores") or []:
                        try:score_map[str(s["name"])]=int(float(s["score"]))
                        except Exception:continue
                    e=wanted[eid]
                    p1=e["player_one"];p2=e["player_two"]
                    if p1 not in score_map or p2 not in score_map:continue
                    s1=score_map[p1];s2=score_map[p2]
                    if s1==s2:continue
                    winner=p1 if s1>s2 else p2
                    fetched=utc_now_iso()
                    self.db.execute(
                        """
                        INSERT INTO tennis_results(
                            event_id,fetched_at,completed_at,winner,player_one_score,
                            player_two_score,source,settlement_quality,
                            settlement_provenance,raw_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(event_id) DO UPDATE SET
                            fetched_at=excluded.fetched_at,
                            completed_at=excluded.completed_at,winner=excluded.winner,
                            player_one_score=excluded.player_one_score,
                            player_two_score=excluded.player_two_score,
                            source=excluded.source,
                            settlement_quality=excluded.settlement_quality,
                            settlement_provenance=excluded.settlement_provenance,
                            raw_json=excluded.raw_json
                        """,
                        (
                            eid,fetched,item.get("commence_time"),winner,s1,s2,
                            "the_odds_api","PROVIDER_COMPLETED",
                            "provider completed match winner; retirement status not separately verified",
                            json.dumps(item,separators=(",",":")),
                        ),
                    )
                    self.db.execute(
                        "UPDATE tennis_events SET status='COMPLETED' WHERE event_id=?",(eid,)
                    )
                    settled+=settle_tennis_event(self.db,eid,winner)
                    found+=1
                checked+=len(events)
                self.db.execute(
                    "UPDATE tennis_tournament_state SET last_results_poll_at=? WHERE sport_key=?",
                    (now.isoformat(),sport_key),
                )
                self.db.record_collector_run(
                    "TENNIS_RESULTS",True,sport_key=sport_key,requested_markets="scores",
                    estimated_cost=2,actual_cost=actual,
                    detail=f"checked={len(events)}; completed_events={found}; bets_settled={settled}",
                )
            except Exception as exc:
                # Back off even when the tournament's scores endpoint is not
                # supported so it cannot be retried every worker tick.
                self.db.execute(
                    "UPDATE tennis_tournament_state SET last_results_poll_at=? WHERE sport_key=?",
                    (now.isoformat(),sport_key),
                )
                self.db.record_collector_run(
                    "TENNIS_RESULTS",False,sport_key=sport_key,requested_markets="scores",
                    estimated_cost=2,actual_cost=actual,
                    detail=f"error={sanitize_sensitive_text(exc)}",
                )
        return {"checked":checked,"settled":settled,"reason":"ok"}

    def maintenance(self,now:Optional[datetime]=None)->Dict[str,int]:
        now=now or datetime.now(timezone.utc)
        return {
            "price_observations":track_tennis_prices(self.db,now),
            "clv_finalized":finalize_tennis_clv(self.db,now),
        }

    def one_cycle(self,now:Optional[datetime]=None)->Dict[str,Any]:
        if not self.enabled:
            return {"enabled":False,"mode":"disabled","polled":0}
        now=now or datetime.now(timezone.utc)
        self._maybe_discover(now)
        due=self._due_lanes(now)
        odds_result={"mode":"idle","polled":0,"reason":"no_due_tennis_lane"}
        if due:
            target=due[0]
            if target["mode"]=="breadth":
                odds_result=self._poll_breadth(target,now)
            else:
                odds_result=self._poll_convergence(target,now)
        maint=self.maintenance(now)
        results=self.collect_results(now)
        return {
            "enabled":True,
            "odds":odds_result,
            "maintenance":maint,
            "results":results,
            "paid_credits_today":self.quota.today_paid_cost(now),
            "daily_budget":self.quota.daily_budget,
        }
