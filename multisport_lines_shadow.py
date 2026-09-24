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
from multisport_shadow import (
    MultiSportQuotaGuard,
    RESULT_DELAY_MINUTES,
    _broad_interval_minutes,
    _convergence_interval_minutes,
    _is_due,
    _lateness,
    _next_event_minutes,
    parse_iso,
    sport_family,
)
from quota import provider_actual_cost

APP_VERSION = "0.9.0"
EXPERIMENT_VERSION = "MSP2_LINES"
SUPPORTED_MARKETS = {"spreads", "totals"}


def _norm_point(value: Any) -> Optional[float]:
    try:
        x = round(float(value), 3)
        if not math.isfinite(x):
            return None
        return x
    except Exception:
        return None


def _safe_hash(settings, execution_bookmaker_keys: Sequence[str]) -> str:
    payload = {
        "experiment": EXPERIMENT_VERSION,
        "target_sports": list(settings.multisport_sport_keys),
        "markets": list(settings.multisport_lines_markets),
        "min_consensus_books": int(settings.multisport_lines_min_consensus_books),
        "min_edge_pct": float(settings.multisport_lines_min_edge_pct),
        "max_consensus_age_minutes": int(settings.multisport_lines_max_consensus_age_minutes),
        "us_reference_region": str(settings.multisport_lines_us_reference_region),
        "au_reference_region": str(settings.multisport_lines_au_reference_region),
        "execution_bookmaker_keys": list(execution_bookmaker_keys),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def reference_region(settings, sport_key: str) -> str:
    family = sport_family(sport_key)
    if family in {"AUSSIE_RULES", "RUGBY_LEAGUE"}:
        return str(settings.multisport_lines_au_reference_region)
    return str(settings.multisport_lines_us_reference_region)


def _required_selections(event: Mapping[str, Any], market_key: str) -> Tuple[str, str]:
    if market_key == "spreads":
        return str(event["home_team"]), str(event["away_team"])
    return "Over", "Under"


def _canonicalize_market(
    event: Mapping[str, Any],
    market: Mapping[str, Any],
) -> Optional[Tuple[str, float, List[Tuple[str, float, float]]]]:
    key = str(market.get("key") or "")
    if key not in SUPPORTED_MARKETS:
        return None
    outcomes = list(market.get("outcomes") or [])
    if len(outcomes) != 2:
        return None

    if key == "spreads":
        home, away = str(event["home_team"]), str(event["away_team"])
        by_name = {str(o.get("name") or ""): o for o in outcomes}
        if set(by_name) != {home, away}:
            return None
        hp = _norm_point(by_name[home].get("point"))
        ap = _norm_point(by_name[away].get("point"))
        if hp is None or ap is None or abs(hp + ap) > 1e-6:
            return None
        rows: List[Tuple[str, float, float]] = []
        for name in (home, away):
            try:
                price = float(by_name[name].get("price"))
            except Exception:
                return None
            if price <= 1.0:
                return None
            rows.append((name, _norm_point(by_name[name].get("point")) or 0.0, price))
        return key, hp, rows

    by_name = {str(o.get("name") or ""): o for o in outcomes}
    if set(by_name) != {"Over", "Under"}:
        return None
    op = _norm_point(by_name["Over"].get("point"))
    up = _norm_point(by_name["Under"].get("point"))
    if op is None or up is None or abs(op - up) > 1e-6:
        return None
    rows = []
    for name in ("Over", "Under"):
        try:
            price = float(by_name[name].get("price"))
        except Exception:
            return None
        if price <= 1.0:
            return None
        rows.append((name, _norm_point(by_name[name].get("point")) or 0.0, price))
    return key, op, rows


def insert_line_payload(
    db: Database,
    *,
    sport_key: str,
    league_title: str,
    payload: Sequence[Mapping[str, Any]],
    capture_mode: str,
    captured_at: str,
) -> Tuple[int, List[str]]:
    family = sport_family(sport_key)
    out_rows: List[Tuple[Any, ...]] = []
    event_ids: List[str] = []
    captured_dt = parse_iso(captured_at)
    for event in payload:
        eid = str(event.get("id") or "")
        commence = str(event.get("commence_time") or "")
        home = str(event.get("home_team") or "")
        away = str(event.get("away_team") or "")
        if not eid or not commence or not home or not away:
            continue
        try:
            if parse_iso(commence) <= captured_dt and not db.fetchone(
                "SELECT event_id FROM multisport_events WHERE event_id=?", (eid,)
            ):
                continue
        except Exception:
            continue
        if not db.fetchone(
            "SELECT sport_key FROM multisport_league_state WHERE sport_key=?", (sport_key,)
        ):
            db.execute(
                """INSERT INTO multisport_league_state(
                       sport_key,title,group_name,sport_family,active,targeted,
                       first_seen_at,last_seen_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (sport_key,league_title,family,family,1,1,captured_at,captured_at),
            )
        db.execute(
            """
            INSERT INTO multisport_events(
                event_id,sport_key,league_title,sport_family,commence_time,
                home_team,away_team,first_seen_at,last_seen_at,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET
                sport_key=excluded.sport_key,league_title=excluded.league_title,
                sport_family=excluded.sport_family,commence_time=excluded.commence_time,
                home_team=excluded.home_team,away_team=excluded.away_team,
                last_seen_at=excluded.last_seen_at
            """,
            (eid, sport_key, league_title, family, commence, home, away,
             captured_at, captured_at, "UPCOMING"),
        )
        event_ids.append(eid)
        event_map = {"home_team": home, "away_team": away}
        for book in event.get("bookmakers") or []:
            for market in book.get("markets") or []:
                normalized = _canonicalize_market(event_map, market)
                if not normalized:
                    continue
                market_key, line_point, rows = normalized
                for selection, outcome_point, price in rows:
                    out_rows.append((
                        eid, sport_key, captured_at, capture_mode,
                        str(book.get("key") or ""),
                        str(book.get("title") or book.get("key") or ""),
                        book.get("last_update"), market_key, selection,
                        outcome_point, line_point, price,
                    ))
    if out_rows:
        db.executemany(
            """
            INSERT INTO multisport_line_odds_snapshots(
                event_id,sport_key,captured_at,capture_mode,bookmaker_key,
                bookmaker_title,bookmaker_last_update,market_key,selection,
                outcome_point,line_point,price
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            out_rows,
        )
    return len(out_rows), list(dict.fromkeys(event_ids))


def _valid_line_groups(
    rows: Sequence[Mapping[str, Any]],
    event: Mapping[str, Any],
    *,
    allowed_books: Optional[Sequence[str]] = None,
    excluded_books: Sequence[str] = (),
) -> Dict[Tuple[str, str, float], Dict[str, float]]:
    allowed = None if allowed_books is None else {str(x) for x in allowed_books if str(x)}
    excluded = {str(x) for x in excluded_books if str(x)}
    grouped: Dict[Tuple[str, str, float], Dict[str, float]] = defaultdict(dict)
    raw_names: Dict[Tuple[str, str, float], set[str]] = defaultdict(set)
    for r in rows:
        book = str(r.get("bookmaker_key") or "")
        market = str(r.get("market_key") or "")
        line = _norm_point(r.get("line_point"))
        sel = str(r.get("selection") or "")
        if not book or market not in SUPPORTED_MARKETS or line is None or not sel:
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
        k = (book, market, line)
        grouped[k][sel] = price
        raw_names[k].add(sel)
    valid: Dict[Tuple[str, str, float], Dict[str, float]] = {}
    for k, prices in grouped.items():
        _, market, _ = k
        required = set(_required_selections(event, market))
        if raw_names[k] == required and all(s in prices for s in required):
            valid[k] = {s: float(prices[s]) for s in required}
    return valid


def _metrics(book_prices: Mapping[str, Mapping[str, float]], sides: Sequence[str]) -> Optional[Dict[str, Any]]:
    probs_by_side = {s: [] for s in sides}
    odds_by_side = {s: [] for s in sides}
    overrounds: List[float] = []
    for prices in book_prices.values():
        if not all(s in prices and float(prices[s]) > 1.0 for s in sides):
            continue
        exact = {s: float(prices[s]) for s in sides}
        try:
            probs = devig_prices(exact)
        except Exception:
            continue
        overrounds.append((sum(1.0 / exact[s] for s in sides) - 1.0) * 100.0)
        for s in sides:
            probs_by_side[s].append(float(probs[s]))
            odds_by_side[s].append(exact[s])
    n = min((len(v) for v in probs_by_side.values()), default=0)
    if not n:
        return None
    consensus = {s: mean(probs_by_side[s]) for s in sides}
    z = sum(consensus.values())
    if z <= 0:
        return None
    consensus = {s: consensus[s] / z for s in sides}
    result: Dict[str, Any] = {"num_books": n, "mean_overround_pct": mean(overrounds) if overrounds else None, "sides": {}}
    for s in sides:
        odds = odds_by_side[s]
        med = median(odds)
        result["sides"][s] = {
            "fair_probability": consensus[s],
            "fair_odds": fair_odds(consensus[s]),
            "median_reference_odds": med,
            "best_reference_odds": max(odds),
            "price_dispersion_pct": ((max(odds)-min(odds))/med*100.0) if med else None,
        }
    return result


def write_line_consensus(
    db: Database,
    event_id: str,
    captured_at: str,
    *,
    min_books: int,
    excluded_books: Sequence[str],
) -> int:
    event = db.fetchone("SELECT * FROM multisport_events WHERE event_id=?", (event_id,))
    if not event:
        return 0
    rows = db.fetchall(
        """
        SELECT * FROM multisport_line_odds_snapshots
        WHERE event_id=? AND captured_at=? AND capture_mode='BREADTH'
        ORDER BY id
        """, (event_id, captured_at)
    )
    groups = _valid_line_groups(rows, event, excluded_books=excluded_books)
    by_line: Dict[Tuple[str, float], Dict[str, Dict[str, float]]] = defaultdict(dict)
    for (book, market, line), prices in groups.items():
        by_line[(market, line)][book] = prices
    written = 0
    for (market, line), books in by_line.items():
        sides = _required_selections(event, market)
        metrics = _metrics(books, sides)
        if not metrics or int(metrics["num_books"]) < int(min_books):
            continue
        for selection in sides:
            m = metrics["sides"][selection]
            db.execute(
                """
                INSERT INTO multisport_line_consensus_snapshots(
                    event_id,sport_key,captured_at,market_key,line_point,selection,
                    fair_probability,fair_odds,num_books,median_reference_odds,
                    best_reference_odds,price_dispersion_pct,mean_overround_pct
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_id,captured_at,market_key,line_point,selection) DO NOTHING
                """,
                (event_id, event["sport_key"], captured_at, market, line, selection,
                 m["fair_probability"], m["fair_odds"], metrics["num_books"],
                 m["median_reference_odds"], m["best_reference_odds"],
                 m["price_dispersion_pct"], metrics["mean_overround_pct"]),
            )
            written += 1
    return written


def _latest_consensus(db: Database, event_id: str, market: str, line: float, selection: str, at: datetime) -> Optional[Dict[str, Any]]:
    return db.fetchone(
        """
        SELECT * FROM multisport_line_consensus_snapshots
        WHERE event_id=? AND market_key=? AND line_point=? AND selection=?
          AND captured_at<=?
        ORDER BY captured_at DESC,id DESC LIMIT 1
        """, (event_id, market, line, selection, at.isoformat())
    )


def _record_eval(db: Database, **kw) -> None:
    db.execute(
        """
        INSERT INTO multisport_line_evaluations(
            evaluated_at,event_id,sport_key,market_key,selection,line_point,
            approved_books_seen,valid_books_seen,best_executable_odds,
            min_required_odds,fair_probability,fair_odds,edge_pct,
            consensus_captured_at,consensus_age_minutes,decision,reason,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (kw["evaluated_at"], kw["event_id"], kw["sport_key"], kw["market_key"],
         kw["selection"], kw.get("line_point"), kw.get("approved_books_seen",0),
         kw.get("valid_books_seen",0), kw.get("best_executable_odds"),
         kw.get("min_required_odds"), kw.get("fair_probability"), kw.get("fair_odds"),
         kw.get("edge_pct"), kw.get("consensus_captured_at"), kw.get("consensus_age_minutes"),
         kw["decision"], kw["reason"], json.dumps(kw.get("metadata") or {}, separators=(",", ":"))),
    )


def evaluate_line_wave(
    db: Database,
    *,
    sport_key: str,
    captured_at: str,
    execution_bookmaker_keys: Sequence[str],
    min_edge_pct: float,
    max_consensus_age_minutes: int,
    config_hash: str,
) -> int:
    at = parse_iso(captured_at)
    events = db.fetchall(
        """
        SELECT DISTINCT e.* FROM multisport_events e
        JOIN multisport_line_odds_snapshots o ON o.event_id=e.event_id
        WHERE o.sport_key=? AND o.captured_at=? AND o.capture_mode='CONVERGENCE'
          AND e.status='UPCOMING'
        ORDER BY e.commence_time
        """, (sport_key, captured_at)
    )
    approved = tuple(str(x) for x in execution_bookmaker_keys if str(x))
    created = 0
    for event in events:
        try:
            if parse_iso(event["commence_time"]) <= at:
                continue
        except Exception:
            continue
        rows = db.fetchall(
            "SELECT * FROM multisport_line_odds_snapshots WHERE event_id=? AND captured_at=? AND capture_mode='CONVERGENCE' ORDER BY id",
            (event["event_id"], captured_at),
        )
        approved_seen = {str(r["bookmaker_key"]) for r in rows if str(r["bookmaker_key"]) in set(approved)}
        groups = _valid_line_groups(rows, event, allowed_books=approved)
        by_market_line: Dict[Tuple[str,float], Dict[str,Dict[str,float]]] = defaultdict(dict)
        for (book, market, line), prices in groups.items():
            by_market_line[(market,line)][book] = prices

        for market in sorted(SUPPORTED_MARKETS):
            for selection in _required_selections(event, market):
                execution_key = f"{event['event_id']}|{market}|{selection}"
                if db.fetchone("SELECT id FROM multisport_line_bets WHERE execution_key=?", (execution_key,)):
                    _record_eval(db, evaluated_at=captured_at, event_id=event["event_id"], sport_key=sport_key,
                                 market_key=market, selection=selection, decision="TRACK", reason="EXECUTION_ALREADY_FROZEN",
                                 approved_books_seen=len(approved_seen), valid_books_seen=len(groups))
                    continue
                candidates: List[Dict[str,Any]] = []
                consensus_lines = 0
                for (mkt, line), books in by_market_line.items():
                    if mkt != market:
                        continue
                    consensus = _latest_consensus(db, event["event_id"], market, line, selection, at)
                    if not consensus:
                        continue
                    consensus_lines += 1
                    age = max(0.0, (at - parse_iso(consensus["captured_at"])).total_seconds()/60.0)
                    if age > float(max_consensus_age_minutes):
                        continue
                    valid_quotes = []
                    for book, prices in books.items():
                        if selection in prices:
                            valid_quotes.append((book, float(prices[selection])))
                    if not valid_quotes:
                        continue
                    best_book, best_odds = max(valid_quotes, key=lambda x: (x[1], x[0]))
                    prob = float(consensus["fair_probability"])
                    minimum = min_odds_for_probability(prob, min_edge_pct)
                    e = edge_pct(prob, best_odds)
                    candidates.append({"line":line,"book":best_book,"odds":best_odds,"prob":prob,
                                       "minimum":minimum,"edge":e,"consensus":consensus,"age":age,
                                       "num_valid_books":len(books)})
                if not candidates:
                    reason = "NO_EXACT_LINE_CONSENSUS" if by_market_line else "NO_APPROVED_LINE_QUOTE"
                    _record_eval(db, evaluated_at=captured_at, event_id=event["event_id"], sport_key=sport_key,
                                 market_key=market, selection=selection, decision="REJECT", reason=reason,
                                 approved_books_seen=len(approved_seen), valid_books_seen=len(groups),
                                 metadata={"consensus_lines_seen":consensus_lines})
                    continue
                best = max(candidates, key=lambda x: (x["edge"], x["odds"]))
                if best["odds"] + 1e-12 < best["minimum"]:
                    _record_eval(db, evaluated_at=captured_at, event_id=event["event_id"], sport_key=sport_key,
                                 market_key=market, selection=selection, line_point=best["line"],
                                 approved_books_seen=len(approved_seen), valid_books_seen=best["num_valid_books"],
                                 best_executable_odds=best["odds"], min_required_odds=best["minimum"],
                                 fair_probability=best["prob"], fair_odds=best["consensus"]["fair_odds"],
                                 edge_pct=best["edge"], consensus_captured_at=best["consensus"]["captured_at"],
                                 consensus_age_minutes=best["age"], decision="REJECT", reason="EXECUTABLE_PRICE_BELOW_MIN",
                                 metadata={"candidate_lines":len(candidates)})
                    continue
                book_row = next((r for r in rows if str(r["bookmaker_key"])==best["book"] and str(r["market_key"])==market and str(r["selection"])==selection and abs(float(r["line_point"])-best["line"])<1e-9), None)
                if not book_row:
                    continue
                c = best["consensus"]
                db.execute(
                    """
                    INSERT INTO multisport_line_bets(
                        execution_key,created_at,event_id,sport_key,market_key,selection,line_point,
                        bookmaker_key,bookmaker_title,offered_odds,fair_probability,fair_odds,
                        edge_pct,min_odds,approved_books_seen,consensus_captured_at,consensus_age_minutes,
                        reference_book_count,reference_median_odds,reference_best_odds,
                        reference_dispersion_pct,reference_mean_overround_pct,status,
                        app_version,experiment_version,config_hash
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (execution_key,captured_at,event["event_id"],sport_key,market,selection,best["line"],
                     best["book"],book_row["bookmaker_title"],best["odds"],best["prob"],float(c["fair_odds"]),
                     best["edge"],best["minimum"],len(approved_seen),c["captured_at"],best["age"],
                     int(c["num_books"]),c.get("median_reference_odds"),c.get("best_reference_odds"),
                     c.get("price_dispersion_pct"),c.get("mean_overround_pct"),"OPEN",
                     APP_VERSION,EXPERIMENT_VERSION,config_hash),
                )
                _record_eval(db, evaluated_at=captured_at, event_id=event["event_id"], sport_key=sport_key,
                             market_key=market, selection=selection, line_point=best["line"],
                             approved_books_seen=len(approved_seen), valid_books_seen=best["num_valid_books"],
                             best_executable_odds=best["odds"], min_required_odds=best["minimum"],
                             fair_probability=best["prob"], fair_odds=c["fair_odds"], edge_pct=best["edge"],
                             consensus_captured_at=c["captured_at"], consensus_age_minutes=best["age"],
                             decision="ACCEPT", reason="FIRST_ACCEPTABLE_EXACT_LINE_PRICE",
                             metadata={"bookmaker_key":best["book"],"candidate_lines":len(candidates)})
                created += 1
    return created


def line_clv_points(market: str, selection: str, entry_line: float, close_line: float, event: Mapping[str, Any]) -> Optional[float]:
    if market == "spreads":
        if selection == str(event["home_team"]):
            return float(entry_line) - float(close_line)
        if selection == str(event["away_team"]):
            return float(close_line) - float(entry_line)
    elif market == "totals":
        if selection == "Over":
            return float(close_line) - float(entry_line)
        if selection == "Under":
            return float(entry_line) - float(close_line)
    return None


def _valid_book_quotes_by_wave(db: Database, bet: Mapping[str, Any], before_at: datetime, after_at: Optional[datetime]=None) -> List[Dict[str,Any]]:
    params: List[Any] = [bet["event_id"], bet["bookmaker_key"], bet["market_key"], before_at.isoformat()]
    extra = ""
    if after_at is not None:
        extra = " AND captured_at>?"
        params.append(after_at.isoformat())
    rows = db.fetchall(
        f"SELECT * FROM multisport_line_odds_snapshots WHERE event_id=? AND bookmaker_key=? AND market_key=? AND capture_mode='CONVERGENCE' AND captured_at<=? {extra} ORDER BY captured_at ASC,id ASC",
        tuple(params),
    )
    event = bet
    by_wave: Dict[str,List[Dict[str,Any]]] = defaultdict(list)
    for r in rows:
        by_wave[str(r["captured_at"])].append(r)
    chosen: List[Dict[str,Any]] = []
    for wave_rows in by_wave.values():
        groups = _valid_line_groups(wave_rows, event, allowed_books=(bet["bookmaker_key"],))
        for (book, market, line), prices in groups.items():
            if market != bet["market_key"] or bet["selection"] not in prices:
                continue
            row = next((r for r in wave_rows if r["bookmaker_key"]==book and r["market_key"]==market and r["selection"]==bet["selection"] and abs(float(r["line_point"])-line)<1e-9), None)
            if row:
                chosen.append(row)
    return chosen


def track_line_prices(db: Database, now: Optional[datetime]=None) -> int:
    now = now or datetime.now(timezone.utc)
    bets = db.fetchall(
        """
        SELECT b.*,e.commence_time,e.home_team,e.away_team,e.sport_family
        FROM multisport_line_bets b JOIN multisport_events e ON e.event_id=b.event_id
        ORDER BY b.id
        """
    )
    inserted = 0
    for bet in bets:
        start = parse_iso(bet["commence_time"])
        rows = _valid_book_quotes_by_wave(db, bet, min(now,start), parse_iso(bet["created_at"]))
        for r in rows:
            line_move = line_clv_points(bet["market_key"], bet["selection"], float(bet["line_point"]), float(r["line_point"]), bet)
            price_move = clv_pct(float(bet["offered_odds"]), float(r["price"])) if abs(float(r["line_point"])-float(bet["line_point"]))<1e-9 else None
            if db.is_postgres:
                db.execute(
                    """
                    INSERT INTO multisport_line_price_observations(
                        line_bet_id,observed_at,source_snapshot_at,bookmaker_key,
                        market_key,selection,line_point,price,price_move_pct,line_move_points
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT (line_bet_id,source_snapshot_at,line_point) DO NOTHING
                    """,
                    (bet["id"],utc_now_iso(),r["captured_at"],bet["bookmaker_key"],bet["market_key"],
                     bet["selection"],r["line_point"],r["price"],price_move,line_move),
                )
            else:
                db.execute(
                    """
                    INSERT OR IGNORE INTO multisport_line_price_observations(
                        line_bet_id,observed_at,source_snapshot_at,bookmaker_key,
                        market_key,selection,line_point,price,price_move_pct,line_move_points
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (bet["id"],utc_now_iso(),r["captured_at"],bet["bookmaker_key"],bet["market_key"],
                     bet["selection"],r["line_point"],r["price"],price_move,line_move),
                )
            inserted += 1
    return inserted


def finalize_line_closes(db: Database, now: Optional[datetime]=None) -> int:
    now = now or datetime.now(timezone.utc)
    bets = db.fetchall(
        """
        SELECT b.*,e.commence_time,e.home_team,e.away_team,e.sport_family
        FROM multisport_line_bets b JOIN multisport_events e ON e.event_id=b.event_id
        WHERE b.closing_observed_at IS NULL ORDER BY b.id
        """
    )
    updated = 0
    for bet in bets:
        start = parse_iso(bet["commence_time"])
        if start > now:
            continue
        rows = _valid_book_quotes_by_wave(db, bet, start)
        if not rows:
            continue
        close = max(rows, key=lambda r:(parse_iso(r["captured_at"]), int(r["id"])))
        mins = max(0.0, (start-parse_iso(close["captured_at"])).total_seconds()/60.0)
        line_move = line_clv_points(bet["market_key"], bet["selection"], float(bet["line_point"]), float(close["line_point"]), bet)
        p_clv = clv_pct(float(bet["offered_odds"]), float(close["price"])) if abs(float(close["line_point"])-float(bet["line_point"]))<1e-9 else None
        db.execute(
            """
            UPDATE multisport_line_bets SET closing_odds=?,closing_line_point=?,price_clv_pct=?,
                line_clv_points=?,closing_observed_at=?,closing_minutes_before_start=?,close_quality=?
            WHERE id=?
            """,
            (close["price"], close["line_point"], p_clv, line_move, close["captured_at"], mins, clv_quality(mins), bet["id"]),
        )
        updated += 1
    return updated


def settle_line_event(db: Database, event_id: str, home_score: int, away_score: int) -> int:
    event = db.fetchone("SELECT * FROM multisport_events WHERE event_id=?", (event_id,))
    if not event:
        return 0
    bets = db.fetchall("SELECT * FROM multisport_line_bets WHERE event_id=? AND status='OPEN' ORDER BY id", (event_id,))
    settled = 0
    for bet in bets:
        market = str(bet["market_key"]); selection = str(bet["selection"]); line = float(bet["line_point"])
        result = "PUSH"
        if market == "spreads":
            adjusted_home = float(home_score) + line
            if abs(adjusted_home - float(away_score)) < 1e-9:
                result = "PUSH"
            elif selection == str(event["home_team"]):
                result = "WIN" if adjusted_home > float(away_score) else "LOSS"
            elif selection == str(event["away_team"]):
                result = "WIN" if adjusted_home < float(away_score) else "LOSS"
        elif market == "totals":
            total = float(home_score + away_score)
            if abs(total-line)<1e-9:
                result = "PUSH"
            elif selection == "Over":
                result = "WIN" if total > line else "LOSS"
            elif selection == "Under":
                result = "WIN" if total < line else "LOSS"
        gross = 0.0 if result=="PUSH" else (float(bet["offered_odds"])-1.0 if result=="WIN" else -1.0)
        rate, commission, net = commission_adjusted_pnl(gross, str(bet["bookmaker_key"]))
        quality = "RULE_SENSITIVE_FINAL_SCORE" if event["sport_family"] in {"BASEBALL","ICE_HOCKEY"} else "PROVIDER_FINAL_SCORE_RESEARCH"
        provenance = "Featured line settled from provider-completed final score; venue-specific line/OT/pitcher rules remain research-only until live hardening."
        db.execute(
            """
            UPDATE multisport_line_bets SET status='SETTLED',result=?,pnl_units=?,commission_rate_pct=?,
                commission_units=?,net_pnl_units=?,settled_at=?,settlement_quality=?,settlement_provenance=?
            WHERE id=?
            """,
            (result,gross,rate,commission,net,utc_now_iso(),quality,provenance,bet["id"]),
        )
        settled += 1
    return settled


def settle_lines_from_stored_results(db: Database) -> int:
    rows = db.fetchall(
        """
        SELECT DISTINCT b.event_id,r.home_score,r.away_score
        FROM multisport_line_bets b JOIN multisport_results r ON r.event_id=b.event_id
        WHERE b.status='OPEN'
        """
    )
    return sum(settle_line_event(db, r["event_id"], int(r["home_score"]), int(r["away_score"])) for r in rows)


def line_funnel(db: Database) -> Dict[str,Any]:
    reason_rows = db.fetchall(
        """SELECT reason,decision,market_key,COUNT(*) AS n FROM multisport_line_evaluations
           GROUP BY reason,decision,market_key ORDER BY n DESC"""
    )
    return {
        "rows": reason_rows,
        "by_reason": {r["reason"]: int(r["n"]) for r in db.fetchall("SELECT reason,COUNT(*) AS n FROM multisport_line_evaluations GROUP BY reason ORDER BY n DESC")},
    }


def _segment_rows(rows: Sequence[Mapping[str,Any]], label_fn) -> List[Dict[str,Any]]:
    groups: Dict[str,List[Mapping[str,Any]]] = defaultdict(list)
    for r in rows:
        groups[str(label_fn(r))].append(r)
    out=[]
    for label, items in sorted(groups.items()):
        settled=[x for x in items if x.get("pnl_units") is not None]
        line_head=[x for x in items if x.get("line_clv_points") is not None and is_headline_clv_quality(x.get("close_quality"))]
        price_head=[x for x in items if x.get("price_clv_pct") is not None and is_headline_clv_quality(x.get("close_quality"))]
        net=sum(float(x.get("net_pnl_units") if x.get("net_pnl_units") is not None else x.get("pnl_units") or 0.0) for x in settled)
        out.append({
            "label":label,"bets":len(items),"settled":len(settled),
            "line_close_samples":len(line_head),"avg_line_clv_points":mean([float(x["line_clv_points"]) for x in line_head]) if line_head else None,
            "price_clv_samples":len(price_head),"avg_price_clv_pct":mean([float(x["price_clv_pct"]) for x in price_head]) if price_head else None,
            "net_pnl_units":net,"net_roi_pct":net/len(settled)*100.0 if settled else None,
        })
    return out


def line_scoreboard(db: Database) -> Dict[str,Any]:
    rows=db.fetchall(
        """
        SELECT b.*,e.sport_family,e.league_title,e.home_team,e.away_team,e.commence_time
        FROM multisport_line_bets b JOIN multisport_events e ON e.event_id=b.event_id ORDER BY b.id
        """
    )
    settled=[r for r in rows if r.get("pnl_units") is not None]
    line_head=[r for r in rows if r.get("line_clv_points") is not None and is_headline_clv_quality(r.get("close_quality"))]
    price_head=[r for r in rows if r.get("price_clv_pct") is not None and is_headline_clv_quality(r.get("close_quality"))]
    net=sum(float(r.get("net_pnl_units") if r.get("net_pnl_units") is not None else r.get("pnl_units") or 0.0) for r in settled)
    return {
        "events":int((db.fetchone("SELECT COUNT(DISTINCT event_id) AS n FROM multisport_line_odds_snapshots") or {}).get("n") or 0),
        "active_leagues":int((db.fetchone("SELECT COUNT(*) AS n FROM multisport_lines_state WHERE active=1") or {}).get("n") or 0),
        "odds_rows":int((db.fetchone("SELECT COUNT(*) AS n FROM multisport_line_odds_snapshots") or {}).get("n") or 0),
        "consensus_rows":int((db.fetchone("SELECT COUNT(*) AS n FROM multisport_line_consensus_snapshots") or {}).get("n") or 0),
        "evaluations":int((db.fetchone("SELECT COUNT(*) AS n FROM multisport_line_evaluations") or {}).get("n") or 0),
        "bets":len(rows),"settled":len(settled),
        "wins":sum(1 for r in settled if r.get("result")=="WIN"),"pushes":sum(1 for r in settled if r.get("result")=="PUSH"),
        "net_pnl_units":net,"net_roi_pct":net/len(settled)*100.0 if settled else None,
        "line_close_samples":len(line_head),"avg_line_clv_points":mean([float(r["line_clv_points"]) for r in line_head]) if line_head else None,
        "median_line_clv_points":median([float(r["line_clv_points"]) for r in line_head]) if line_head else None,
        "positive_line_move_pct":sum(1 for r in line_head if float(r["line_clv_points"])>0)/len(line_head)*100.0 if line_head else None,
        "price_clv_samples":len(price_head),"avg_price_clv_pct":mean([float(r["price_clv_pct"]) for r in price_head]) if price_head else None,
    }


def line_segments(db: Database) -> Dict[str,Any]:
    rows=db.fetchall(
        """SELECT b.*,e.sport_family,e.league_title,e.home_team,e.away_team
           FROM multisport_line_bets b JOIN multisport_events e ON e.event_id=b.event_id ORDER BY b.id"""
    )
    return {
        "market":_segment_rows(rows,lambda r:r["market_key"]),
        "sport":_segment_rows(rows,lambda r:r["sport_family"]),
        "league":_segment_rows(rows,lambda r:r["league_title"]),
        "venue":_segment_rows(rows,lambda r:r["bookmaker_key"]),
    }


def latest_line_bets(db: Database, limit: int=100) -> List[Dict[str,Any]]:
    return db.fetchall(
        """
        SELECT b.*,e.sport_family,e.league_title,e.home_team,e.away_team,e.commence_time,
          (SELECT p.line_move_points FROM multisport_line_price_observations p WHERE p.line_bet_id=b.id ORDER BY p.source_snapshot_at DESC LIMIT 1) AS latest_line_move_points
        FROM multisport_line_bets b JOIN multisport_events e ON e.event_id=b.event_id
        ORDER BY b.id DESC LIMIT ?
        """, (limit,)
    )


class MultiSportLinesEngine:
    def __init__(self, db: Database, api, settings, *, execution_bookmaker_keys: Sequence[str]):
        self.db=db; self.api=api; self.settings=settings
        self.execution_bookmaker_keys=tuple(str(x) for x in execution_bookmaker_keys if str(x))
        self.target_sport_keys=tuple(str(x) for x in settings.multisport_sport_keys if str(x))
        self.markets=tuple(x for x in settings.multisport_lines_markets if x in SUPPORTED_MARKETS) or ("spreads","totals")
        self.quota=MultiSportQuotaGuard(db,daily_budget=settings.multisport_daily_paid_credit_budget,reserve=settings.multisport_quota_reserve_credits)
        self.config_hash=_safe_hash(settings,self.execution_bookmaker_keys)
        self._last_discovery_at: Optional[datetime]=None

    @property
    def enabled(self)->bool:
        return bool(self.settings.multisport_lines_enabled)

    def discover_active_leagues(self, now: Optional[datetime]=None)->int:
        if not self.enabled: return 0
        now=now or datetime.now(timezone.utc); stamp=now.isoformat()
        result=self.api.sports(); self.quota.update(remaining=result.remaining,used=result.used,last_cost=result.last_cost)
        sports=result.data if isinstance(result.data,list) else []
        target=set(self.target_sport_keys); active=[]
        for item in sports:
            key=str(item.get("key") or "")
            if key not in target or not bool(item.get("active",True)): continue
            title=str(item.get("title") or key); family=sport_family(key)
            self.db.execute(
                """INSERT INTO multisport_lines_state(sport_key,title,sport_family,active,first_seen_at,last_seen_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(sport_key) DO UPDATE SET title=excluded.title,sport_family=excluded.sport_family,active=1,last_seen_at=excluded.last_seen_at""",
                (key,title,family,1,stamp,stamp))
            # Ensure shared event FK parent exists even if MSP1 discovery has not run yet.
            self.db.execute(
                """INSERT INTO multisport_league_state(sport_key,title,group_name,sport_family,active,targeted,first_seen_at,last_seen_at)
                   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(sport_key) DO UPDATE SET title=excluded.title,sport_family=excluded.sport_family,active=1,last_seen_at=excluded.last_seen_at""",
                (key,title,str(item.get("group") or family),family,1,1,stamp,stamp))
            active.append(key)
        for key in self.target_sport_keys:
            if not self.db.fetchone("SELECT sport_key FROM multisport_lines_state WHERE sport_key=?",(key,)):
                self.db.execute("INSERT INTO multisport_lines_state(sport_key,title,sport_family,active,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?)",(key,key,sport_family(key),0,stamp,stamp))
        if self.target_sport_keys:
            ph=",".join("?" for _ in self.target_sport_keys)
            self.db.execute(f"UPDATE multisport_lines_state SET active=0 WHERE sport_key IN ({ph})",self.target_sport_keys)
            for key in active:
                self.db.execute("UPDATE multisport_lines_state SET active=1,last_seen_at=? WHERE sport_key=?",(stamp,key))
        self.db.record_collector_run("MULTISPORT_LINES_DISCOVERY",True,requested_markets="sports",actual_cost=provider_actual_cost(result.last_cost,0),detail=f"targeted={len(self.target_sport_keys)}; active={len(active)}; provider_call_cost=0")
        self._last_discovery_at=now
        return len(active)

    def _maybe_discover(self, now: datetime)->None:
        interval=max(300,int(self.settings.multisport_lines_discovery_interval_seconds))
        if self._last_discovery_at is None:
            row=self.db.fetchone("SELECT started_at FROM collector_runs WHERE run_type='MULTISPORT_LINES_DISCOVERY' AND ok=1 ORDER BY id DESC LIMIT 1")
            if row:
                try:self._last_discovery_at=parse_iso(row["started_at"])
                except Exception:self._last_discovery_at=None
        if self._last_discovery_at is None or (now-self._last_discovery_at).total_seconds()>=interval:
            try:self.discover_active_leagues(now)
            except Exception as exc:
                self.db.record_collector_run("MULTISPORT_LINES_DISCOVERY",False,detail=f"error={sanitize_sensitive_text(exc)}"); self._last_discovery_at=now

    def _due_lanes(self, now: datetime)->List[Dict[str,Any]]:
        due=[]
        for row in self.db.fetchall("SELECT * FROM multisport_lines_state WHERE active=1 ORDER BY sport_key"):
            mins=_next_event_minutes(self.db,row["sport_key"],now); bi=_broad_interval_minutes(mins); ci=_convergence_interval_minutes(mins)
            if _is_due(row.get("last_broad_poll_at"),bi,now):
                due.append({"mode":"breadth","sport_key":row["sport_key"],"title":row["title"],"next_mins":mins,"lateness":_lateness(row.get("last_broad_poll_at"),bi,now)})
            has=self.db.fetchone("SELECT id FROM multisport_line_consensus_snapshots WHERE sport_key=? LIMIT 1",(row["sport_key"],))
            if has and _is_due(row.get("last_convergence_poll_at"),ci,now):
                due.append({"mode":"convergence","sport_key":row["sport_key"],"title":row["title"],"next_mins":mins,"lateness":_lateness(row.get("last_convergence_poll_at"),ci,now)})
        return sorted(due,key=lambda x:(10**9 if x["next_mins"] is None else max(0,float(x["next_mins"])), -float(x["lateness"]), 0 if x["mode"]=="breadth" else 1,x["sport_key"]))

    def _poll_breadth(self,target:Mapping[str,Any],now:datetime)->Dict[str,Any]:
        estimated=max(1,len(self.markets)); allowed,reason=self.quota.decide(estimated)
        if not allowed:return {"mode":"breadth","polled":0,"reason":reason}
        key=str(target["sport_key"]); title=str(target["title"]); actual=0; region=reference_region(self.settings,key)
        try:
            result=self.api.sport_odds(key,region,self.markets); actual=provider_actual_cost(result.last_cost,estimated); self.quota.update(remaining=result.remaining,used=result.used,last_cost=result.last_cost)
            captured=now.isoformat(); payload=result.data if isinstance(result.data,list) else []
            quote_rows,event_ids=insert_line_payload(self.db,sport_key=key,league_title=title,payload=payload,capture_mode="BREADTH",captured_at=captured)
            consensus=0
            for eid in event_ids:
                consensus+=write_line_consensus(self.db,eid,captured,min_books=self.settings.multisport_lines_min_consensus_books,excluded_books=self.execution_bookmaker_keys)
            self.db.execute("UPDATE multisport_lines_state SET last_broad_poll_at=? WHERE sport_key=?",(captured,key))
            self.db.record_collector_run("MULTISPORT_LINES_ODDS",True,sport_key=key,requested_markets=",".join(self.markets),estimated_cost=estimated,actual_cost=actual,detail=f"region={region}; events={len(event_ids)}; quote_rows={quote_rows}; consensus_rows={consensus}")
            return {"mode":"breadth","polled":1,"sport_key":key,"reference_region":region,"events":len(event_ids),"quote_rows":quote_rows,"consensus_rows":consensus,"cost":actual}
        except Exception as exc:
            self.db.record_collector_run("MULTISPORT_LINES_ODDS",False,sport_key=key,requested_markets=",".join(self.markets),estimated_cost=estimated,actual_cost=actual,detail=f"error={sanitize_sensitive_text(exc)}")
            return {"mode":"breadth","polled":0,"sport_key":key,"reason":"error"}

    def _poll_convergence(self,target:Mapping[str,Any],now:datetime)->Dict[str,Any]:
        estimated=max(1,len(self.markets)); allowed,reason=self.quota.decide(estimated)
        if not allowed:return {"mode":"convergence","polled":0,"reason":reason}
        key=str(target["sport_key"]); title=str(target["title"]); actual=0
        try:
            result=self.api.sport_odds(key,self.settings.multisport_odds_region,self.markets,bookmaker_keys=self.execution_bookmaker_keys); actual=provider_actual_cost(result.last_cost,estimated); self.quota.update(remaining=result.remaining,used=result.used,last_cost=result.last_cost)
            captured=now.isoformat(); payload=result.data if isinstance(result.data,list) else []
            quote_rows,event_ids=insert_line_payload(self.db,sport_key=key,league_title=title,payload=payload,capture_mode="CONVERGENCE",captured_at=captured)
            created=evaluate_line_wave(self.db,sport_key=key,captured_at=captured,execution_bookmaker_keys=self.execution_bookmaker_keys,min_edge_pct=self.settings.multisport_lines_min_edge_pct,max_consensus_age_minutes=self.settings.multisport_lines_max_consensus_age_minutes,config_hash=self.config_hash)
            self.db.execute("UPDATE multisport_lines_state SET last_convergence_poll_at=? WHERE sport_key=?",(captured,key))
            self.db.record_collector_run("MULTISPORT_LINES_CONVERGENCE",True,sport_key=key,requested_markets=",".join(self.markets),estimated_cost=estimated,actual_cost=actual,detail=f"events={len(event_ids)}; quote_rows={quote_rows}; executions_created={created}")
            return {"mode":"convergence","polled":1,"sport_key":key,"events":len(event_ids),"quote_rows":quote_rows,"executions_created":created,"cost":actual}
        except Exception as exc:
            self.db.record_collector_run("MULTISPORT_LINES_CONVERGENCE",False,sport_key=key,requested_markets=",".join(self.markets),estimated_cost=estimated,actual_cost=actual,detail=f"error={sanitize_sensitive_text(exc)}")
            return {"mode":"convergence","polled":0,"sport_key":key,"reason":"error"}

    def collect_results(self, now: datetime)->Dict[str,Any]:
        settled_existing=settle_lines_from_stored_results(self.db)
        open_events=self.db.fetchall("""
            SELECT DISTINCT e.* FROM multisport_events e
            JOIN multisport_line_bets b ON b.event_id=e.event_id
            LEFT JOIN multisport_results r ON r.event_id=e.event_id
            WHERE b.status='OPEN' AND r.event_id IS NULL ORDER BY e.commence_time
        """)
        due=[]
        for e in open_events:
            delay=int(RESULT_DELAY_MINUTES.get(str(e["sport_family"]),240))
            try:
                if parse_iso(e["commence_time"])+timedelta(minutes=delay)<=now: due.append(e)
            except Exception:pass
        if not due:return {"checked":0,"settled":settled_existing,"reason":"stored_or_none"}
        grouped:Dict[str,List[Dict[str,Any]]]=defaultdict(list)
        for e in due:grouped[e["sport_key"]].append(e)
        checked=0; settled=settled_existing
        for key,events in grouped.items():
            state=self.db.fetchone("SELECT * FROM multisport_lines_state WHERE sport_key=?",(key,)) or {}; last=state.get("last_results_poll_at")
            interval=max(900,int(self.settings.multisport_lines_result_poll_interval_seconds))
            if last:
                try:
                    if (now-parse_iso(last)).total_seconds()<interval:continue
                except Exception:pass
            allowed,reason=self.quota.decide(2)
            if not allowed:continue
            actual=0
            try:
                result=self.api.scores(key,event_ids=[e["event_id"] for e in events],days_from=1); actual=provider_actual_cost(result.last_cost,2); self.quota.update(remaining=result.remaining,used=result.used,last_cost=result.last_cost)
                wanted={e["event_id"]:e for e in events}; completed=0
                for item in (result.data if isinstance(result.data,list) else []):
                    eid=str(item.get("id") or "")
                    if eid not in wanted or not item.get("completed"):continue
                    scores={}
                    for s in item.get("scores") or []:
                        try:scores[str(s["name"])]=int(float(s["score"]))
                        except Exception:pass
                    e=wanted[eid]; home=e["home_team"];away=e["away_team"]
                    if home not in scores or away not in scores:continue
                    hs,as_=scores[home],scores[away]; winner=None if hs==as_ else (home if hs>as_ else away)
                    self.db.execute("""
                        INSERT INTO multisport_results(event_id,fetched_at,completed_at,home_score,away_score,winner,result_kind,source,settlement_quality,settlement_provenance,raw_json)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET fetched_at=excluded.fetched_at,completed_at=excluded.completed_at,home_score=excluded.home_score,away_score=excluded.away_score,winner=excluded.winner,result_kind=excluded.result_kind,raw_json=excluded.raw_json
                    """,(eid,utc_now_iso(),item.get("commence_time"),hs,as_,winner,"TIE" if hs==as_ else "WINNER","the_odds_api","PROVIDER_COMPLETED","provider completed final score",json.dumps(item,separators=(",",":"))))
                    self.db.execute("UPDATE multisport_events SET status='COMPLETED' WHERE event_id=?",(eid,)); settled+=settle_line_event(self.db,eid,hs,as_); completed+=1
                checked+=len(events); self.db.execute("UPDATE multisport_lines_state SET last_results_poll_at=? WHERE sport_key=?",(now.isoformat(),key))
                self.db.record_collector_run("MULTISPORT_LINES_RESULTS",True,sport_key=key,requested_markets="scores",estimated_cost=2,actual_cost=actual,detail=f"checked={len(events)}; completed_events={completed}; bets_settled={settled}")
            except Exception as exc:
                self.db.record_collector_run("MULTISPORT_LINES_RESULTS",False,sport_key=key,requested_markets="scores",estimated_cost=2,actual_cost=actual,detail=f"error={sanitize_sensitive_text(exc)}")
        return {"checked":checked,"settled":settled,"reason":"ok"}

    def maintenance(self, now: Optional[datetime]=None)->Dict[str,int]:
        now=now or datetime.now(timezone.utc)
        return {"price_observations":track_line_prices(self.db,now),"closes_finalized":finalize_line_closes(self.db,now),"settled_from_stored":settle_lines_from_stored_results(self.db)}

    def one_cycle(self, now: Optional[datetime]=None)->Dict[str,Any]:
        if not self.enabled:return {"enabled":False,"mode":"disabled","polled":0}
        now=now or datetime.now(timezone.utc); self._maybe_discover(now); due=self._due_lanes(now)
        odds={"mode":"idle","polled":0,"reason":"no_due_lines_lane"}
        if due:odds=self._poll_breadth(due[0],now) if due[0]["mode"]=="breadth" else self._poll_convergence(due[0],now)
        maint=self.maintenance(now); results=self.collect_results(now)
        return {"enabled":True,"odds":odds,"maintenance":maint,"results":results,"paid_credits_today":self.quota.today_paid_cost(now),"shared_daily_budget":self.quota.daily_budget}
