from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from statistics import mean, median
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from db import Database, sanitize_sensitive_text, utc_now_iso
from execution_shadow import clv_quality, commission_adjusted_pnl, is_headline_clv_quality
from fair_value import clv_pct, devig_prices, edge_pct, fair_odds, min_odds_for_probability
from quota import provider_actual_cost

APP_VERSION = "0.8.1"
EXPERIMENT_VERSION = "MSP1_TWO_WAY_PRICE"
MARKET_MODEL = "two_way_bookmaker_consensus"

SPORT_FAMILY_BY_PREFIX = (
    ("baseball_", "BASEBALL"),
    ("americanfootball_", "AMERICAN_FOOTBALL"),
    ("basketball_", "BASKETBALL"),
    ("aussierules_", "AUSSIE_RULES"),
    ("rugbyleague_", "RUGBY_LEAGUE"),
    ("icehockey_", "ICE_HOCKEY"),
)

RESULT_DELAY_MINUTES = {
    "BASEBALL": 240,
    "AMERICAN_FOOTBALL": 240,
    "BASKETBALL": 180,
    "AUSSIE_RULES": 210,
    "RUGBY_LEAGUE": 180,
    "ICE_HOCKEY": 180,
}

VERIFIED_AFL_DEAD_HEAT_BOOKS = {"betfair_ex_uk", "smarkets"}


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def sport_family(sport_key: str) -> str:
    key = str(sport_key)
    for prefix, family in SPORT_FAMILY_BY_PREFIX:
        if key.startswith(prefix):
            return family
    return "OTHER"


