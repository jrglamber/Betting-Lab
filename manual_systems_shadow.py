from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import combinations
import json
from math import ceil
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from db import Database, sanitize_sensitive_text, utc_now_iso
from execution_shadow import clv_quality, parse_iso
from quota import provider_actual_cost
from results import grade_signal

ALGORITHM_VERSION = "HP1_HIGH_PAYOUT_FORWARD"
APP_VERSION = "0.16.1"
SYSTEM_SPECS = {"DOUBLE": (2, 1), "TREBLE": (3, 1), "FOURFOLD": (4, 1), "YANKEE": (4, 11), "SIXFOLD": (6, 1), "HEINZ": (6, 57)}\nHIGH_PAYOUT_SYSTEMS = tuple(SYSTEM_SPECS)\nMIN_LEG_ODDS = 1.50\nMAX_LEG_ODDS = 3.00
DEFAULT_PLACEABLE_BOOKS = ("williamhill", "ladbrokes_uk")
DEFAULT_COMPARISON_BOOKS = ("betfair_ex_uk", "matchbook", "smarkets")
DEFAULT_COHORTS = ("MIXED_BEST", "CONSENSUS", "PRED1", "PRED2")


def _same_point(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) < 1e-9
    except Exception:
        return str(a) == str(b)


def _uk_date(value: datetime) -> str:
    return value.astimezone(ZoneInfo("Europe/London")).date().isoformat()


def _line_combos(system_type: str, leg_orders: Sequence[int]) -> List[Tuple[int, ...]]:
    system_type = str(system_type).upper()
    n = len(leg_orders)
    if system_type == "YANKEE" and n == 4:
        sizes = (2, 3, 4)
    elif system_type == "HEINZ" and n == 6:
        sizes = (2, 3, 4, 5, 6)
    else:
        return []
    out: List[Tuple[int, ...]] = []
    for size in sizes:
        out.extend(combinations(tuple(leg_orders), size))
    return out


def _source_rows(db: Database, *, now: datetime, horizon_hours: float) -> List[Dict[str, Any]]:
    horizon = now + timedelta(hours=float(horizon_hours))
    sources: List[Tuple[str, str, str]] = [
        (
            "CONSENSUS",
            "execution_shadow_bets",
            """
            SELECT b.id,b.created_at,b.event_id,b.market_key,b.selection,
                   b.outcome_description,b.point,b.fair_probability,b.fair_odds,
                   b.edge_pct,b.min_odds,e.sport_key,e.league,e.home_team,e.away_team,
                   e.commence_time
            FROM execution_shadow_bets b JOIN events e ON e.event_id=b.event_id
            WHERE b.status='OPEN' AND e.status='UPCOMING'
            """,
        ),
        (
            "PRED1",
            "football_predictive_bets",
            """
            SELECT b.id,b.created_at,b.event_id,'h2h' AS market_key,b.selection,
                   NULL AS outcome_description,NULL AS point,
                   b.model_probability AS fair_probability,b.model_fair_odds AS fair_odds,
                   b.edge_pct,b.min_odds,e.sport_key,e.league,e.home_team,e.away_team,
                   e.commence_time
            FROM football_predictive_bets b JOIN events e ON e.event_id=b.event_id
            WHERE b.status='OPEN' AND e.status='UPCOMING'
            """,
        ),
        (
            "PRED1",
            "football_predictive_market_bets",
            """
            SELECT b.id,b.created_at,b.event_id,b.market_key,b.selection,
                   NULL AS outcome_description,b.point,
                   b.model_probability AS fair_probability,b.model_fair_odds AS fair_odds,
                   b.edge_pct,b.min_odds,e.sport_key,e.league,e.home_team,e.away_team,
                   e.commence_time
            FROM football_predictive_market_bets b JOIN events e ON e.event_id=b.event_id
            WHERE b.status='OPEN' AND e.status='UPCOMING'
            """,
        ),
        (
            "PRED2",
            "football_predictive2_bets",
            """
            SELECT b.id,b.created_at,b.event_id,'h2h' AS market_key,b.selection,
                   NULL AS outcome_description,NULL AS point,
                   b.model_probability AS fair_probability,b.model_fair_odds AS fair_odds,
                   b.edge_pct,b.min_odds,e.sport_key,e.league,e.home_team,e.away_team,
                   e.commence_time
            FROM football_predictive2_bets b JOIN events e ON e.event_id=b.event_id
            WHERE b.status='OPEN' AND e.status='UPCOMING'
            """,
        ),
        (
            "PRED2",
            "football_predictive2_market_bets",
            """
            SELECT b.id,b.created_at,b.event_id,b.market_key,b.selection,
                   NULL AS outcome_description,b.point,
                   b.model_probability AS fair_probability,b.model_fair_odds AS fair_odds,
                   b.edge_pct,b.min_odds,e.sport_key,e.league,e.home_team,e.away_team,
                   e.commence_time
            FROM football_predictive2_market_bets b JOIN events e ON e.event_id=b.event_id
            WHERE b.status='OPEN' AND e.status='UPCOMING'
            """,
        ),
    ]
    out: List[Dict[str, Any]] = []
    for engine, table, sql in sources:
        try:
            rows = db.fetchall(sql)
        except Exception:
            continue
        for raw in rows:
            row = dict(raw)
            try:
                kickoff = parse_iso(str(row["commence_time"]))
            except Exception:
                continue
            if not (now < kickoff <= horizon):
                continue
            if float(row.get("fair_probability") or 0.0) <= 0.0:
                continue
            row["source_engine"] = engine
            row["source_table"] = table
            row["source_id"] = int(row["id"])
            row["kickoff_dt"] = kickoff
            row["uk_kickoff_date"] = _uk_date(kickoff)
            row["source_key"] = f"{engine}:{table}:{row['id']}"
            out.append(row)
    return out


