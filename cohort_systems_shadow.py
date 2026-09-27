from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import combinations
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from db import Database, utc_now_iso

APP_VERSION = "0.19.2"
ALGORITHM_VERSION = "MS3_COHORT_SYSTEMS_V2_ARM_CONFIRM"
PRICING_MODE = "SYNTHETIC_CONSTITUENT_PRICES"
QUOTE_FRESHNESS_MINUTES = 45.0
ARM_CANDIDATE_FRESHNESS_MINUTES = 360.0
ARM_CONFIRM_TIMEOUT_MINUTES = 45.0
FORMATION_HORIZON_HOURS = 30.0

SYSTEM_SPECS = {
    "YANKEE": (4, 11),
    "HEINZ": (6, 57),
}
COHORT_SPECS = {
    "BTTS": {"YANKEE": (4, 0), "HEINZ": (6, 0)},
    "ODDS_4_TO_7_49": {"YANKEE": (0, 4), "HEINZ": (0, 6)},
    "HYBRID": {"YANKEE": (3, 1), "HEINZ": (4, 2)},
}

SOURCE_SPECS: Tuple[Tuple[str, str, str, str, str, bool], ...] = (
    ("CONSENSUS", "execution_shadow_bets", "execution_price_observations", "execution_bet_id", "fair_probability", False),
    ("PRED1", "football_predictive_bets", "football_predictive_price_observations", "predictive_bet_id", "model_probability", False),
    ("PRED1", "football_predictive_market_bets", "football_predictive_market_price_observations", "predictive_bet_id", "model_probability", True),
    ("PRED2", "football_predictive2_bets", "football_predictive2_price_observations", "predictive_bet_id", "model_probability", False),
    ("PRED2", "football_predictive2_market_bets", "football_predictive2_market_price_observations", "predictive_bet_id", "model_probability", True),
    ("PRED3", "football_predictive3_bets", "football_predictive3_price_observations", "predictive_bet_id", "model_probability", False),
    ("PRED3", "football_predictive3_market_bets", "football_predictive3_market_price_observations", "predictive_bet_id", "model_probability", True),
    ("PRED4", "football_predictive4_bets", "football_predictive4_price_observations", "predictive_bet_id", "model_probability", False),
    ("PRED4", "football_predictive4_market_bets", "football_predictive4_market_price_observations", "predictive_bet_id", "model_probability", True),
)


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _point_key(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.4f}"
    except Exception:
        return str(value)


def _selection_key(row: Mapping[str, Any]) -> str:
    return "|".join((
        str(row.get("event_id") or ""), str(row.get("market_key") or ""),
        str(row.get("selection") or ""), _point_key(row.get("point")),
    ))


def _line_combos(system_type: str, leg_orders: Sequence[int]) -> List[Tuple[int, ...]]:
    n = len(leg_orders)
    if str(system_type).upper() == "YANKEE" and n == 4:
        sizes = (2, 3, 4)
    elif str(system_type).upper() == "HEINZ" and n == 6:
        sizes = (2, 3, 4, 5, 6)
    else:
        return []
    out: List[Tuple[int, ...]] = []
    for size in sizes:
        out.extend(combinations(tuple(leg_orders), size))
    return out


def ensure_cohort_system_state(db: Database) -> Dict[str, Any]:
    row = db.fetchone("SELECT * FROM cohort_system_shadow_state WHERE singleton_id=1")
    if row:
        # v0.19.2 changes only formation mechanics. Preserve the original
        # forward-test start time while recording the active algorithm.
        if str(row.get("algorithm_version") or "") != ALGORITHM_VERSION:
            db.execute(
                "UPDATE cohort_system_shadow_state SET algorithm_version=? WHERE singleton_id=1",
                (ALGORITHM_VERSION,),
            )
            row = db.fetchone("SELECT * FROM cohort_system_shadow_state WHERE singleton_id=1") or row
        return row
    stamp = utc_now_iso()
    db.execute(
        "INSERT INTO cohort_system_shadow_state(singleton_id,started_at,algorithm_version) VALUES(1,?,?)",
        (stamp, ALGORITHM_VERSION),
    )
    return db.fetchone("SELECT * FROM cohort_system_shadow_state WHERE singleton_id=1") or {
        "singleton_id": 1, "started_at": stamp, "algorithm_version": ALGORITHM_VERSION,
    }