def _safe_hash(settings, execution_bookmaker_keys: Sequence[str]) -> str:
    payload = {
        "experiment": EXPERIMENT_VERSION,
        "target_sports": list(settings.multisport_sport_keys),
        "market": str(settings.multisport_market),
        "region": str(settings.multisport_odds_region),
        "hockey_reference_region": str(
            getattr(settings, "multisport_hockey_reference_region", "us")
        ),
        "min_consensus_books": int(settings.multisport_min_consensus_books),
        "min_edge_pct": float(settings.multisport_min_edge_pct),
        "max_consensus_age_minutes": int(settings.multisport_max_consensus_age_minutes),
        "execution_bookmaker_keys": list(execution_bookmaker_keys),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


class MultiSportQuotaGuard:
    RUN_TYPES = ("MULTISPORT_ODDS", "MULTISPORT_CONVERGENCE", "MULTISPORT_RESULTS", "MULTISPORT_LINES_ODDS", "MULTISPORT_LINES_CONVERGENCE", "MULTISPORT_LINES_RESULTS")

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
            WHERE run_type IN ('MULTISPORT_ODDS','MULTISPORT_CONVERGENCE','MULTISPORT_RESULTS','MULTISPORT_LINES_ODDS','MULTISPORT_LINES_CONVERGENCE','MULTISPORT_LINES_RESULTS')
              AND actual_cost>0 AND substr(started_at,1,10)=?
            """,
            (now.date().isoformat(),),
        )
        return int((row or {}).get("total") or 0)

    def decide(self, estimated_cost: int) -> Tuple[bool, str]:
        estimated_cost = max(1, int(estimated_cost))
        if self.today_paid_cost() + estimated_cost > self.daily_budget:
            return False, "multisport_daily_paid_credit_budget"
        state = self.db.fetchone("SELECT credits_remaining FROM quota_state WHERE singleton_id=1") or {}
        remaining = state.get("credits_remaining")
        if remaining is not None and int(remaining) - estimated_cost < self.reserve:
            return False, "protected_provider_reserve"
        return True, "ok"


def _book_waves(
    rows: Sequence[Mapping[str, Any]],
    home_team: str,
    away_team: str,
    *,
    allowed_books: Optional[Sequence[str]] = None,
    excluded_books: Sequence[str] = (),
) -> Dict[str, Dict[str, float]]:
    allowed = None if allowed_books is None else {str(x) for x in allowed_books if str(x)}
    excluded = {str(x) for x in excluded_books if str(x)}
    grouped: Dict[str, Dict[str, float]] = defaultdict(dict)
    outcomes: Dict[str, set[str]] = defaultdict(set)
    for r in rows:
        book = str(r.get("bookmaker_key") or "")
        selection = str(r.get("selection") or "")
        if not book or not selection:
            continue
        if allowed is not None and book not in allowed:
            continue
        if book in excluded or (allowed is None and "_ex_" in book):
            continue
        try:
            price = float(r.get("price"))
        except Exception:
            continue
        if price <= 1.0:
            continue
        outcomes[book].add(selection)
        grouped[book][selection] = price
    required = {str(home_team), str(away_team)}
    return {
        book: {side: float(prices[side]) for side in required}
        for book, prices in grouped.items()
        if outcomes[book] == required and all(side in prices for side in required)
    }


def _market_metrics(book_prices: Mapping[str, Mapping[str, float]], sides: Sequence[str]) -> Optional[Dict[str, Any]]:
    sides = tuple(str(x) for x in sides)
    per_side_probs: Dict[str, List[float]] = {s: [] for s in sides}
    per_side_odds: Dict[str, List[float]] = {s: [] for s in sides}
    overrounds: List[float] = []
    used = 0
    for prices in book_prices.values():
        if not all(s in prices and float(prices[s]) > 1.0 for s in sides):
            continue
        exact = {s: float(prices[s]) for s in sides}
        try:
            probs = devig_prices(exact)
        except Exception:
            continue
        used += 1
        overrounds.append((sum(1.0 / exact[s] for s in sides) - 1.0) * 100.0)
        for s in sides:
            per_side_probs[s].append(float(probs[s]))
            per_side_odds[s].append(float(exact[s]))
    if not used:
        return None
    consensus = {s: mean(per_side_probs[s]) for s in sides}
    total = sum(consensus.values())
    if total <= 0:
        return None
    consensus = {s: consensus[s] / total for s in sides}
    result: Dict[str, Any] = {
        "num_books": used,
        "mean_overround_pct": mean(overrounds) if overrounds else None,
        "sides": {},
    }
    for s in sides:
        odds = per_side_odds[s]
        med = median(odds) if odds else None
        result["sides"][s] = {
            "fair_probability": consensus[s],
            "fair_odds": fair_odds(consensus[s]),
            "median_reference_odds": med,
            "best_reference_odds": max(odds) if odds else None,
            "price_dispersion_pct": ((max(odds) - min(odds)) / med * 100.0) if odds and med and med > 0 else None,
        }
    return result


def _next_event_minutes(db: Database, sport_key: str, now: datetime) -> Optional[float]:
    rows = db.fetchall(
        "SELECT commence_time FROM multisport_events WHERE sport_key=? AND status='UPCOMING' ORDER BY commence_time ASC",
        (sport_key,),
    )
    vals = []
    for r in rows:
        try:
            mins = (parse_iso(r["commence_time"]) - now).total_seconds() / 60.0
        except Exception:
            continue
        if mins > 0:
            vals.append(mins)
    return min(vals) if vals else None


def _broad_interval_minutes(next_mins: Optional[float]) -> int:
    if next_mins is None: return 360
    if next_mins <= 30: return 10
    if next_mins <= 90: return 20
    if next_mins <= 360: return 60
    if next_mins <= 1440: return 180
    return 360


def _convergence_interval_minutes(next_mins: Optional[float]) -> int:
    if next_mins is None: return 180
    if next_mins <= 30: return 5
    if next_mins <= 90: return 10
    if next_mins <= 360: return 20
    if next_mins <= 1440: return 60
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


def _insert_payload(db: Database, *, sport_key: str, league_title: str, payload: Sequence[Mapping[str, Any]], capture_mode: str, captured_at: str) -> Tuple[int, List[str]]:
    family = sport_family(sport_key)
    rows = []
    event_ids: List[str] = []
    captured_dt = parse_iso(captured_at)
    for event in payload:
        event_id = str(event.get("id") or "")
        commence = str(event.get("commence_time") or "")
        home = str(event.get("home_team") or "")
        away = str(event.get("away_team") or "")
        if not event_id or not commence or not home or not away:
            continue
        try:
            if parse_iso(commence) <= captured_dt:
                if not db.fetchone("SELECT event_id FROM multisport_events WHERE event_id=?", (event_id,)):
                    continue
        except Exception:
            continue
        db.execute(
            """
            INSERT INTO multisport_events(event_id,sport_key,league_title,sport_family,commence_time,home_team,away_team,first_seen_at,last_seen_at,status)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET sport_key=excluded.sport_key,league_title=excluded.league_title,sport_family=excluded.sport_family,commence_time=excluded.commence_time,home_team=excluded.home_team,away_team=excluded.away_team,last_seen_at=excluded.last_seen_at
            """,
            (event_id,sport_key,league_title,family,commence,home,away,captured_at,captured_at,"UPCOMING"),
        )
        event_ids.append(event_id)
        for book in event.get("bookmakers") or []:
            for market in book.get("markets") or []:
                if str(market.get("key") or "") != "h2h":
                    continue
                for outcome in market.get("outcomes") or []:
                    selection = str(outcome.get("name") or "")
                    try: price = float(outcome.get("price"))
                    except Exception: continue
                    if selection and price > 1.0:
                        rows.append((event_id,sport_key,captured_at,capture_mode,str(book.get("key") or ""),str(book.get("title") or book.get("key") or ""),book.get("last_update"),"h2h",selection,price))
    if rows:
        db.executemany(
            """
            INSERT INTO multisport_odds_snapshots(event_id,sport_key,captured_at,capture_mode,bookmaker_key,bookmaker_title,bookmaker_last_update,market_key,selection,price)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
    return len(rows), event_ids


def write_multisport_consensus(db: Database, event_id: str, captured_at: str, *, min_books: int, excluded_books: Sequence[str]) -> int:
    event = db.fetchone("SELECT * FROM multisport_events WHERE event_id=?", (event_id,))
    if not event:
        return 0
    rows = db.fetchall(
        "SELECT * FROM multisport_odds_snapshots WHERE event_id=? AND captured_at=? AND capture_mode='BREADTH' AND market_key='h2h' ORDER BY id",
        (event_id,captured_at),
    )
    grouped = _book_waves(rows,event["home_team"],event["away_team"],excluded_books=excluded_books)
    metrics = _market_metrics(grouped,(event["home_team"],event["away_team"]))
    if not metrics or int(metrics["num_books"]) < int(min_books):
        return 0
    written = 0
    for selection in (event["home_team"],event["away_team"]):
        m = metrics["sides"][selection]
        db.execute(
            """
            INSERT INTO multisport_consensus_snapshots(event_id,sport_key,captured_at,selection,fair_probability,fair_odds,num_books,median_reference_odds,best_reference_odds,price_dispersion_pct,mean_overround_pct,model)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id,captured_at,selection) DO NOTHING
            """,
            (event_id,event["sport_key"],captured_at,selection,m["fair_probability"],m["fair_odds"],metrics["num_books"],m["median_reference_odds"],m["best_reference_odds"],m["price_dispersion_pct"],metrics["mean_overround_pct"],MARKET_MODEL),
        )
        written += 1
    return written


def _latest_consensus(db: Database, event_id: str, selection: str, evaluated_at: datetime) -> Optional[Dict[str, Any]]:
    return db.fetchone(
        "SELECT * FROM multisport_consensus_snapshots WHERE event_id=? AND selection=? AND captured_at<=? ORDER BY captured_at DESC,id DESC LIMIT 1",
        (event_id,selection,evaluated_at.isoformat()),
    )


def _record_eval(db: Database, *, evaluated_at: str, event_id: str, sport_key: str, selection: str, approved_books_seen: int, valid_two_way_books_seen: int, best_executable_odds: Optional[float], min_required_odds: Optional[float], fair_probability: Optional[float], fair_odds_value: Optional[float], edge: Optional[float], consensus_captured_at: Optional[str], consensus_age_minutes: Optional[float], decision: str, reason: str, metadata: Optional[Mapping[str, Any]] = None) -> None:
    if db.fetchone(
        "SELECT id FROM multisport_execution_evaluations WHERE evaluated_at=? AND event_id=? AND selection=? AND decision=? AND reason=? LIMIT 1",
        (evaluated_at,event_id,selection,decision,reason),
    ):
        return
    db.execute(
        """
        INSERT INTO multisport_execution_evaluations(evaluated_at,event_id,sport_key,selection,approved_books_seen,valid_two_way_books_seen,best_executable_odds,min_required_odds,fair_probability,fair_odds,edge_pct,consensus_captured_at,consensus_age_minutes,decision,reason,metadata_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (evaluated_at,event_id,sport_key,selection,approved_books_seen,valid_two_way_books_seen,best_executable_odds,min_required_odds,fair_probability,fair_odds_value,edge,consensus_captured_at,consensus_age_minutes,decision,reason,json.dumps(dict(metadata or {}),separators=(",",":"))),
    )


def evaluate_multisport_convergence_wave(db: Database, *, sport_key: str, captured_at: str, execution_bookmaker_keys: Sequence[str], min_edge_pct: float, max_consensus_age_minutes: int, config_hash: str) -> int:
    evaluated_at = parse_iso(captured_at)
    events = db.fetchall(
        """
        SELECT DISTINCT e.* FROM multisport_events e JOIN multisport_odds_snapshots o ON o.event_id=e.event_id
        WHERE o.sport_key=? AND o.captured_at=? AND o.capture_mode='CONVERGENCE' AND e.status='UPCOMING' ORDER BY e.commence_time
        """,
        (sport_key,captured_at),
    )
    approved = tuple(str(x) for x in execution_bookmaker_keys)
    approved_set = set(approved)
    created = 0
    for event in events:
        try:
            if parse_iso(event["commence_time"]) <= evaluated_at:
                continue
        except Exception:
            continue
        quote_rows = db.fetchall(
            "SELECT * FROM multisport_odds_snapshots WHERE event_id=? AND captured_at=? AND capture_mode='CONVERGENCE' AND market_key='h2h' ORDER BY id",
            (event["event_id"],captured_at),
        )
        approved_seen = {str(q["bookmaker_key"]) for q in quote_rows if str(q["bookmaker_key"]) in approved_set}
        valid_books = _book_waves(quote_rows,event["home_team"],event["away_team"],allowed_books=approved)
        for selection in (event["home_team"],event["away_team"]):
            consensus = _latest_consensus(db,event["event_id"],selection,evaluated_at)
            valid_quotes = []
            for book in valid_books:
                row = next((q for q in quote_rows if str(q["bookmaker_key"])==book and str(q["selection"])==selection),None)
                if row: valid_quotes.append(row)
            if not consensus:
                _record_eval(db,evaluated_at=captured_at,event_id=event["event_id"],sport_key=sport_key,selection=selection,approved_books_seen=len(approved_seen),valid_two_way_books_seen=len(valid_books),best_executable_odds=max([float(q["price"]) for q in valid_quotes],default=None),min_required_odds=None,fair_probability=None,fair_odds_value=None,edge=None,consensus_captured_at=None,consensus_age_minutes=None,decision="REJECT",reason="NO_REFERENCE_CONSENSUS")
                continue
            age = max(0.0,(evaluated_at-parse_iso(consensus["captured_at"])).total_seconds()/60.0)
            prob = float(consensus["fair_probability"])
            f_odds = float(consensus["fair_odds"])
            min_odds = min_odds_for_probability(prob,min_edge_pct)
            if age > float(max_consensus_age_minutes):
                _record_eval(db,evaluated_at=captured_at,event_id=event["event_id"],sport_key=sport_key,selection=selection,approved_books_seen=len(approved_seen),valid_two_way_books_seen=len(valid_books),best_executable_odds=max([float(q["price"]) for q in valid_quotes],default=None),min_required_odds=min_odds,fair_probability=prob,fair_odds_value=f_odds,edge=None,consensus_captured_at=consensus["captured_at"],consensus_age_minutes=age,decision="REJECT",reason="STALE_REFERENCE_CONSENSUS")
                continue
            if not approved_seen:
                reason = "NO_APPROVED_VENUE_QUOTE"
            elif not valid_books:
                reason = "APPROVED_VENUE_NOT_TWO_WAY"
            elif not valid_quotes:
                reason = "NO_VALID_SELECTION_QUOTE"
            else:
                reason = ""
            if reason:
                _record_eval(db,evaluated_at=captured_at,event_id=event["event_id"],sport_key=sport_key,selection=selection,approved_books_seen=len(approved_seen),valid_two_way_books_seen=len(valid_books),best_executable_odds=None,min_required_odds=min_odds,fair_probability=prob,fair_odds_value=f_odds,edge=None,consensus_captured_at=consensus["captured_at"],consensus_age_minutes=age,decision="REJECT",reason=reason)
                continue
            best = max(valid_quotes,key=lambda q:(float(q["price"]),str(q["bookmaker_key"])))
            best_odds = float(best["price"])
            e = edge_pct(prob,best_odds)
            execution_key = f"{event['event_id']}|h2h|{selection}"
            if db.fetchone("SELECT id FROM multisport_execution_bets WHERE execution_key=?",(execution_key,)):
                _record_eval(db,evaluated_at=captured_at,event_id=event["event_id"],sport_key=sport_key,selection=selection,approved_books_seen=len(approved_seen),valid_two_way_books_seen=len(valid_books),best_executable_odds=best_odds,min_required_odds=min_odds,fair_probability=prob,fair_odds_value=f_odds,edge=e,consensus_captured_at=consensus["captured_at"],consensus_age_minutes=age,decision="TRACK",reason="EXECUTION_ALREADY_FROZEN")
                continue
            if best_odds + 1e-12 < min_odds:
                _record_eval(db,evaluated_at=captured_at,event_id=event["event_id"],sport_key=sport_key,selection=selection,approved_books_seen=len(approved_seen),valid_two_way_books_seen=len(valid_books),best_executable_odds=best_odds,min_required_odds=min_odds,fair_probability=prob,fair_odds_value=f_odds,edge=e,consensus_captured_at=consensus["captured_at"],consensus_age_minutes=age,decision="REJECT",reason="EXECUTABLE_PRICE_BELOW_MIN")
                continue
            db.execute(
                """
                INSERT INTO multisport_execution_bets(execution_key,created_at,event_id,sport_key,selection,bookmaker_key,bookmaker_title,offered_odds,fair_probability,fair_odds,edge_pct,min_odds,approved_books_seen,consensus_captured_at,consensus_age_minutes,reference_book_count,reference_median_odds,reference_best_odds,reference_dispersion_pct,reference_mean_overround_pct,status,app_version,experiment_version,config_hash)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (execution_key,captured_at,event["event_id"],sport_key,selection,best["bookmaker_key"],best["bookmaker_title"],best_odds,prob,f_odds,e,min_odds,len(approved_seen),consensus["captured_at"],age,int(consensus["num_books"]),consensus.get("median_reference_odds"),consensus.get("best_reference_odds"),consensus.get("price_dispersion_pct"),consensus.get("mean_overround_pct"),"OPEN",APP_VERSION,EXPERIMENT_VERSION,config_hash),
            )
            _record_eval(db,evaluated_at=captured_at,event_id=event["event_id"],sport_key=sport_key,selection=selection,approved_books_seen=len(approved_seen),valid_two_way_books_seen=len(valid_books),best_executable_odds=best_odds,min_required_odds=min_odds,fair_probability=prob,fair_odds_value=f_odds,edge=e,consensus_captured_at=consensus["captured_at"],consensus_age_minutes=age,decision="ACCEPT",reason="FIRST_ACCEPTABLE_APPROVED_TWO_WAY_PRICE",metadata={"bookmaker_key":best["bookmaker_key"]})
            created += 1
    return created


def _valid_quote_rows_for_book(db: Database, *, event: Mapping[str, Any], bookmaker_key: str, before_at: datetime, after_at: Optional[datetime] = None) -> List[Dict[str, Any]]:
    params: List[Any] = [event["event_id"],bookmaker_key,before_at.isoformat()]
    after_sql = ""
    if after_at is not None:
        after_sql = " AND captured_at>?"
        params.append(after_at.isoformat())
    rows = db.fetchall(
        f"SELECT * FROM multisport_odds_snapshots WHERE event_id=? AND capture_mode='CONVERGENCE' AND bookmaker_key=? AND captured_at<=? {after_sql} ORDER BY captured_at ASC,id ASC",
        tuple(params),
    )
    by_wave: Dict[str,List[Dict[str,Any]]] = defaultdict(list)
    for r in rows: by_wave[str(r["captured_at"])].append(r)
    valid_rows=[]
    for wave_rows in by_wave.values():
        valid = _book_waves(wave_rows,event["home_team"],event["away_team"],allowed_books=(bookmaker_key,))
        if bookmaker_key in valid: valid_rows.extend(wave_rows)
    return valid_rows


def track_multisport_prices(db: Database, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    bets = db.fetchall("SELECT b.*,e.commence_time,e.home_team,e.away_team FROM multisport_execution_bets b JOIN multisport_events e ON e.event_id=b.event_id ORDER BY b.id")
    inserted=0
    for bet in bets:
        start=parse_iso(bet["commence_time"])
        rows=_valid_quote_rows_for_book(db,event=bet,bookmaker_key=bet["bookmaker_key"],before_at=min(now,start),after_at=parse_iso(bet["created_at"]))
        for row in rows:
            if str(row["selection"]) != str(bet["selection"]): continue
            if db.fetchone("SELECT id FROM multisport_price_observations WHERE multisport_bet_id=? AND source_snapshot_at=?",(bet["id"],row["captured_at"])):
                continue
            move=(float(bet["offered_odds"])/float(row["price"])-1.0)*100.0
            db.execute("INSERT INTO multisport_price_observations(multisport_bet_id,observed_at,source_snapshot_at,bookmaker_key,price,move_vs_entry_pct) VALUES(?,?,?,?,?,?)",(bet["id"],utc_now_iso(),row["captured_at"],bet["bookmaker_key"],row["price"],move))
            inserted += 1
    return inserted


def finalize_multisport_clv(db: Database, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    bets = db.fetchall("SELECT b.*,e.commence_time,e.home_team,e.away_team FROM multisport_execution_bets b JOIN multisport_events e ON e.event_id=b.event_id WHERE b.clv_pct IS NULL OR b.clv_quality IS NULL ORDER BY b.id")
    updated=0
    for bet in bets:
        start=parse_iso(bet["commence_time"])
        if start > now: continue
        rows=_valid_quote_rows_for_book(db,event=bet,bookmaker_key=bet["bookmaker_key"],before_at=start)
        selection_rows=[r for r in rows if str(r["selection"])==str(bet["selection"])]
        if not selection_rows: continue
        close=max(selection_rows,key=lambda r:(parse_iso(r["captured_at"]),int(r["id"])))
        close_at=parse_iso(close["captured_at"])
        mins=max(0.0,(start-close_at).total_seconds()/60.0)
        closing=float(close["price"])
        consensus=db.fetchone("SELECT * FROM multisport_consensus_snapshots WHERE event_id=? AND selection=? AND captured_at<=? ORDER BY captured_at DESC,id DESC LIMIT 1",(bet["event_id"],bet["selection"],start.isoformat()))
        c_odds=c_at=c_mins=c_quality=c_clv=None
        if consensus:
            c_odds=float(consensus["fair_odds"]);c_at=consensus["captured_at"]
            c_mins=max(0.0,(start-parse_iso(c_at)).total_seconds()/60.0)
            c_quality=clv_quality(c_mins);c_clv=clv_pct(float(bet["offered_odds"]),c_odds)
        db.execute("UPDATE multisport_execution_bets SET closing_odds=?,clv_pct=?,closing_observed_at=?,closing_minutes_before_start=?,clv_quality=?,closing_consensus_fair_odds=?,closing_consensus_observed_at=?,closing_consensus_minutes_before_start=?,closing_consensus_quality=?,clv_vs_consensus_pct=? WHERE id=?",(closing,clv_pct(float(bet["offered_odds"]),closing),close["captured_at"],mins,clv_quality(mins),c_odds,c_at,c_mins,c_quality,c_clv,bet["id"]))
        updated += 1
    return updated


def _afl_dead_heat_gross(offered_odds: float) -> float:
    return float(offered_odds) / 2.0 - 1.0


def _settlement_metadata(
    event: Mapping[str, Any],
    bet: Mapping[str, Any],
    *,
    tie: bool,
) -> Tuple[str, str]:
    family=str(event.get("sport_family") or "")
    book=str(bet.get("bookmaker_key") or "")
    if family=="AUSSIE_RULES" and tie:
        if book in VERIFIED_AFL_DEAD_HEAT_BOOKS:
            return (
                "VERIFIED_VENUE_RULE",
                "AFL tied match settled using verified two-runner dead-heat "
                "calculation for this execution venue.",
            )
        return (
            "UNVERIFIED_VENUE_RULE",
            "AFL tied match: execution venue dead-heat treatment is not "
            "independently verified; excluded from headline MSP1 P&L/ROI.",
        )
    if family=="BASEBALL":
        if book=="smarkets":
            return (
                "VERIFIED_SCORE_RULE",
                "Baseball final-score settlement; Smarkets action stands "
                "regardless of listed starting pitcher.",
            )
        return (
            "RULE_SENSITIVE",
            "Baseball final-score research settlement; listed-pitcher/void "
            "conditions are not encoded in provider quote metadata.",
        )
    return (
        "PROVIDER_COMPLETED",
        "Provider-completed final score; no known venue-specific settlement "
        "exception applied by MSP1.",
    )


def settle_multisport_event(db: Database, event_id: str, *, home_score: int, away_score: int) -> int:
    event=db.fetchone("SELECT * FROM multisport_events WHERE event_id=?",(event_id,))
    if not event: return 0
    tie=int(home_score)==int(away_score)
    winner=None if tie else (
        event["home_team"] if int(home_score)>int(away_score)
        else event["away_team"]
    )
    bets=db.fetchall(
        "SELECT * FROM multisport_execution_bets "
        "WHERE event_id=? AND status='OPEN' ORDER BY id",(event_id,)
    )
    settled=0
    for bet in bets:
        quality,provenance=_settlement_metadata(event,bet,tie=tie)
        family=str(event.get("sport_family") or "")
        book=str(bet.get("bookmaker_key") or "")
        if tie and family=="AUSSIE_RULES":
            if book in VERIFIED_AFL_DEAD_HEAT_BOOKS:
                result="DEAD_HEAT"
                gross=_afl_dead_heat_gross(float(bet["offered_odds"]))
            else:
                result="DEAD_HEAT_UNVERIFIED"
                gross=0.0
        elif tie:
            result="PUSH";gross=0.0
        else:
            result="WIN" if str(bet["selection"])==str(winner) else "LOSS"
            gross=float(bet["offered_odds"])-1.0 if result=="WIN" else -1.0
        rate,commission,net=commission_adjusted_pnl(
            gross,str(bet["bookmaker_key"])
        )
        db.execute(
            """UPDATE multisport_execution_bets
               SET status='SETTLED',result=?,pnl_units=?,
                   commission_rate_pct=?,commission_units=?,net_pnl_units=?,
                   settled_at=?,settlement_quality=?,settlement_provenance=?
               WHERE id=?""",
            (
                result,gross,rate,commission,net,utc_now_iso(),
                quality,provenance,bet["id"],
            ),
        )
        settled += 1
    return settled


def _probability_calibration(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    usable=[]
    for r in rows:
        if r.get("result") not in {"WIN","LOSS"}: continue
        p=float(r["fair_probability"])
        if not 0 < p < 1: continue
        usable.append((p,1.0 if r["result"]=="WIN" else 0.0))
    if not usable:
        return {"samples":0,"expected_win_pct":None,"observed_win_pct":None,"calibration_gap_pp":None,"brier_score":None,"log_loss":None}
    eps=1e-12;exp=mean(p for p,_ in usable);obs=mean(y for _,y in usable)
    return {"samples":len(usable),"expected_win_pct":exp*100.0,"observed_win_pct":obs*100.0,"calibration_gap_pp":(obs-exp)*100.0,"brier_score":mean((p-y)**2 for p,y in usable),"log_loss":-mean(y*math.log(max(eps,min(1-eps,p)))+(1-y)*math.log(max(eps,min(1-eps,1-p))) for p,y in usable)}


def _segment_rows(rows: Sequence[Mapping[str, Any]], label_fn) -> List[Dict[str, Any]]:
    groups: Dict[str,List[Mapping[str,Any]]] = defaultdict(list)
    for r in rows: groups[str(label_fn(r))].append(r)
    out=[]
    for label,items in sorted(groups.items()):
        settled=[x for x in items if x.get("pnl_units") is not None and x.get("settlement_quality")!="UNVERIFIED_VENUE_RULE"]
        headline=[x for x in items if x.get("clv_pct") is not None and is_headline_clv_quality(x.get("clv_quality"))]
        clvs=[float(x["clv_pct"]) for x in headline]
        net=sum(float(x.get("net_pnl_units") if x.get("net_pnl_units") is not None else x.get("pnl_units") or 0.0) for x in settled)
        out.append({"label":label,"bets":len(items),"settled":len(settled),"net_pnl_units":net,"net_roi_pct":(net/len(settled)*100.0) if settled else None,"clv_samples":len(clvs),"avg_clv_pct":mean(clvs) if clvs else None,"median_clv_pct":median(clvs) if clvs else None,"beat_close_pct":sum(1 for x in clvs if x>0)/len(clvs)*100.0 if clvs else None})
    return out


def multisport_scoreboard(db: Database) -> Dict[str, Any]:
    rows=db.fetchall("SELECT b.*,e.sport_family,e.league_title,e.home_team,e.away_team,e.commence_time FROM multisport_execution_bets b JOIN multisport_events e ON e.event_id=b.event_id ORDER BY b.created_at,b.id")
    all_settled=[r for r in rows if r.get("pnl_units") is not None]
    settled=[r for r in all_settled if r.get("settlement_quality")!="UNVERIFIED_VENUE_RULE"]
    headline=[r for r in rows if r.get("clv_pct") is not None and is_headline_clv_quality(r.get("clv_quality"))]
    clvs=[float(r["clv_pct"]) for r in headline]
    all_clv=[float(r["clv_pct"]) for r in rows if r.get("clv_pct") is not None]
    gross=sum(float(r.get("pnl_units") or 0.0) for r in settled)
    net=sum(float(r.get("net_pnl_units") if r.get("net_pnl_units") is not None else r.get("pnl_units") or 0.0) for r in settled)
    commission=sum(float(r.get("commission_units") or 0.0) for r in settled)
    wins=sum(1 for r in settled if r.get("result")=="WIN")
    pushes=sum(1 for r in settled if r.get("result")=="PUSH")
    qcounts={q:0 for q in ("A","B","C","STALE")}
    for r in rows:
        if r.get("clv_quality") in qcounts and r.get("clv_pct") is not None: qcounts[r["clv_quality"]]+=1
    decision_settled=[r for r in settled if r.get("result") in {"WIN","LOSS"}]
    return {"events":int((db.fetchone("SELECT COUNT(*) AS n FROM multisport_events") or {}).get("n") or 0),"active_leagues":int((db.fetchone("SELECT COUNT(*) AS n FROM multisport_league_state WHERE active=1") or {}).get("n") or 0),"consensus_rows":int((db.fetchone("SELECT COUNT(*) AS n FROM multisport_consensus_snapshots") or {}).get("n") or 0),"evaluations":int((db.fetchone("SELECT COUNT(*) AS n FROM multisport_execution_evaluations") or {}).get("n") or 0),"bets":len(rows),"settled":len(settled),"all_settled":len(all_settled),"settlement_excluded":len(all_settled)-len(settled),"wins":wins,"pushes":pushes,"gross_pnl_units":gross,"net_pnl_units":net,"commission_units":commission,"gross_roi_pct":gross/len(settled)*100.0 if settled else None,"net_roi_pct":net/len(settled)*100.0 if settled else None,"win_rate_pct":wins/len(decision_settled)*100.0 if decision_settled else None,"clv_samples":len(clvs),"avg_clv_pct":mean(clvs) if clvs else None,"median_clv_pct":median(clvs) if clvs else None,"beat_close_pct":sum(1 for x in clvs if x>0)/len(clvs)*100.0 if clvs else None,"all_clv_samples":len(all_clv),"all_avg_clv_pct":mean(all_clv) if all_clv else None,"clv_quality_counts":qcounts,"calibration":_probability_calibration(settled)}


def multisport_funnel(db: Database) -> Dict[str, Any]:
    rows = db.fetchall(
        """SELECT decision,reason,COUNT(*) AS n
           FROM multisport_execution_evaluations
           GROUP BY decision,reason ORDER BY n DESC"""
    )
    return {
        "rows": rows,
        "by_reason": {
            r["reason"]: int(r["n"])
            for r in db.fetchall(
                """SELECT reason,COUNT(*) AS n
                   FROM multisport_execution_evaluations
                   GROUP BY reason ORDER BY n DESC"""
            )
        },
    }


def multisport_segments(db: Database) -> Dict[str, Any]:
    rows=db.fetchall("SELECT b.*,e.sport_family,e.league_title,e.home_team,e.away_team FROM multisport_execution_bets b JOIN multisport_events e ON e.event_id=b.event_id ORDER BY b.id")
    def band(r):
        o=float(r["offered_odds"])
        if o < 1.5:return "<1.5"
        if o < 2.0:return "1.5-2"
        if o < 3.0:return "2-3"
        if o < 5.0:return "3-5"
        return "5+"
    return {"sport":_segment_rows(rows,lambda r:r["sport_family"]),"league":_segment_rows(rows,lambda r:r["league_title"]),"odds_band":_segment_rows(rows,band),"side":_segment_rows(rows,lambda r:"FAVOURITE" if float(r["fair_probability"])>=0.5 else "OUTSIDER"),"venue":_segment_rows(rows,lambda r:r["bookmaker_key"])}


def latest_multisport_bets(db: Database, limit: int = 100) -> List[Dict[str, Any]]:
    return db.fetchall("""
        SELECT b.*,e.sport_family,e.league_title,e.home_team,e.away_team,e.commence_time,
               (SELECT p.move_vs_entry_pct FROM multisport_price_observations p WHERE p.multisport_bet_id=b.id ORDER BY p.source_snapshot_at DESC LIMIT 1) AS latest_move_pct
        FROM multisport_execution_bets b JOIN multisport_events e ON e.event_id=b.event_id ORDER BY b.id DESC LIMIT ?
    """,(limit,))


class MultiSportShadowEngine:
    def __init__(self, db: Database, api, settings, *, execution_bookmaker_keys: Sequence[str]):
        self.db=db;self.api=api;self.settings=settings
        self.execution_bookmaker_keys=tuple(str(x) for x in execution_bookmaker_keys)
        self.target_sport_keys=tuple(str(x) for x in settings.multisport_sport_keys if str(x))
        self.quota=MultiSportQuotaGuard(db,daily_budget=settings.multisport_daily_paid_credit_budget,reserve=settings.multisport_quota_reserve_credits)
        self.config_hash=_safe_hash(settings,self.execution_bookmaker_keys)
        self._last_discovery_at: Optional[datetime]=None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.multisport_shadow_enabled)

    def discover_active_leagues(self, now: Optional[datetime]=None) -> int:
        if not self.enabled: return 0
        now=now or datetime.now(timezone.utc)
        result=self.api.sports();self.quota.update(remaining=result.remaining,used=result.used,last_cost=result.last_cost)
        sports=result.data if isinstance(result.data,list) else []
        target=set(self.target_sport_keys);active=[];stamp=now.isoformat()
        found={str(item.get("key") or ""):item for item in sports}
        for key in self.target_sport_keys:
            item=found.get(key);is_active=bool(item and item.get("active",True))
            title=str((item or {}).get("title") or key);group_name=str((item or {}).get("group") or sport_family(key));family=sport_family(key)
            self.db.execute("""
                INSERT INTO multisport_league_state(sport_key,title,group_name,sport_family,active,targeted,first_seen_at,last_seen_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(sport_key) DO UPDATE SET title=excluded.title,group_name=excluded.group_name,sport_family=excluded.sport_family,active=excluded.active,targeted=1,last_seen_at=excluded.last_seen_at
            """,(key,title,group_name,family,1 if is_active else 0,1,stamp,stamp))
            if is_active: active.append(key)
        self.db.record_collector_run("MULTISPORT_DISCOVERY",True,requested_markets="sports",actual_cost=provider_actual_cost(result.last_cost, 0),detail=f"targeted={len(self.target_sport_keys)}; active={len(active)}; provider_call_cost=0")
        self._last_discovery_at=now
        return len(active)

    def _maybe_discover(self,now:datetime)->None:
        interval=max(300,int(self.settings.multisport_discovery_interval_seconds))
        if self._last_discovery_at is None:
            row=self.db.fetchone("SELECT started_at FROM collector_runs WHERE run_type='MULTISPORT_DISCOVERY' AND ok=1 ORDER BY id DESC LIMIT 1")
            if row:
                try:self._last_discovery_at=parse_iso(row["started_at"])
                except Exception:self._last_discovery_at=None
        if self._last_discovery_at is None or (now-self._last_discovery_at).total_seconds()>=interval:
            try:self.discover_active_leagues(now)
            except Exception as exc:
                self.db.record_collector_run("MULTISPORT_DISCOVERY",False,detail=f"error={sanitize_sensitive_text(exc)}");self._last_discovery_at=now

    def _due_lanes(self,now:datetime)->List[Dict[str,Any]]:
        rows=self.db.fetchall("SELECT * FROM multisport_league_state WHERE active=1 AND targeted=1 ORDER BY sport_key")
        due=[]
        for row in rows:
            next_mins=_next_event_minutes(self.db,row["sport_key"],now);b_int=_broad_interval_minutes(next_mins);c_int=_convergence_interval_minutes(next_mins)
            if _is_due(row.get("last_broad_poll_at"),b_int,now):
                due.append({"mode":"breadth","sport_key":row["sport_key"],"title":row["title"],"lateness":_lateness(row.get("last_broad_poll_at"),b_int,now),"next_mins":next_mins})
            has_consensus=self.db.fetchone("SELECT id FROM multisport_consensus_snapshots WHERE sport_key=? LIMIT 1",(row["sport_key"],))
            if has_consensus and _is_due(row.get("last_convergence_poll_at"),c_int,now):
                due.append({"mode":"convergence","sport_key":row["sport_key"],"title":row["title"],"lateness":_lateness(row.get("last_convergence_poll_at"),c_int,now),"next_mins":next_mins})
        return sorted(due,key=lambda x:(10**9 if x["next_mins"] is None else max(0.0,float(x["next_mins"])),-float(x["lateness"]),0 if x["mode"]=="breadth" else 1,x["sport_key"]))

    def _poll_breadth(self,target:Mapping[str,Any],now:datetime)->Dict[str,Any]:
        allowed,reason=self.quota.decide(1)
        if not allowed:return {"mode":"breadth","polled":0,"reason":reason}
        sport_key=str(target["sport_key"]);title=str(target["title"]);actual=0
        try:
            reference_region=(
                self.settings.multisport_hockey_reference_region
                if sport_family(sport_key)=="ICE_HOCKEY"
                else self.settings.multisport_odds_region
            )
            result=self.api.sport_odds(
                sport_key,reference_region,(self.settings.multisport_market,)
            );actual=provider_actual_cost(result.last_cost, 1);self.quota.update(remaining=result.remaining,used=result.used,last_cost=result.last_cost)
            captured=now.isoformat();payload=result.data if isinstance(result.data,list) else []
            quote_rows,event_ids=_insert_payload(self.db,sport_key=sport_key,league_title=title,payload=payload,capture_mode="BREADTH",captured_at=captured)
            consensus=sum(write_multisport_consensus(self.db,eid,captured,min_books=self.settings.multisport_min_consensus_books,excluded_books=self.execution_bookmaker_keys) for eid in event_ids)
            self.db.execute("UPDATE multisport_league_state SET last_broad_poll_at=? WHERE sport_key=?",(captured,sport_key))
            self.db.record_collector_run("MULTISPORT_ODDS",True,sport_key=sport_key,requested_markets="h2h",estimated_cost=1,actual_cost=actual,detail=f"mode=breadth; region={reference_region}; events={len(event_ids)}; quote_rows={quote_rows}; consensus_rows={consensus}")
            return {"mode":"breadth","polled":1,"sport_key":sport_key,"reference_region":reference_region,"events":len(event_ids),"quote_rows":quote_rows,"consensus_rows":consensus,"cost":actual}
        except Exception as exc:
            self.db.record_collector_run("MULTISPORT_ODDS",False,sport_key=sport_key,requested_markets="h2h",estimated_cost=1,actual_cost=actual,detail=f"mode=breadth; error={sanitize_sensitive_text(exc)}")
            return {"mode":"breadth","polled":0,"sport_key":sport_key,"reason":"error"}

    def _poll_convergence(self,target:Mapping[str,Any],now:datetime)->Dict[str,Any]:
        allowed,reason=self.quota.decide(1)
        if not allowed:return {"mode":"convergence","polled":0,"reason":reason}
        sport_key=str(target["sport_key"]);title=str(target["title"]);actual=0
        try:
            result=self.api.sport_odds(sport_key,self.settings.multisport_odds_region,(self.settings.multisport_market,),bookmaker_keys=self.execution_bookmaker_keys);actual=provider_actual_cost(result.last_cost, 1);self.quota.update(remaining=result.remaining,used=result.used,last_cost=result.last_cost)
            captured=now.isoformat();payload=result.data if isinstance(result.data,list) else []
            quote_rows,event_ids=_insert_payload(self.db,sport_key=sport_key,league_title=title,payload=payload,capture_mode="CONVERGENCE",captured_at=captured)
            created=evaluate_multisport_convergence_wave(self.db,sport_key=sport_key,captured_at=captured,execution_bookmaker_keys=self.execution_bookmaker_keys,min_edge_pct=self.settings.multisport_min_edge_pct,max_consensus_age_minutes=self.settings.multisport_max_consensus_age_minutes,config_hash=self.config_hash)
            self.db.execute("UPDATE multisport_league_state SET last_convergence_poll_at=? WHERE sport_key=?",(captured,sport_key))
            self.db.record_collector_run("MULTISPORT_CONVERGENCE",True,sport_key=sport_key,requested_markets="h2h",estimated_cost=1,actual_cost=actual,detail=f"mode=convergence; events={len(event_ids)}; quote_rows={quote_rows}; executions_created={created}")
            return {"mode":"convergence","polled":1,"sport_key":sport_key,"events":len(event_ids),"quote_rows":quote_rows,"executions_created":created,"cost":actual}
        except Exception as exc:
            self.db.record_collector_run("MULTISPORT_CONVERGENCE",False,sport_key=sport_key,requested_markets="h2h",estimated_cost=1,actual_cost=actual,detail=f"mode=convergence; error={sanitize_sensitive_text(exc)}")
            return {"mode":"convergence","polled":0,"sport_key":sport_key,"reason":"error"}

    def collect_results(self,now:datetime)->Dict[str,Any]:
        open_events=self.db.fetchall("SELECT DISTINCT e.* FROM multisport_events e JOIN multisport_execution_bets b ON b.event_id=e.event_id LEFT JOIN multisport_results r ON r.event_id=e.event_id WHERE b.status='OPEN' AND r.event_id IS NULL ORDER BY e.commence_time")
        due=[]
        for e in open_events:
            try:
                if parse_iso(e["commence_time"])+timedelta(minutes=int(RESULT_DELAY_MINUTES.get(str(e["sport_family"]),240)))<=now: due.append(e)
            except Exception: pass
        if not due:return {"checked":0,"settled":0,"reason":"no_due_results"}
        grouped:Dict[str,List[Dict[str,Any]]]=defaultdict(list)
        for e in due:grouped[e["sport_key"]].append(e)
        checked=settled=0
        for sport_key,events in grouped.items():
            state=self.db.fetchone("SELECT * FROM multisport_league_state WHERE sport_key=?",(sport_key,)) or {};last=state.get("last_results_poll_at");interval=max(900,int(self.settings.multisport_result_poll_interval_seconds))
            if last:
                try:
                    if (now-parse_iso(last)).total_seconds()<interval:continue
                except Exception:pass
            allowed,reason=self.quota.decide(2)
            if not allowed:
                self.db.record_collector_run("MULTISPORT_RESULTS",False,sport_key=sport_key,requested_markets="scores",estimated_cost=2,actual_cost=0,detail=f"quota_block:{reason}");continue
            actual=0
            try:
                result=self.api.scores(sport_key,event_ids=[e["event_id"] for e in events],days_from=3);actual=provider_actual_cost(result.last_cost, 2);self.quota.update(remaining=result.remaining,used=result.used,last_cost=result.last_cost)
                payload=result.data if isinstance(result.data,list) else [];wanted={e["event_id"]:e for e in events};completed_count=0
                for item in payload:
                    eid=str(item.get("id") or "")
                    if eid not in wanted or not item.get("completed"):continue
                    score_map={}
                    for s in item.get("scores") or []:
                        try:score_map[str(s["name"])]=int(float(s["score"]))
                        except Exception:continue
                    e=wanted[eid];home=e["home_team"];away=e["away_team"]
                    if home not in score_map or away not in score_map:continue
                    hs=int(score_map[home]);as_=int(score_map[away]);tie=hs==as_
                    winner=None if tie else (home if hs>as_ else away)
                    kind="TIE" if tie else "WINNER"
                    provenance=(
                        "provider completed tied final score; bet-level venue "
                        "settlement rule applied separately"
                        if tie else "provider completed final score"
                    )
                    self.db.execute("""
                        INSERT INTO multisport_results(event_id,fetched_at,completed_at,home_score,away_score,winner,result_kind,source,settlement_quality,settlement_provenance,raw_json)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET fetched_at=excluded.fetched_at,completed_at=excluded.completed_at,home_score=excluded.home_score,away_score=excluded.away_score,winner=excluded.winner,result_kind=excluded.result_kind,source=excluded.source,settlement_quality=excluded.settlement_quality,settlement_provenance=excluded.settlement_provenance,raw_json=excluded.raw_json
                    """,(eid,utc_now_iso(),item.get("commence_time"),hs,as_,winner,kind,"the_odds_api","PROVIDER_COMPLETED",provenance,json.dumps(item,separators=(",",":"))))
                    self.db.execute("UPDATE multisport_events SET status='COMPLETED' WHERE event_id=?",(eid,));settled+=settle_multisport_event(self.db,eid,home_score=hs,away_score=as_);completed_count+=1
                checked+=len(events);self.db.execute("UPDATE multisport_league_state SET last_results_poll_at=? WHERE sport_key=?",(now.isoformat(),sport_key));self.db.record_collector_run("MULTISPORT_RESULTS",True,sport_key=sport_key,requested_markets="scores",estimated_cost=2,actual_cost=actual,detail=f"checked={len(events)}; completed_events={completed_count}; bets_settled={settled}")
            except Exception as exc:
                self.db.execute("UPDATE multisport_league_state SET last_results_poll_at=? WHERE sport_key=?",(now.isoformat(),sport_key));self.db.record_collector_run("MULTISPORT_RESULTS",False,sport_key=sport_key,requested_markets="scores",estimated_cost=2,actual_cost=actual,detail=f"error={sanitize_sensitive_text(exc)}")
        return {"checked":checked,"settled":settled,"reason":"ok"}

    def maintenance(self,now:Optional[datetime]=None)->Dict[str,int]:
        now=now or datetime.now(timezone.utc)
        return {"price_observations":track_multisport_prices(self.db,now),"clv_finalized":finalize_multisport_clv(self.db,now)}

    def one_cycle(self,now:Optional[datetime]=None)->Dict[str,Any]:
        if not self.enabled:return {"enabled":False,"mode":"disabled","polled":0}
        now=now or datetime.now(timezone.utc);self._maybe_discover(now);due=self._due_lanes(now);odds_result={"mode":"idle","polled":0,"reason":"no_due_multisport_lane"}
        if due:
            odds_result=self._poll_breadth(due[0],now) if due[0]["mode"]=="breadth" else self._poll_convergence(due[0],now)
        maint=self.maintenance(now);results=self.collect_results(now)
        return {"enabled":True,"odds":odds_result,"maintenance":maint,"results":results,"paid_credits_today":self.quota.today_paid_cost(now),"daily_budget":self.quota.daily_budget}