def _cohort_rows(rows: Sequence[Dict[str, Any]], cohort: str) -> List[Dict[str, Any]]:
    cohort = str(cohort).upper()
    if cohort == "CONSENSUS":
        return [r for r in rows if r["source_engine"] == "CONSENSUS"]
    if cohort == "PRED1":
        return [r for r in rows if r["source_engine"] == "PRED1"]
    if cohort == "PRED2":
        return [r for r in rows if r["source_engine"] == "PRED2"]
    return list(rows)


def _latest_quote(
    db: Database,
    leg: Mapping[str, Any],
    *,
    bookmaker_key: str,
    now: datetime,
    freshness_minutes: float,
) -> Optional[Dict[str, Any]]:
    rows = db.fetchall(
        """
        SELECT * FROM odds_snapshots
        WHERE event_id=? AND bookmaker_key=? AND market_key=? AND outcome_name=?
        ORDER BY id DESC LIMIT 1000
        """,
        (leg["event_id"], bookmaker_key, leg["market_key"], leg["selection"]),
    )
    cutoff = now - timedelta(minutes=float(freshness_minutes))
    source_created = parse_iso(str(leg["created_at"]))
    want_desc = str(leg.get("outcome_description") or "")
    best: Optional[Dict[str, Any]] = None
    for raw in rows:
        row = dict(raw)
        if str(row.get("outcome_description") or "") != want_desc:
            continue
        if not _same_point(row.get("point"), leg.get("point")):
            continue
        try:
            ts = parse_iso(str(row["captured_at"]))
            price = float(row["price"])
        except Exception:
            continue
        if ts < source_created or ts < cutoff or ts > now:
            continue
        if price + 1e-12 < float(leg["min_odds"]):
            continue
        if best is None or parse_iso(str(best["captured_at"])) < ts:
            best = row
    if best is not None:
        best = dict(best)
        ts = parse_iso(str(best["captured_at"]))
        best["quote_age_minutes"] = max(0.0, (now - ts).total_seconds() / 60.0)
        best["venue_edge_pct"] = (float(leg["fair_probability"]) * float(best["price"]) - 1.0) * 100.0
    return best