def _current_quote(
    db: Database, row: Mapping[str, Any], *, started_at: datetime, now: datetime,
    max_age_minutes: float = QUOTE_FRESHNESS_MINUTES,
    min_observed_at: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    created_at = _parse_iso(str(row["created_at"]))
    obs_table = str(row["obs_table"])
    obs_fk = str(row["obs_fk"])
    source_id = int(row["source_id"])
    threshold = max(created_at, started_at, min_observed_at or started_at)
    latest = db.fetchone(
        f"SELECT observed_at,source_snapshot_at,bookmaker_key,price FROM {obs_table} WHERE {obs_fk}=? ORDER BY id DESC LIMIT 1",
        (source_id,),
    )
    if latest:
        observed = _parse_iso(str(latest["observed_at"]))
        if observed >= threshold:
            age = (now - observed).total_seconds() / 60.0
            if 0.0 <= age <= float(max_age_minutes):
                return {
                    "price": float(latest["price"]), "observed_at": observed.isoformat(),
                    "bookmaker_key": str(latest.get("bookmaker_key") or row.get("bookmaker_key") or ""),
                    "age_minutes": age,
                }
    # A newly created source bet is itself an executable quote for discovery.
    # It is deliberately NOT accepted for post-arm confirmation because a card
    # must receive a fresh provider wave after it was armed.
    if min_observed_at is None and created_at >= started_at:
        age = (now - created_at).total_seconds() / 60.0
        if 0.0 <= age <= float(max_age_minutes):
            return {
                "price": float(row["offered_odds"]), "observed_at": created_at.isoformat(),
                "bookmaker_key": str(row.get("bookmaker_key") or ""), "age_minutes": age,
            }
    return None


def _source_candidates(
    db: Database, *, now: datetime, started_at: datetime,
    max_quote_age_minutes: float = ARM_CANDIDATE_FRESHNESS_MINUTES,
) -> List[Dict[str, Any]]:
    horizon = now + timedelta(hours=FORMATION_HORIZON_HOURS)
    rows: List[Dict[str, Any]] = []
    for engine, table, obs_table, obs_fk, prob_col, is_market in SOURCE_SPECS:
        market_expr = "b.market_key" if is_market or table == "execution_shadow_bets" else "'h2h'"
        point_expr = "b.point" if is_market or table == "execution_shadow_bets" else "NULL"
        outcome_expr = "b.outcome_description" if table == "execution_shadow_bets" else "NULL"
        try:
            part = db.fetchall(
                f"""
                SELECT b.id,b.created_at,b.event_id,{market_expr} AS market_key,b.selection,
                       {outcome_expr} AS outcome_description,{point_expr} AS point,
                       b.offered_odds,b.{prob_col} AS fair_probability,b.min_odds,
                       b.bookmaker_key,b.status,b.result,b.closing_odds,b.clv_pct,b.clv_quality,
                       e.commence_time,e.home_team,e.away_team,e.league,e.sport_key,e.status AS event_status
                FROM {table} b JOIN events e ON e.event_id=b.event_id
                WHERE b.status='OPEN' AND e.status='UPCOMING'
                """
            )
        except Exception:
            continue
        for raw in part:
            try:
                kickoff = _parse_iso(str(raw["commence_time"]))
            except Exception:
                continue
            if not (now < kickoff <= horizon):
                continue
            row = dict(raw)
            row.update({
                "source_engine": engine, "source_table": table, "source_id": int(row["id"]),
                "obs_table": obs_table, "obs_fk": obs_fk, "kickoff_dt": kickoff,
            })
            quote = _current_quote(
                db, row, started_at=started_at, now=now,
                max_age_minutes=max_quote_age_minutes,
            )
            if not quote:
                continue
            price = float(quote["price"])
            if price + 1e-12 < float(row.get("min_odds") or 0.0):
                continue
            row["current_odds"] = price
            row["quote_observed_at"] = quote["observed_at"]
            row["quote_bookmaker_key"] = quote["bookmaker_key"]
            row["quote_age_minutes"] = quote["age_minutes"]
            row["selection_key"] = _selection_key(row)
            rows.append(row)
    # Collapse model duplicates of the same exact outcome; preserve the best currently observed executable price.
    best: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = str(row["selection_key"])
        prev = best.get(key)
        if prev is None or float(row["current_odds"]) > float(prev["current_odds"]):
            best[key] = row
        elif float(row["current_odds"]) == float(prev["current_odds"]):
            if str(row["quote_observed_at"]) < str(prev["quote_observed_at"]):
                best[key] = row
    return sorted(best.values(), key=lambda r: (str(r["quote_observed_at"]), str(r["created_at"]), str(r["selection_key"])))


def _eligible_pools(candidates: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    btts = [r for r in candidates if r["source_engine"] in {"PRED1", "PRED2"} and str(r["market_key"]) == "btts"]
    odds = [r for r in candidates if 4.0 <= float(r["current_odds"]) < 7.5]
    return btts, odds


def _used_keys(db: Database, cohort_key: str, system_type: str) -> set[str]:
    rows = db.fetchall(
        """SELECT l.selection_key FROM cohort_system_shadow_legs l
           JOIN cohort_system_shadow_bets b ON b.id=l.system_bet_id
           WHERE b.cohort_key=? AND b.system_type=?
             AND b.status IN ('ARMED','OPEN','SETTLED')""",
        (cohort_key, system_type),
    )
    return {str(r["selection_key"]) for r in rows}


def _pick_distinct(pool: Sequence[Dict[str, Any]], count: int, *, used: set[str], blocked_events: set[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    events = set(blocked_events)
    for row in pool:
        if str(row["selection_key"]) in used or str(row["event_id"]) in events:
            continue
        out.append(row)
        events.add(str(row["event_id"]))
        if len(out) >= count:
            break
    return out


def _choose_legs(db: Database, cohort_key: str, system_type: str, btts: Sequence[Dict[str, Any]], odds: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    need_btts, need_odds = COHORT_SPECS[cohort_key][system_type]
    used = _used_keys(db, cohort_key, system_type)
    chosen_btts = _pick_distinct(btts, need_btts, used=used, blocked_events=set()) if need_btts else []
    if len(chosen_btts) != need_btts:
        return []
    blocked = {str(x["event_id"]) for x in chosen_btts}
    chosen_odds = _pick_distinct(odds, need_odds, used=used, blocked_events=blocked) if need_odds else []
    if len(chosen_odds) != need_odds:
        return []
    chosen = chosen_btts + chosen_odds
    expected_n = SYSTEM_SPECS[system_type][0]
    return chosen if len(chosen) == expected_n else []


def _leg_requires_odds_band(cohort_key: str, system_type: str, leg_order: int) -> bool:
    need_btts, need_odds = COHORT_SPECS[cohort_key][system_type]
    return need_odds > 0 and int(leg_order) > int(need_btts)


def _arm_card(db: Database, cohort_key: str, system_type: str, legs: Sequence[Dict[str, Any]], now: datetime) -> bool:
    leg_count, expected_lines = SYSTEM_SPECS[system_type]
    if len(legs) != leg_count:
        return False
    fingerprint = ";".join(str(x["selection_key"]) for x in legs)
    system_key = f"{ALGORITHM_VERSION}|{cohort_key}|{system_type}|{fingerprint}"
    if db.fetchone("SELECT id FROM cohort_system_shadow_bets WHERE system_key=?", (system_key,)):
        return False
    line_stake = 1.0 / expected_lines
    first_kickoff = min(x["kickoff_dt"] for x in legs).isoformat()
    last_kickoff = max(x["kickoff_dt"] for x in legs).isoformat()
    db.execute(
        """INSERT INTO cohort_system_shadow_bets(
           system_key,created_at,algorithm_version,cohort_key,system_type,leg_count,line_count,pricing_mode,
           total_stake_units,line_stake_units,singles_control_stake_units,first_kickoff,last_kickoff,status,
           armed_at,app_version
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (system_key, now.isoformat(), ALGORITHM_VERSION, cohort_key, system_type, leg_count, expected_lines,
         PRICING_MODE, 1.0, line_stake, 1.0 / leg_count, first_kickoff, last_kickoff, "ARMED",
         now.isoformat(), APP_VERSION),
    )
    parent = db.fetchone("SELECT id FROM cohort_system_shadow_bets WHERE system_key=?", (system_key,))
    if not parent:
        return False
    pid = int(parent["id"])
    for order, leg in enumerate(legs, start=1):
        db.execute(
            """INSERT INTO cohort_system_shadow_legs(
               system_bet_id,leg_order,selection_key,source_engine,source_table,source_id,event_id,market_key,selection,point,
               entry_odds,fair_probability,min_odds,bookmaker_key,source_created_at,entry_quote_observed_at,entry_quote_age_minutes
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, order, leg["selection_key"], leg["source_engine"], leg["source_table"], leg["source_id"],
             leg["event_id"], leg["market_key"], leg["selection"], leg.get("point"), float(leg["current_odds"]),
             float(leg.get("fair_probability") or 0.0), float(leg.get("min_odds") or 0.0), leg.get("quote_bookmaker_key"),
             leg["created_at"], leg["quote_observed_at"], float(leg["quote_age_minutes"])),
        )
    return True


def _source_spec_for_table(table: str) -> Optional[Tuple[str, str]]:
    for _engine, source_table, obs_table, obs_fk, _prob_col, _is_market in SOURCE_SPECS:
        if source_table == table:
            return obs_table, obs_fk
    return None


def _post_arm_quote(db: Database, leg: Mapping[str, Any], *, armed_at: datetime, now: datetime) -> Optional[Dict[str, Any]]:
    spec = _source_spec_for_table(str(leg["source_table"]))
    if spec is None:
        return None
    obs_table, obs_fk = spec
    source = db.fetchone(
        f"SELECT created_at,status,offered_odds,bookmaker_key FROM {leg['source_table']} WHERE id=?",
        (int(leg["source_id"]),),
    )
    if not source or str(source.get("status") or "") != "OPEN":
        return {"invalid_reason": "source_not_open"}
    row = dict(source)
    row.update({"obs_table": obs_table, "obs_fk": obs_fk, "source_id": int(leg["source_id"])})
    quote = _current_quote(
        db, row, started_at=armed_at, now=now,
        max_age_minutes=QUOTE_FRESHNESS_MINUTES,
        min_observed_at=armed_at + timedelta(microseconds=1),
    )
    return quote


def _reject_arm(db: Database, card_id: int, now: datetime, *, reason: str, status: str = "REJECTED") -> None:
    db.execute(
        "UPDATE cohort_system_shadow_bets SET status=?,rejected_at=?,rejection_reason=? WHERE id=?",
        (status, now.isoformat(), reason, int(card_id)),
    )


def _confirm_armed_card(db: Database, card: Mapping[str, Any], legs: Sequence[Mapping[str, Any]], quotes: Sequence[Mapping[str, Any]], now: datetime) -> bool:
    system_type = str(card["system_type"])
    leg_count, expected_lines = SYSTEM_SPECS[system_type]
    combos = _line_combos(system_type, tuple(range(1, leg_count + 1)))
    if len(legs) != leg_count or len(quotes) != leg_count or len(combos) != expected_lines:
        return False
    pid = int(card["id"])
    db.execute("DELETE FROM cohort_system_shadow_lines WHERE system_bet_id=?", (pid,))
    for leg, quote in zip(legs, quotes):
        db.execute(
            """UPDATE cohort_system_shadow_legs
               SET entry_odds=?,bookmaker_key=?,entry_quote_observed_at=?,entry_quote_age_minutes=?
               WHERE id=?""",
            (float(quote["price"]), str(quote.get("bookmaker_key") or ""), str(quote["observed_at"]),
             float(quote["age_minutes"]), int(leg["id"])),
        )
    refreshed_legs = db.fetchall(
        "SELECT * FROM cohort_system_shadow_legs WHERE system_bet_id=? ORDER BY leg_order", (pid,)
    )
    line_stake = 1.0 / expected_lines
    for line_order, combo in enumerate(combos, start=1):
        odds_product = 1.0
        for order in combo:
            odds_product *= float(refreshed_legs[order - 1]["entry_odds"])
        db.execute(
            """INSERT INTO cohort_system_shadow_lines(
               system_bet_id,line_order,line_size,leg_orders_json,entry_odds,stake_units
               ) VALUES(?,?,?,?,?,?)""",
            (pid, line_order, len(combo), json.dumps(combo), odds_product, line_stake),
        )
    db.execute(
        """UPDATE cohort_system_shadow_bets
           SET status='OPEN',confirmed_at=?,rejected_at=NULL,rejection_reason=NULL,app_version=?
           WHERE id=?""",
        (now.isoformat(), APP_VERSION, pid),
    )
    return True


def _advance_armed_cards(db: Database, *, now: datetime) -> Dict[str, int]:
    stats = {"confirmed": 0, "rejected": 0, "expired": 0, "pending": 0}
    cards = db.fetchall("SELECT * FROM cohort_system_shadow_bets WHERE status='ARMED' ORDER BY id")
    for card in cards:
        armed_at = _parse_iso(str(card.get("armed_at") or card["created_at"]))
        first_kickoff = _parse_iso(str(card["first_kickoff"]))
        if now >= first_kickoff:
            _reject_arm(db, int(card["id"]), now, reason="first_kickoff_reached_before_confirmation", status="EXPIRED")
            stats["expired"] += 1
            continue
        age_minutes = (now - armed_at).total_seconds() / 60.0
        if age_minutes > ARM_CONFIRM_TIMEOUT_MINUTES:
            _reject_arm(db, int(card["id"]), now, reason="fresh_confirmation_timeout", status="EXPIRED")
            stats["expired"] += 1
            continue
        legs = db.fetchall(
            "SELECT * FROM cohort_system_shadow_legs WHERE system_bet_id=? ORDER BY leg_order", (card["id"],)
        )
        quotes: List[Dict[str, Any]] = []
        rejected_reason: Optional[str] = None
        pending = False
        for leg in legs:
            quote = _post_arm_quote(db, leg, armed_at=armed_at, now=now)
            if quote is None:
                pending = True
                break
            if quote.get("invalid_reason"):
                rejected_reason = str(quote["invalid_reason"])
                break
            price = float(quote["price"])
            if price + 1e-12 < float(leg.get("min_odds") or 0.0):
                rejected_reason = f"leg_{leg['leg_order']}_below_min_odds"
                break
            if _leg_requires_odds_band(str(card["cohort_key"]), str(card["system_type"]), int(leg["leg_order"])):
                if not (4.0 <= price < 7.5):
                    rejected_reason = f"leg_{leg['leg_order']}_left_4_00_7_49_band"
                    break
            quotes.append(dict(quote))
        if rejected_reason:
            _reject_arm(db, int(card["id"]), now, reason=rejected_reason)
            stats["rejected"] += 1
            continue
        if pending or len(quotes) != len(legs):
            stats["pending"] += 1
            continue
        if _confirm_armed_card(db, card, legs, quotes, now):
            stats["confirmed"] += 1
        else:
            _reject_arm(db, int(card["id"]), now, reason="confirmation_build_failed")
            stats["rejected"] += 1
    return stats


def _experiment_order(db: Database) -> List[Tuple[str, str]]:
    order = [
        ("BTTS", "YANKEE"), ("BTTS", "HEINZ"),
        ("ODDS_4_TO_7_49", "YANKEE"), ("ODDS_4_TO_7_49", "HEINZ"),
        ("HYBRID", "YANKEE"), ("HYBRID", "HEINZ"),
    ]
    last = db.fetchone(
        "SELECT cohort_key,system_type FROM cohort_system_shadow_bets ORDER BY id DESC LIMIT 1"
    )
    if not last:
        return order
    key = (str(last.get("cohort_key") or ""), str(last.get("system_type") or ""))
    try:
        idx = order.index(key)
    except ValueError:
        return order
    return order[idx + 1:] + order[:idx + 1]


def _generation_cycle(db: Database, *, now: Optional[datetime] = None) -> Dict[str, int]:
    state = ensure_cohort_system_state(db)
    started_at = _parse_iso(str(state["started_at"]))
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    stats = _advance_armed_cards(db, now=now)
    stats["armed"] = 0

    # Keep only one prospective card armed globally. This makes the normal
    # three-event convergence budget sufficient to refresh a Yankee in two
    # worker ticks and a Heinz in two ticks, rather than creating an ever-growing
    # confirmation queue. Once the active arm confirms/rejects/expires, the next
    # cohort gets its turn via deterministic round-robin ordering.
    if db.fetchone("SELECT id FROM cohort_system_shadow_bets WHERE status='ARMED' LIMIT 1"):
        return stats

    candidates = _source_candidates(
        db, now=now, started_at=started_at,
        max_quote_age_minutes=ARM_CANDIDATE_FRESHNESS_MINUTES,
    )
    btts, odds = _eligible_pools(candidates)
    for cohort_key, system_type in _experiment_order(db):
        chosen = _choose_legs(db, cohort_key, system_type, btts, odds)
        if chosen and _arm_card(db, cohort_key, system_type, chosen, now):
            stats["armed"] = 1
            break
    return stats


def generate_cohort_system_shadows(db: Database, *, now: Optional[datetime] = None) -> int:
    """Advance MS3 formation; return the number of cards newly confirmed OPEN."""
    return int(_generation_cycle(db, now=now)["confirmed"])


def _source_state(db: Database, table: str, source_id: int) -> Optional[Dict[str, Any]]:
    try:
        return db.fetchone(f"SELECT result,closing_odds,clv_pct,clv_quality FROM {table} WHERE id=?", (source_id,))
    except Exception:
        return None


def _normal_result(value: Any) -> Optional[str]:
    x = str(value or "").upper().strip()
    if x in {"WIN", "WON"}: return "WIN"
    if x in {"LOSS", "LOST"}: return "LOSS"
    if x in {"PUSH", "VOID", "REFUND"}: return "PUSH"
    return None


def settle_cohort_system_shadows(db: Database) -> int:
    settled = 0
    cards = db.fetchall("SELECT * FROM cohort_system_shadow_bets WHERE status='OPEN' ORDER BY id")
    for card in cards:
        legs = db.fetchall("SELECT * FROM cohort_system_shadow_legs WHERE system_bet_id=? ORDER BY leg_order", (card["id"],))
        states: Dict[int, str] = {}
        clvs: List[float] = []
        ab_count = 0
        complete = True
        for leg in legs:
            src = _source_state(db, str(leg["source_table"]), int(leg["source_id"]))
            result = _normal_result(src.get("result") if src else None)
            if result is None:
                complete = False
                continue
            states[int(leg["leg_order"])] = result
            quality = str((src or {}).get("clv_quality") or "").upper()
            clv = (src or {}).get("clv_pct")
            if quality in {"A", "B"} and clv is not None:
                clvs.append(float(clv)); ab_count += 1
            db.execute(
                """UPDATE cohort_system_shadow_legs SET result=?,closing_odds=?,clv_pct=?,clv_quality=?
                   WHERE id=?""",
                (result, (src or {}).get("closing_odds"), clv, (src or {}).get("clv_quality"), leg["id"]),
            )
        if not complete or len(states) != len(legs):
            continue
        line_rows = db.fetchall("SELECT * FROM cohort_system_shadow_lines WHERE system_bet_id=? ORDER BY line_order", (card["id"],))
        system_return = 0.0
        for line in line_rows:
            orders = [int(x) for x in json.loads(str(line["leg_orders_json"]))]
            line_result = "WIN"
            return_units = float(line["stake_units"])
            if any(states[o] == "LOSS" for o in orders):
                line_result = "LOSS"; return_units = 0.0
            else:
                product = 1.0
                any_win = False
                for o in orders:
                    if states[o] == "WIN":
                        product *= float(legs[o - 1]["entry_odds"]); any_win = True
                return_units = float(line["stake_units"]) * product
                if not any_win:
                    line_result = "PUSH"
            pnl = return_units - float(line["stake_units"])
            system_return += return_units
            db.execute(
                "UPDATE cohort_system_shadow_lines SET result=?,return_units=?,pnl_units=? WHERE id=?",
                (line_result, return_units, pnl, line["id"]),
            )
        single_stake = float(card["singles_control_stake_units"])
        singles_return = 0.0
        for leg in legs:
            result = states[int(leg["leg_order"])]
            if result == "WIN": singles_return += single_stake * float(leg["entry_odds"])
            elif result == "PUSH": singles_return += single_stake
        system_pnl = system_return - 1.0
        singles_pnl = singles_return - 1.0
        wins = sum(1 for x in states.values() if x == "WIN")
        pushes = sum(1 for x in states.values() if x == "PUSH")
        db.execute(
            """UPDATE cohort_system_shadow_bets SET status='SETTLED',result=?,winning_legs=?,push_legs=?,
               system_return_units=?,system_pnl_units=?,system_roi_pct=?,singles_return_units=?,singles_pnl_units=?,singles_roi_pct=?,
               avg_leg_clv_pct=?,ab_clv_samples=?,settled_at=? WHERE id=?""",
            ("PROFIT" if system_pnl > 1e-12 else ("LOSS" if system_pnl < -1e-12 else "PUSH"), wins, pushes,
             system_return, system_pnl, system_pnl * 100.0, singles_return, singles_pnl, singles_pnl * 100.0,
             (sum(clvs) / len(clvs) if clvs else None), ab_count, utc_now_iso(), card["id"]),
        )
        settled += 1
    return settled


def _metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    active = [r for r in rows if str(r.get("status")) in {"OPEN", "SETTLED"}]
    settled = [r for r in active if str(r.get("status")) == "SETTLED"]
    n = len(active); s = len(settled)
    system_pnl = sum(float(r.get("system_pnl_units") or 0.0) for r in settled)
    singles_pnl = sum(float(r.get("singles_pnl_units") or 0.0) for r in settled)
    ab_n = sum(int(r.get("ab_clv_samples") or 0) for r in settled)
    weighted_clv = sum(float(r.get("avg_leg_clv_pct") or 0.0) * int(r.get("ab_clv_samples") or 0) for r in settled)
    return {
        "cards": n, "open": sum(1 for r in active if str(r.get("status")) == "OPEN"), "settled": s,
        "profitable_cards": sum(1 for r in settled if float(r.get("system_pnl_units") or 0.0) > 0),
        "system_pnl_units": system_pnl, "system_roi_pct": (100.0 * system_pnl / s if s else None),
        "singles_pnl_units": singles_pnl, "singles_roi_pct": (100.0 * singles_pnl / s if s else None),
        "system_minus_singles_units": system_pnl - singles_pnl,
        "ab_clv_samples": ab_n, "avg_leg_clv_pct": (weighted_clv / ab_n if ab_n else None),
    }


def cohort_systems_scoreboard(db: Database) -> Dict[str, Any]:
    # State is created/updated by startup + worker maintenance. Scoreboard reads
    # must not mutate forward-test metadata while rendering a page/API response.
    state = db.fetchone("SELECT * FROM cohort_system_shadow_state WHERE singleton_id=1") or {
        "started_at": None, "algorithm_version": ALGORITHM_VERSION,
    }
    rows = db.fetchall("SELECT * FROM cohort_system_shadow_bets ORDER BY id")
    by_cohort: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_system: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_combo: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_cohort[str(r["cohort_key"])].append(r)
        by_system[str(r["system_type"])].append(r)
        by_combo[f"{r['cohort_key']} · {r['system_type']}"] .append(r)
    return {
        "started_at": state["started_at"], "algorithm_version": state["algorithm_version"],
        "pricing_mode": PRICING_MODE, "quote_freshness_minutes": QUOTE_FRESHNESS_MINUTES,
        "arm_candidate_freshness_minutes": ARM_CANDIDATE_FRESHNESS_MINUTES,
        "arm_confirm_timeout_minutes": ARM_CONFIRM_TIMEOUT_MINUTES,
        "formation_horizon_hours": FORMATION_HORIZON_HOURS,
        "armed": sum(1 for r in rows if str(r.get("status")) == "ARMED"),
        "rejected_arms": sum(1 for r in rows if str(r.get("status")) == "REJECTED"),
        "expired_arms": sum(1 for r in rows if str(r.get("status")) == "EXPIRED"),
        **_metrics(rows),
        "segments": {
            "cohort": [{"key": k, "label": k, **_metrics(v)} for k, v in sorted(by_cohort.items())],
            "system_type": [{"key": k, "label": k, **_metrics(v)} for k, v in sorted(by_system.items())],
            "cohort_system": [{"key": k, "label": k, **_metrics(v)} for k, v in sorted(by_combo.items())],
        },
    }


def latest_cohort_system_cards(db: Database, limit: int = 100) -> List[Dict[str, Any]]:
    rows = db.fetchall("SELECT * FROM cohort_system_shadow_bets ORDER BY id DESC LIMIT ?", (int(limit),))
    for row in rows:
        legs = db.fetchall(
            """SELECT l.*,e.home_team,e.away_team,e.commence_time FROM cohort_system_shadow_legs l
               JOIN events e ON e.event_id=l.event_id WHERE l.system_bet_id=? ORDER BY l.leg_order""",
            (row["id"],),
        )
        row["legs"] = legs
    return rows


def run_cohort_systems_maintenance(db: Database, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    formation = _generation_cycle(db, now=now)
    settled = settle_cohort_system_shadows(db)
    score = cohort_systems_scoreboard(db)
    return {
        "armed": int(formation.get("armed") or 0),
        "confirmed": int(formation.get("confirmed") or 0),
        "rejected": int(formation.get("rejected") or 0),
        "expired": int(formation.get("expired") or 0),
        "pending_arms": int(score.get("armed") or 0),
        "settled": settled,
        "cards": score["cards"],
    }