def manual_quote_targets(
    db: Database,
    *,
    now: Optional[datetime] = None,
    horizon_hours: float = 12.0,
    min_distinct_fixtures: int = 2,
    refresh_interval_minutes: float = 30.0,
) -> List[Dict[str, Any]]:
    """Return only events that could contribute to a Yankee/Heinz today.

    The function does not make provider calls. It merely prevents targeted
    manual-system quote refreshes when there are not even four distinct source
    fixtures on a UK date.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    rows = _source_rows(db, now=now, horizon_hours=horizon_hours)
    by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_date[str(row["uk_kickoff_date"])].append(row)

    targets: Dict[str, Dict[str, Any]] = {}
    for date, items in by_date.items():
        if len({str(x["event_id"]) for x in items}) < int(min_distinct_fixtures):
            continue
        for row in items:
            event_id = str(row["event_id"])
            item = targets.setdefault(
                event_id,
                {
                    "event_id": event_id,
                    "sport_key": str(row["sport_key"]),
                    "commence_time": str(row["commence_time"]),
                    "uk_kickoff_date": date,
                    "markets": set(),
                },
            )
            item["markets"].add(str(row["market_key"]))

    due: List[Dict[str, Any]] = []
    for item in targets.values():
        last = db.fetchone(
            """
            SELECT MAX(started_at) AS at FROM collector_runs
            WHERE run_type='MANUAL_SYSTEM_QUOTES' AND event_id=? AND ok=1
            """,
            (item["event_id"],),
        ) or {}
        last_at = last.get("at")
        if last_at:
            try:
                elapsed = (now - parse_iso(str(last_at))).total_seconds() / 60.0
                if elapsed < float(refresh_interval_minutes):
                    continue
            except Exception:
                pass
        item = dict(item)
        item["markets"] = tuple(sorted(item["markets"]))
        due.append(item)
    return sorted(due, key=lambda x: (str(x["uk_kickoff_date"]), str(x["commence_time"]), x["event_id"]))


def _manual_credits_today(db: Database, now: datetime) -> int:
    day = now.astimezone(timezone.utc).date().isoformat()
    row = db.fetchone(
        """
        SELECT COALESCE(SUM(actual_cost),0) AS n FROM collector_runs
        WHERE run_type='MANUAL_SYSTEM_QUOTES' AND started_at>=?
        """,
        (day + "T00:00:00+00:00",),
    ) or {}
    return int(row.get("n") or 0)


def refresh_manual_system_quotes(
    db: Database,
    collector: Any,
    *,
    bookmaker_keys: Sequence[str],
    now: Optional[datetime] = None,
    horizon_hours: float = 12.0,
    refresh_interval_minutes: float = 30.0,
    max_events_per_cycle: int = 6,
    daily_credit_budget: int = 300,
) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    books = tuple(dict.fromkeys(str(x) for x in bookmaker_keys if str(x)))
    if not books:
        return {"polled": 0, "inserted": 0, "reason": "no_manual_system_books"}
    spent = _manual_credits_today(db, now)
    if spent >= int(daily_credit_budget):
        return {"polled": 0, "inserted": 0, "reason": "manual_system_daily_budget", "spent": spent}

    targets = manual_quote_targets(
        db,
        now=now,
        horizon_hours=horizon_hours,
        refresh_interval_minutes=refresh_interval_minutes,
    )
    polled = inserted = 0
    for target in targets[: max(1, int(max_events_per_cycle))]:
        markets = tuple(target.get("markets") or ())
        if not markets:
            continue
        estimated_cost = max(1, ceil(len(books) / 10)) * max(1, len(markets))
        if spent + estimated_cost > int(daily_credit_budget):
            break
        decision = collector.quota.decide(estimated_cost)
        if not decision.allowed:
            return {
                "polled": polled,
                "inserted": inserted,
                "reason": decision.reason,
                "spent": spent,
            }
        actual_cost = 0
        try:
            result = collector.api.event_odds(
                target["sport_key"], target["event_id"], collector.region,
                markets, bookmaker_keys=books,
            )
            actual_cost = provider_actual_cost(result.last_cost, estimated_cost)
            collector.quota.update(
                remaining=result.remaining, used=result.used, last_cost=result.last_cost,
            )
            payload = result.data if isinstance(result.data, dict) else {}
            added = collector._insert_odds_payload(target["event_id"], payload)
            collector._clear_event_odds_failure(target["event_id"])
            db.record_collector_run(
                "MANUAL_SYSTEM_QUOTES", True,
                event_id=target["event_id"], sport_key=target["sport_key"],
                requested_markets=",".join(markets), estimated_cost=estimated_cost,
                actual_cost=actual_cost,
                detail=(
                    "mode=manual_systems; "
                    f"bookmakers={','.join(books)}; quote_rows={added}"
                ),
            )
            spent += actual_cost
            inserted += added
            polled += 1
        except Exception as exc:
            db.record_collector_run(
                "MANUAL_SYSTEM_QUOTES", False,
                event_id=target["event_id"], sport_key=target["sport_key"],
                requested_markets=",".join(markets), estimated_cost=estimated_cost,
                actual_cost=actual_cost,
                detail=f"mode=manual_systems; error={sanitize_sensitive_text(exc)}",
            )
    return {"polled": polled, "inserted": inserted, "reason": "ok", "spent": spent}


def _cohort_best_legs_for_book(
    db: Database,
    rows: Sequence[Dict[str, Any]],
    *,
    bookmaker_key: str,
    now: datetime,
    freshness_minutes: float,
) -> List[Dict[str, Any]]:
    # One leg per fixture. Within a fixture choose the source/market with the
    # best actual EV at this venue, not simply the largest model-reported edge.
    best_by_event: Dict[str, Dict[str, Any]] = {}
    for leg in rows:
        quote = _latest_quote(
            db, leg, bookmaker_key=bookmaker_key, now=now,
            freshness_minutes=freshness_minutes,
        )
        if quote is None:
            continue
        item = dict(leg)
        item["quote"] = quote
        event_id = str(item["event_id"])
        prev = best_by_event.get(event_id)
        if prev is None:
            best_by_event[event_id] = item
            continue
        cur_key = (float(quote["venue_edge_pct"]), float(item.get("edge_pct") or 0.0), item["source_key"])
        pquote = prev["quote"]
        prev_key = (float(pquote["venue_edge_pct"]), float(prev.get("edge_pct") or 0.0), prev["source_key"])
        if cur_key > prev_key:
            best_by_event[event_id] = item
    return sorted(
        best_by_event.values(),
        key=lambda x: (-float(x["quote"]["venue_edge_pct"]), -float(x.get("edge_pct") or 0.0), x["source_key"]),
    )


def _book_title(legs: Sequence[Dict[str, Any]], fallback: str) -> str:
    for leg in legs:
        title = str(leg.get("quote", {}).get("bookmaker_title") or "")
        if title:
            return title
    return fallback


def generate_manual_system_shadows(
    db: Database,
    *,
    now: Optional[datetime] = None,
    system_types: Sequence[str] = HIGH_PAYOUT_SYSTEMS,
    placeable_bookmaker_keys: Sequence[str] = DEFAULT_PLACEABLE_BOOKS,
    comparison_bookmaker_keys: Sequence[str] = DEFAULT_COMPARISON_BOOKS,
    source_cohorts: Sequence[str] = DEFAULT_COHORTS,
    quote_freshness_minutes: float = 45.0,
    max_quote_spread_minutes: float = 15.0,
    horizon_hours: float = 30.0,
) -> int:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    placeable = tuple(dict.fromkeys(str(x) for x in placeable_bookmaker_keys if str(x)))
    comparison = tuple(dict.fromkeys(str(x) for x in comparison_bookmaker_keys if str(x)))
    all_books = tuple(dict.fromkeys(placeable + comparison))
    systems = tuple(x.upper() for x in system_types if x.upper() in SYSTEM_SPECS)
    cohorts = tuple(x.upper() for x in source_cohorts if str(x))
    if not all_books or not systems or not cohorts:
        return 0

    source_rows = _source_rows(db, now=now, horizon_hours=horizon_hours)
    by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in source_rows:
        by_date[str(row["uk_kickoff_date"])].append(row)

    existing = {
        str(r["system_key"])
        for r in db.fetchall("SELECT system_key FROM manual_system_shadow_bets")
    }
    created = 0
    for kickoff_date, date_rows in sorted(by_date.items()):
        for cohort in cohorts:
            cohort_rows = _cohort_rows(date_rows, cohort)
            if len({str(x["event_id"]) for x in cohort_rows}) < 4:
                continue
            for book in all_books:
                eligible = _cohort_best_legs_for_book(
                    db, cohort_rows, bookmaker_key=book, now=now,
                    freshness_minutes=quote_freshness_minutes,
                )
                for system_type in systems:
                    leg_count, expected_lines = SYSTEM_SPECS[system_type]
                    key = f"{ALGORITHM_VERSION}|{kickoff_date}|{book}|{cohort}|{system_type}"
                    if key in existing or len(eligible) < leg_count:
                        continue
                    chosen = eligible[:leg_count]
                    quote_times = [parse_iso(str(x["quote"]["captured_at"])) for x in chosen]
                    spread = (max(quote_times) - min(quote_times)).total_seconds() / 60.0
                    if spread > float(max_quote_spread_minutes):
                        continue
                    leg_orders = tuple(range(1, leg_count + 1))
                    line_combos = _line_combos(system_type, leg_orders)
                    if len(line_combos) != expected_lines:
                        continue
                    line_stake = 1.0 / float(expected_lines)
                    expected_return = 0.0
                    all_win_return = 0.0
                    for combo in line_combos:
                        odds_product = 1.0
                        prob_product = 1.0
                        for order in combo:
                            leg = chosen[order - 1]
                            odds_product *= float(leg["quote"]["price"])
                            prob_product *= float(leg["fair_probability"])
                        expected_return += line_stake * prob_product * odds_product
                        all_win_return += line_stake * odds_product
                    singles_expected_return = sum(
                        float(x["fair_probability"]) * float(x["quote"]["price"])
                        for x in chosen
                    ) / float(leg_count)
                    first_kickoff = min(x["kickoff_dt"] for x in chosen).isoformat()
                    last_kickoff = max(x["kickoff_dt"] for x in chosen).isoformat()
                    source_engines = sorted({str(x["source_engine"]) for x in chosen})
                    manual_placeable = int(book in placeable)
                    placement_mode = "MANUAL_PLACEABLE" if manual_placeable else "SYNTHETIC_COMPARISON"
                    title = _book_title(chosen, book)
                    db.execute(
                        """
                        INSERT INTO manual_system_shadow_bets(
                          system_key,created_at,algorithm_version,system_type,leg_count,line_count,
                          bookmaker_key,bookmaker_title,placement_mode,manual_placeable,source_cohort,
                          source_engines_json,kickoff_date,first_kickoff,last_kickoff,
                          total_stake_units,line_stake_units,singles_control_stake_units,
                          expected_return_units,expected_pnl_units,expected_roi_pct,
                          singles_expected_return_units,singles_expected_pnl_units,singles_expected_roi_pct,
                          all_win_return_units,entry_quote_time_spread_minutes,status,app_version
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            key, now.isoformat(), ALGORITHM_VERSION, system_type, leg_count, expected_lines,
                            book, title, placement_mode, manual_placeable, cohort,
                            json.dumps(source_engines), kickoff_date, first_kickoff, last_kickoff,
                            1.0, line_stake, 1.0 / float(leg_count),
                            expected_return, expected_return - 1.0, (expected_return - 1.0) * 100.0,
                            singles_expected_return, singles_expected_return - 1.0,
                            (singles_expected_return - 1.0) * 100.0,
                            all_win_return, spread, "OPEN", APP_VERSION,
                        ),
                    )
                    parent = db.fetchone(
                        "SELECT id FROM manual_system_shadow_bets WHERE system_key=?", (key,)
                    )
                    if not parent:
                        continue
                    parent_id = int(parent["id"])
                    for order, leg in enumerate(chosen, start=1):
                        quote = leg["quote"]
                        db.execute(
                            """
                            INSERT INTO manual_system_shadow_legs(
                              system_bet_id,leg_order,source_engine,source_table,source_id,event_id,
                              market_key,selection,outcome_description,point,entry_odds,min_odds,
                              fair_probability,fair_odds,source_edge_pct,venue_edge_pct,
                              entry_quote_captured_at,entry_quote_age_minutes
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                parent_id, order, leg["source_engine"], leg["source_table"], leg["source_id"],
                                leg["event_id"], leg["market_key"], leg["selection"], leg.get("outcome_description"),
                                leg.get("point"), float(quote["price"]), float(leg["min_odds"]),
                                float(leg["fair_probability"]), float(leg["fair_odds"]),
                                float(leg.get("edge_pct") or 0.0), float(quote["venue_edge_pct"]),
                                quote["captured_at"], float(quote["quote_age_minutes"]),
                            ),
                        )
                    for line_order, combo in enumerate(line_combos, start=1):
                        odds_product = 1.0
                        prob_product = 1.0
                        for order in combo:
                            leg = chosen[order - 1]
                            odds_product *= float(leg["quote"]["price"])
                            prob_product *= float(leg["fair_probability"])
                        db.execute(
                            """
                            INSERT INTO manual_system_shadow_lines(
                              system_bet_id,line_order,line_size,leg_orders_json,entry_odds,
                              fair_probability,expected_return_units,expected_pnl_units,stake_units
                            ) VALUES(?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                parent_id, line_order, len(combo), json.dumps(list(combo)),
                                odds_product, prob_product, line_stake * prob_product * odds_product,
                                line_stake * (prob_product * odds_product - 1.0), line_stake,
                            ),
                        )
                    existing.add(key)
                    created += 1
    return created


def _closing_quote_for_leg(db: Database, leg: Mapping[str, Any], bookmaker_key: str, kickoff: datetime) -> Optional[Dict[str, Any]]:
    rows = db.fetchall(
        """
        SELECT * FROM odds_snapshots
        WHERE event_id=? AND bookmaker_key=? AND market_key=? AND outcome_name=?
        ORDER BY id DESC LIMIT 2000
        """,
        (leg["event_id"], bookmaker_key, leg["market_key"], leg["selection"]),
    )
    want_desc = str(leg.get("outcome_description") or "")
    entry_at = parse_iso(str(leg["entry_quote_captured_at"]))
    valid: List[Dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        if str(row.get("outcome_description") or "") != want_desc:
            continue
        if not _same_point(row.get("point"), leg.get("point")):
            continue
        try:
            ts = parse_iso(str(row["captured_at"]))
        except Exception:
            continue
        if entry_at <= ts <= kickoff:
            valid.append(row)
    if not valid:
        return None
    return max(valid, key=lambda r: parse_iso(str(r["captured_at"])))


def finalize_manual_system_clv(db: Database, *, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    cards = db.fetchall(
        """
        SELECT * FROM manual_system_shadow_bets
        WHERE closing_price_index IS NULL OR clv_quality IS NULL
        ORDER BY id ASC
        """
    )
    updated = 0
    for card in cards:
        legs = db.fetchall(
            """
            SELECT l.*,e.commence_time FROM manual_system_shadow_legs l
            JOIN events e ON e.event_id=l.event_id
            WHERE l.system_bet_id=? ORDER BY l.leg_order ASC
            """,
            (card["id"],),
        )
        if not legs:
            continue
        qualities: List[str] = []
        ready = True
        for leg in legs:
            kickoff = parse_iso(str(leg["commence_time"]))
            if kickoff > now:
                ready = False
                continue
            if leg.get("closing_odds") is None:
                close = _closing_quote_for_leg(db, leg, str(card["bookmaker_key"]), kickoff)
                if close is None:
                    ready = False
                    continue
                close_at = parse_iso(str(close["captured_at"]))
                mins = max(0.0, (kickoff - close_at).total_seconds() / 60.0)
                quality = clv_quality(mins)
                leg_clv = (float(leg["entry_odds"]) / float(close["price"]) - 1.0) * 100.0
                db.execute(
                    """
                    UPDATE manual_system_shadow_legs SET closing_odds=?,closing_observed_at=?,
                      closing_minutes_before_kickoff=?,clv_quality=?,clv_pct=? WHERE id=?
                    """,
                    (float(close["price"]), close["captured_at"], mins, quality, leg_clv, leg["id"]),
                )
                leg = dict(leg)
                leg["closing_odds"] = float(close["price"])
                leg["clv_quality"] = quality
            qualities.append(str(leg.get("clv_quality") or "STALE"))
        if not ready or len(qualities) != int(card["leg_count"]):
            continue
        refreshed = db.fetchall(
            "SELECT * FROM manual_system_shadow_legs WHERE system_bet_id=? ORDER BY leg_order ASC",
            (card["id"],),
        )
        entry_index = 0.0
        close_index = 0.0
        line_stake = float(card["line_stake_units"])
        combos = _line_combos(str(card["system_type"]), [int(x["leg_order"]) for x in refreshed])
        by_order = {int(x["leg_order"]): x for x in refreshed}
        for combo in combos:
            entry_odds = close_odds = 1.0
            for order in combo:
                entry_odds *= float(by_order[order]["entry_odds"])
                close_odds *= float(by_order[order]["closing_odds"])
            entry_index += line_stake * entry_odds
            close_index += line_stake * close_odds
        quality_rank = {"A": 0, "B": 1, "C": 2, "STALE": 3}
        quality = max(qualities, key=lambda x: quality_rank.get(x, 3))
        clv = (entry_index / close_index - 1.0) * 100.0 if close_index > 0 else None
        avg_leg_clv = sum(float(x.get("clv_pct") or 0.0) for x in refreshed) / len(refreshed)
        db.execute(
            """
            UPDATE manual_system_shadow_bets SET entry_price_index=?,closing_price_index=?,
              clv_pct=?,avg_leg_clv_pct=?,clv_quality=? WHERE id=?
            """,
            (entry_index, close_index, clv, avg_leg_clv, quality, card["id"]),
        )
        updated += 1
    return updated


def settle_manual_system_shadows(db: Database) -> int:
    cards = db.fetchall(
        "SELECT * FROM manual_system_shadow_bets WHERE status='OPEN' ORDER BY id ASC"
    )
    settled = 0
    for card in cards:
        legs = db.fetchall(
            """
            SELECT l.*,e.home_team,e.away_team,r.home_score,r.away_score
            FROM manual_system_shadow_legs l
            JOIN events e ON e.event_id=l.event_id
            LEFT JOIN event_results r ON r.event_id=l.event_id
            WHERE l.system_bet_id=? ORDER BY l.leg_order ASC
            """,
            (card["id"],),
        )
        if not legs or any(x.get("home_score") is None or x.get("away_score") is None for x in legs):
            continue
        by_order: Dict[int, Dict[str, Any]] = {}
        for leg in legs:
            result = grade_signal(
                leg,
                home_team=str(leg["home_team"]), away_team=str(leg["away_team"]),
                home_score=int(leg["home_score"]), away_score=int(leg["away_score"]),
            )
            db.execute("UPDATE manual_system_shadow_legs SET result=? WHERE id=?", (result, leg["id"]))
            item = dict(leg)
            item["result"] = result
            by_order[int(leg["leg_order"])] = item

        system_return = 0.0
        line_rows = db.fetchall(
            "SELECT * FROM manual_system_shadow_lines WHERE system_bet_id=? ORDER BY line_order ASC",
            (card["id"],),
        )
        for line in line_rows:
            orders = [int(x) for x in json.loads(str(line["leg_orders_json"]))]
            line_result = "WIN"
            settled_odds = 1.0
            for order in orders:
                leg = by_order[order]
                res = str(leg["result"])
                if res == "LOSS":
                    line_result = "LOSS"
                    settled_odds = 0.0
                    break
                if res == "WIN":
                    settled_odds *= float(leg["entry_odds"])
                elif res in {"PUSH", "VOID"}:
                    settled_odds *= 1.0
            if line_result != "LOSS" and all(str(by_order[o]["result"]) in {"PUSH", "VOID"} for o in orders):
                line_result = "PUSH"
                settled_odds = 1.0
            stake = float(line["stake_units"])
            line_return = stake * settled_odds
            line_pnl = line_return - stake
            system_return += line_return
            db.execute(
                """
                UPDATE manual_system_shadow_lines SET result=?,settled_odds=?,return_units=?,pnl_units=?
                WHERE id=?
                """,
                (line_result, settled_odds, line_return, line_pnl, line["id"]),
            )

        singles_stake = float(card["singles_control_stake_units"])
        singles_return = 0.0
        wins = 0
        for leg in by_order.values():
            res = str(leg["result"])
            if res == "WIN":
                wins += 1
                singles_return += singles_stake * float(leg["entry_odds"])
            elif res in {"PUSH", "VOID"}:
                singles_return += singles_stake
        system_pnl = system_return - float(card["total_stake_units"])
        singles_pnl = singles_return - 1.0
        result = "PROFIT" if system_pnl > 1e-12 else "LOSS" if system_pnl < -1e-12 else "BREAKEVEN"
        db.execute(
            """
            UPDATE manual_system_shadow_bets SET status='SETTLED',result=?,winning_legs=?,
              system_return_units=?,system_pnl_units=?,system_roi_pct=?,
              singles_return_units=?,singles_pnl_units=?,singles_roi_pct=?,settled_at=?
            WHERE id=?
            """,
            (
                result, wins, system_return, system_pnl, system_pnl * 100.0,
                singles_return, singles_pnl, singles_pnl * 100.0, utc_now_iso(), card["id"],
            ),
        )
        settled += 1
    return settled


def _max_drawdown(rows: Sequence[Mapping[str, Any]], field: str) -> float:
    equity = peak = 0.0
    max_dd = 0.0
    for row in rows:
        equity += float(row.get(field) or 0.0)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return max_dd


def _metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    rows = list(rows)
    settled = [x for x in rows if x.get("system_pnl_units") is not None]
    system_pnl = sum(float(x.get("system_pnl_units") or 0.0) for x in settled)
    singles_pnl = sum(float(x.get("singles_pnl_units") or 0.0) for x in settled)
    clv = [x for x in rows if x.get("clv_pct") is not None and str(x.get("clv_quality") or "") in {"A", "B"}]
    ordered = sorted(settled, key=lambda x: (str(x.get("settled_at") or ""), int(x["id"])))
    return {
        "cards": len(rows),
        "open": len([x for x in rows if str(x.get("status")) == "OPEN"]),
        "settled": len(settled),
        "profitable": len([x for x in settled if float(x.get("system_pnl_units") or 0.0) > 0]),
        "system_pnl_units": system_pnl,
        "system_roi_pct": (system_pnl / len(settled) * 100.0) if settled else None,
        "singles_pnl_units": singles_pnl,
        "singles_roi_pct": (singles_pnl / len(settled) * 100.0) if settled else None,
        "system_minus_singles_units": system_pnl - singles_pnl,
        "avg_expected_roi_pct": (
            sum(float(x.get("expected_roi_pct") or 0.0) for x in rows) / len(rows) if rows else None
        ),
        "avg_singles_expected_roi_pct": (
            sum(float(x.get("singles_expected_roi_pct") or 0.0) for x in rows) / len(rows) if rows else None
        ),
        "ab_clv_samples": len(clv),
        "avg_clv_pct": (sum(float(x["clv_pct"]) for x in clv) / len(clv)) if clv else None,
        "median_clv_pct": median([float(x["clv_pct"]) for x in clv]) if clv else None,
        "max_drawdown_units": _max_drawdown(ordered, "system_pnl_units"),
        "singles_max_drawdown_units": _max_drawdown(ordered, "singles_pnl_units"),
    }


def manual_systems_scoreboard(db: Database) -> Dict[str, Any]:
    rows = db.fetchall("SELECT * FROM manual_system_shadow_bets ORDER BY id ASC")
    overall = _metrics(rows)
    overall.update({
        "algorithm_version": ALGORITHM_VERSION,
        "research_only": True,
        "equal_total_stake_control": True,
        "manual_placeable_cards": len([x for x in rows if int(x.get("manual_placeable") or 0) == 1]),
        "synthetic_comparison_cards": len([x for x in rows if int(x.get("manual_placeable") or 0) == 0]),
        "quote_credits_today": _manual_credits_today(db, datetime.now(timezone.utc)),
    })
    segments: Dict[str, Any] = {}
    for field in ("system_type", "bookmaker_key", "source_cohort", "placement_mode"):
        items = []
        for key in sorted({str(x.get(field) or "") for x in rows}):
            sample = [x for x in rows if str(x.get(field) or "") == key]
            item = _metrics(sample)
            item["key"] = key
            item["label"] = str(sample[0].get("bookmaker_title") or key) if field == "bookmaker_key" else key
            items.append(item)
        segments[field] = items
    overall["segments"] = segments
    return overall


def latest_manual_system_cards(db: Database, limit: int = 100, *, manual_only: bool = False) -> List[Dict[str, Any]]:
    where = "WHERE manual_placeable=1" if manual_only else ""
    rows = db.fetchall(
        f"SELECT * FROM manual_system_shadow_bets {where} ORDER BY id DESC LIMIT ?", (int(limit),)
    )
    for row in rows:
        row["legs"] = db.fetchall(
            """
            SELECT l.*,e.league,e.home_team,e.away_team,e.commence_time
            FROM manual_system_shadow_legs l JOIN events e ON e.event_id=l.event_id
            WHERE l.system_bet_id=? ORDER BY l.leg_order ASC
            """,
            (row["id"],),
        )
    return rows


class ManualSystemsShadowEngine:
    def __init__(self, db: Database, collector: Any, settings: Any):
        self.db = db
        self.collector = collector
        self.settings = settings

    def one_cycle(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        if not bool(getattr(self.settings, "manual_systems_enabled", True)):
            return {"enabled": False}
        now = now or datetime.now(timezone.utc)
        placeable = tuple(getattr(self.settings, "manual_systems_placeable_bookmaker_keys", DEFAULT_PLACEABLE_BOOKS) or ())
        comparison = tuple(getattr(self.settings, "manual_systems_comparison_bookmaker_keys", DEFAULT_COMPARISON_BOOKS) or ())
        all_books = tuple(dict.fromkeys(placeable + comparison))
        refresh = refresh_manual_system_quotes(
            self.db, self.collector, bookmaker_keys=all_books, now=now,
            horizon_hours=float(getattr(self.settings, "manual_systems_refresh_horizon_hours", 12.0)),
            refresh_interval_minutes=float(getattr(self.settings, "manual_systems_refresh_interval_minutes", 30.0)),
            max_events_per_cycle=int(getattr(self.settings, "manual_systems_max_events_per_refresh", 6)),
            daily_credit_budget=int(getattr(self.settings, "manual_systems_daily_credit_budget", 300)),
        )
        created = generate_manual_system_shadows(
            self.db, now=now,
            # HP1 is a frozen construction experiment: always shadow the full\n            # double/treble/fourfold/Yankee/sixfold/Heinz family. Existing env\n            # settings cannot silently narrow the forward cohort.\n            system_types=HIGH_PAYOUT_SYSTEMS,
            placeable_bookmaker_keys=placeable,
            comparison_bookmaker_keys=comparison,
            source_cohorts=tuple(getattr(self.settings, "manual_systems_source_cohorts", DEFAULT_COHORTS) or ()),
            quote_freshness_minutes=float(getattr(self.settings, "manual_systems_quote_freshness_minutes", 45.0)),
            max_quote_spread_minutes=float(getattr(self.settings, "manual_systems_max_quote_spread_minutes", 15.0)),
            horizon_hours=float(getattr(self.settings, "manual_systems_formation_horizon_hours", 30.0)),
        )
        clv = finalize_manual_system_clv(self.db, now=now)
        settled = settle_manual_system_shadows(self.db)
        return {"enabled": True, "refresh": refresh, "created": created, "clv_finalized": clv, "settled": settled}
