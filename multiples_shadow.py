from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import combinations
import json
from statistics import median
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from db import Database, utc_now_iso
from execution_shadow import clv_quality, parse_iso
from results import grade_signal
from instrumentation import experiment_fingerprint, market_wave_metrics


ALGORITHM_VERSION = "MS1.1_API_GATE"
MULTIPLES_APP_VERSION = "0.7.2"
QUOTE_FRESHNESS_MINUTES = 30.0
FORMATION_HORIZON_HOURS = 36.0
MAX_SOURCE_LEGS_PER_UK_DATE = 14
LEG_COUNTS = (2, 3)

# v0.7.1: a singles price feed/API does not prove that a multi-leg accumulator
# can itself be submitted through that venue's official API. New Multiples
# Shadow records therefore require an explicit, separately configured
# accumulator-capable API allowlist. The default is deliberately empty.
#
# These exchange-like keys remain excluded only from broad fixed-book reference
# benchmarking; they are NOT automatically approved for multiple execution.
DEFAULT_EXCLUDED_BOOKMAKER_KEYS = {"betfair_ex_uk", "matchbook", "smarkets"}
VENUE_POLICY = "EXPLICIT_VERIFIED_MULTIPLES_API_ALLOWLIST"


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


def _book_is_exchange_like(key: str, excluded: Sequence[str]) -> bool:
    k = str(key or "")
    return k in set(str(x) for x in excluded) or "_ex_" in k


def _latest_leg_quotes(
    db: Database,
    bet: Mapping[str, Any],
    *,
    now: datetime,
    freshness_minutes: float,
    allowed_bookmaker_keys: Sequence[str],
) -> Dict[str, Dict[str, Any]]:
    """Latest fresh, post-signal broad-market quote for this exact leg by book."""
    rows = db.fetchall(
        """
        SELECT * FROM odds_snapshots
        WHERE event_id=? AND market_key=? AND outcome_name=?
        ORDER BY id DESC
        LIMIT 1000
        """,
        (bet["event_id"], bet["market_key"], bet["selection"]),
    )
    created = parse_iso(str(bet["created_at"]))
    cutoff = now - timedelta(minutes=float(freshness_minutes))
    want_desc = str(bet.get("outcome_description") or "")
    allowed = {str(x) for x in allowed_bookmaker_keys if str(x)}
    if not allowed:
        return {}
    latest: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        book = str(row.get("bookmaker_key") or "")
        if not book or book not in allowed:
            continue
        if str(row.get("outcome_description") or "") != want_desc:
            continue
        if not _same_point(row.get("point"), bet.get("point")):
            continue
        try:
            ts = parse_iso(str(row["captured_at"]))
        except Exception:
            continue
        if ts < created or ts > now or ts < cutoff:
            continue
        price = float(row["price"])
        # A same-book multiple leg must still satisfy the source strategy's
        # frozen minimum acceptable odds; otherwise we would be stacking a leg
        # that the singles engine itself said was too short.
        if price + 1e-12 < float(bet["min_odds"]):
            continue
        prev = latest.get(book)
        if prev is None or parse_iso(str(prev["captured_at"])) < ts:
            item = dict(row)
            item["quote_age_minutes"] = max(0.0, (now - ts).total_seconds() / 60.0)
            latest[book] = item
    return latest


def _candidate_source_bets(
    db: Database,
    *,
    now: datetime,
    horizon_hours: float,
) -> List[Dict[str, Any]]:
    rows = db.fetchall(
        """
        SELECT x.*,e.commence_time,e.league,e.home_team,e.away_team
        FROM execution_shadow_bets x
        JOIN events e ON e.event_id=x.event_id
        WHERE x.status='OPEN'
        ORDER BY x.edge_pct DESC,x.id ASC
        """
    )
    horizon = now + timedelta(hours=float(horizon_hours))
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            kickoff = parse_iso(str(row["commence_time"]))
        except Exception:
            continue
        if now < kickoff <= horizon:
            item = dict(row)
            item["kickoff_dt"] = kickoff
            item["uk_kickoff_date"] = _uk_date(kickoff)
            out.append(item)
    return out


def _combined_quality(qualities: Sequence[str]) -> Optional[str]:
    if not qualities:
        return None
    rank = {"A": 0, "B": 1, "C": 2, "STALE": 3}
    normalized = [str(q or "STALE").upper() for q in qualities]
    return max(normalized, key=lambda x: rank.get(x, 3))


def _latest_broad_closing_wave_for_leg(
    db: Database,
    leg: Mapping[str, Any],
    kickoff: datetime,
    *,
    min_non_exchange_books: int = 3,
) -> Optional[Tuple[datetime, List[Dict[str, Any]]]]:
    rows = db.fetchall(
        """
        SELECT * FROM odds_snapshots
        WHERE event_id=? AND market_key=? AND captured_at>=?
        ORDER BY id DESC
        LIMIT 8000
        """,
        (leg["event_id"], leg["market_key"], leg["entry_quote_captured_at"]),
    )
    by_time: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not _same_point(row.get("point"), leg.get("point")):
            continue
        want_desc = str(leg.get("outcome_description") or "")
        if want_desc and str(row.get("outcome_description") or "") != want_desc:
            continue
        try:
            ts = parse_iso(str(row["captured_at"]))
        except Exception:
            continue
        if ts <= kickoff:
            by_time[str(row["captured_at"])].append(dict(row))

    candidates = []
    for raw_ts, wave in by_time.items():
        books = {
            str(r.get("bookmaker_key") or "")
            for r in wave
            if str(r.get("outcome_name")) == str(leg["selection"])
            and not _book_is_exchange_like(
                str(r.get("bookmaker_key") or ""),
                tuple(DEFAULT_EXCLUDED_BOOKMAKER_KEYS),
            )
        }
        if len(books) >= int(min_non_exchange_books):
            candidates.append((parse_iso(raw_ts), wave))
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[0])


def generate_multiple_shadows(
    db: Database,
    *,
    now: Optional[datetime] = None,
    allowed_bookmaker_keys: Sequence[str] = (),
    freshness_minutes: float = QUOTE_FRESHNESS_MINUTES,
    horizon_hours: float = FORMATION_HORIZON_HOURS,
    max_source_legs_per_date: int = MAX_SOURCE_LEGS_PER_UK_DATE,
) -> int:
    """
    Create forward-only doubles/trebles from current executable-shadow singles.

    Formation rule (MS1):
    - source legs are currently OPEN execution-shadow bets;
    - different fixtures only;
    - all legs kick off on the same UK calendar date;
    - use at most the top N source singles by modeled edge for that date;
    - every leg must have a fresh stored quote at the SAME bookmaker;
    - that bookmaker must be explicitly allowlisted as having a verified
      accumulator-capable official API;
    - each same-book quote must be >= that source leg's frozen min_odds;
    - at the first cycle where the combination is actionable, freeze the best
      combined price among common allowlisted books available in that cycle.

    No provider/API calls are made here. If the allowlist is empty, formation is
    intentionally paused and no new multiples are created.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    allowed_bookmaker_keys = tuple(
        dict.fromkeys(str(x) for x in allowed_bookmaker_keys if str(x))
    )
    if not allowed_bookmaker_keys:
        return 0

    existing = {
        str(r["multiple_key"])
        for r in db.fetchall("SELECT multiple_key FROM multiple_shadow_bets")
    }

    candidates = _candidate_source_bets(db, now=now, horizon_hours=horizon_hours)
    by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for bet in candidates:
        by_date[str(bet["uk_kickoff_date"])].append(bet)

    created = 0
    for kickoff_date, date_bets in sorted(by_date.items()):
        # Explicit deterministic source-pool rule prevents combinatorial
        # explosion while still producing hundreds of research shadows/day.
        pool = sorted(
            date_bets,
            key=lambda x: (-float(x.get("edge_pct") or 0.0), int(x["id"])),
        )[: max(2, int(max_source_legs_per_date))]

        quotes_by_exec: Dict[int, Dict[str, Dict[str, Any]]] = {}
        for bet in pool:
            quotes_by_exec[int(bet["id"])] = _latest_leg_quotes(
                db,
                bet,
                now=now,
                freshness_minutes=freshness_minutes,
                allowed_bookmaker_keys=allowed_bookmaker_keys,
            )

        for leg_count in LEG_COUNTS:
            if len(pool) < leg_count:
                continue
            for combo in combinations(pool, leg_count):
                if len({str(x["event_id"]) for x in combo}) != leg_count:
                    continue
                ids = tuple(sorted(int(x["id"]) for x in combo))
                key = f"{ALGORITHM_VERSION}|{leg_count}|" + ",".join(str(x) for x in ids)
                if key in existing:
                    continue

                common_books: Optional[set[str]] = None
                for bet in combo:
                    books = set(quotes_by_exec.get(int(bet["id"]), {}).keys())
                    common_books = books if common_books is None else common_books & books
                if not common_books:
                    continue

                choices: List[Tuple[float, str, List[Dict[str, Any]]]] = []
                for book in sorted(common_books):
                    leg_quotes = [quotes_by_exec[int(b["id"])][book] for b in combo]
                    combined_odds = 1.0
                    for q in leg_quotes:
                        combined_odds *= float(q["price"])
                    choices.append((combined_odds, book, leg_quotes))
                if not choices:
                    continue

                combined_odds, book, leg_quotes = max(choices, key=lambda x: (x[0], x[1]))
                combined_fair_probability = 1.0
                for bet in combo:
                    combined_fair_probability *= float(bet["fair_probability"])
                if combined_fair_probability <= 0:
                    continue
                combined_fair_odds = 1.0 / combined_fair_probability
                combined_edge_pct = (combined_fair_probability * combined_odds - 1.0) * 100.0
                market_mix = "+".join(sorted(str(b["market_key"]) for b in combo))
                first_kickoff = min(b["kickoff_dt"] for b in combo).isoformat()
                last_kickoff = max(b["kickoff_dt"] for b in combo).isoformat()
                max_age = max(float(q["quote_age_minutes"]) for q in leg_quotes)
                title = str(leg_quotes[0].get("bookmaker_title") or book)
                captured_times = [parse_iso(str(q["captured_at"])) for q in leg_quotes]
                quote_spread = (
                    (max(captured_times) - min(captured_times)).total_seconds() / 60.0
                    if captured_times else 0.0
                )
                from config import settings
                fingerprint = experiment_fingerprint(
                    settings,
                    experiment_version=ALGORITHM_VERSION,
                    extra={
                        "leg_counts": list(LEG_COUNTS),
                        "quote_freshness_minutes": float(freshness_minutes),
                        "formation_horizon_hours": float(horizon_hours),
                        "max_source_legs_per_uk_date": int(max_source_legs_per_date),
                        "different_fixtures_only": True,
                        "same_uk_kickoff_date": True,
                        "venue_policy": VENUE_POLICY,
                        "multiples_api_bookmaker_keys": list(allowed_bookmaker_keys),
                    },
                )

                db.execute(
                    """
                    INSERT INTO multiple_shadow_bets(
                      multiple_key,created_at,algorithm_version,leg_count,
                      bookmaker_key,bookmaker_title,source_execution_ids_json,
                      market_mix,kickoff_date,first_kickoff,last_kickoff,
                      combined_odds,fair_probability,fair_odds,edge_pct,
                      max_entry_quote_age_minutes,status,
                      app_version,experiment_version,config_hash,
                      common_bookmaker_count,common_bookmakers_json,
                      entry_quote_time_spread_minutes,source_pool_size,
                      automation_eligible,venue_policy,allowed_api_bookmakers_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        key, now.isoformat(), ALGORITHM_VERSION, leg_count,
                        book, title, json.dumps(list(ids)), market_mix,
                        kickoff_date, first_kickoff, last_kickoff,
                        combined_odds, combined_fair_probability,
                        combined_fair_odds, combined_edge_pct, max_age, "OPEN",
                        MULTIPLES_APP_VERSION,fingerprint["experiment_version"],fingerprint["config_hash"],
                        len(common_books),json.dumps(sorted(common_books)),
                        quote_spread,len(pool),1,VENUE_POLICY,
                        json.dumps(list(allowed_bookmaker_keys)),
                    ),
                )
                parent = db.fetchone(
                    "SELECT id FROM multiple_shadow_bets WHERE multiple_key=?", (key,)
                )
                if not parent:
                    continue

                # Preserve a stable ordering by kickoff then execution id.
                ordered = sorted(
                    zip(combo, leg_quotes),
                    key=lambda pair: (pair[0]["kickoff_dt"], int(pair[0]["id"])),
                )
                for idx, (bet, quote) in enumerate(ordered, start=1):
                    db.execute(
                        """
                        INSERT INTO multiple_shadow_legs(
                          multiple_bet_id,leg_order,execution_bet_id,event_id,
                          market_key,selection,outcome_description,point,
                          entry_odds,min_odds,fair_probability,fair_odds,
                          entry_quote_captured_at,entry_quote_age_minutes
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            parent["id"], idx, bet["id"], bet["event_id"],
                            bet["market_key"], bet["selection"],
                            bet.get("outcome_description"), bet.get("point"),
                            float(quote["price"]), float(bet["min_odds"]),
                            float(bet["fair_probability"]), float(bet["fair_odds"]),
                            quote["captured_at"], float(quote["quote_age_minutes"]),
                        ),
                    )
                existing.add(key)
                created += 1
    return created


def _closing_quote_for_leg(db: Database, leg: Mapping[str, Any], kickoff: datetime) -> Optional[Dict[str, Any]]:
    rows = db.fetchall(
        """
        SELECT * FROM odds_snapshots
        WHERE event_id=? AND bookmaker_key=? AND market_key=? AND outcome_name=?
        ORDER BY id DESC
        LIMIT 2000
        """,
        (
            leg["event_id"], leg["bookmaker_key"], leg["market_key"], leg["selection"],
        ),
    )
    want_desc = str(leg.get("outcome_description") or "")
    entry_at = parse_iso(str(leg["entry_quote_captured_at"]))
    valid: List[Dict[str, Any]] = []
    for row in rows:
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


def finalize_multiple_clv(db: Database, *, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    multiples = db.fetchall(
        """
        SELECT * FROM multiple_shadow_bets
        WHERE closing_combined_odds IS NULL OR clv_quality IS NULL
        ORDER BY id ASC
        """
    )
    updated = 0
    for multiple in multiples:
        legs = db.fetchall(
            """
            SELECT l.*,m.bookmaker_key,e.commence_time
            FROM multiple_shadow_legs l
            JOIN multiple_shadow_bets m ON m.id=l.multiple_bet_id
            JOIN events e ON e.event_id=l.event_id
            WHERE l.multiple_bet_id=? ORDER BY l.leg_order ASC
            """,
            (multiple["id"],),
        )
        if not legs:
            continue
        all_ready = True
        qualities: List[str] = []
        close_product = 1.0
        for leg in legs:
            kickoff = parse_iso(str(leg["commence_time"]))
            if kickoff > now:
                all_ready = False
                continue
            if leg.get("closing_odds") is None:
                close = _closing_quote_for_leg(db, leg, kickoff)
                if close is None:
                    all_ready = False
                    continue
                close_at = parse_iso(str(close["captured_at"]))
                minutes = max(0.0, (kickoff - close_at).total_seconds() / 60.0)
                quality = clv_quality(minutes)
                db.execute(
                    """
                    UPDATE multiple_shadow_legs
                    SET closing_odds=?,closing_observed_at=?,
                        closing_minutes_before_kickoff=?,clv_quality=?
                    WHERE id=?
                    """,
                    (float(close["price"]), close["captured_at"], minutes, quality, leg["id"]),
                )
                leg = dict(leg)
                leg["closing_odds"] = float(close["price"])
                leg["clv_quality"] = quality
            close_product *= float(leg["closing_odds"])
            qualities.append(str(leg.get("clv_quality") or "STALE"))

        if all_ready and len(qualities) == int(multiple["leg_count"]):
            quality = _combined_quality(qualities)
            clv = (float(multiple["combined_odds"]) / close_product - 1.0) * 100.0

            consensus_product = 1.0
            broad_qualities: List[str] = []
            per_leg_book_prices: List[Dict[str, float]] = []
            broad_ready = True
            for leg in legs:
                kickoff = parse_iso(str(leg["commence_time"]))
                found = _latest_broad_closing_wave_for_leg(db, leg, kickoff)
                if not found:
                    broad_ready = False
                    break
                observed_at, wave = found
                metrics = market_wave_metrics(
                    wave,
                    market_key=str(leg["market_key"]),
                    selection=str(leg["selection"]),
                    point=leg.get("point"),
                    outcome_description=leg.get("outcome_description"),
                    chosen_odds=float(leg["entry_odds"]),
                )
                if metrics["median_odds"] is None:
                    broad_ready = False
                    break
                consensus_product *= float(metrics["median_odds"])
                minutes = max(0.0, (kickoff - observed_at).total_seconds() / 60.0)
                broad_qualities.append(clv_quality(minutes))

                book_prices: Dict[str, float] = {}
                for row in wave:
                    if str(row.get("outcome_name")) != str(leg["selection"]):
                        continue
                    if not _same_point(row.get("point"), leg.get("point")):
                        continue
                    want_desc = str(leg.get("outcome_description") or "")
                    if want_desc and str(row.get("outcome_description") or "") != want_desc:
                        continue
                    bkey = str(row.get("bookmaker_key") or "")
                    if not bkey or _book_is_exchange_like(
                        bkey, tuple(DEFAULT_EXCLUDED_BOOKMAKER_KEYS)
                    ):
                        continue
                    price = float(row.get("price") or 0.0)
                    if price > 1.0:
                        book_prices[bkey] = price
                per_leg_book_prices.append(book_prices)

            best_common_odds = None
            best_common_book = None
            common_close_count = None
            broad_quality = None
            clv_consensus = None
            clv_best_common = None
            if broad_ready and per_leg_book_prices:
                common = set(per_leg_book_prices[0].keys())
                for prices in per_leg_book_prices[1:]:
                    common &= set(prices.keys())
                common_close_count = len(common)
                if common:
                    choices = []
                    for bkey in common:
                        product = 1.0
                        for prices in per_leg_book_prices:
                            product *= float(prices[bkey])
                        choices.append((product, bkey))
                    best_common_odds, best_common_book = max(
                        choices, key=lambda x: (x[0], x[1])
                    )
                broad_quality = _combined_quality(broad_qualities)
                clv_consensus = (
                    float(multiple["combined_odds"]) / consensus_product - 1.0
                ) * 100.0
                if best_common_odds:
                    clv_best_common = (
                        float(multiple["combined_odds"]) / best_common_odds - 1.0
                    ) * 100.0

            db.execute(
                """
                UPDATE multiple_shadow_bets
                SET closing_combined_odds=?,clv_pct=?,clv_quality=?,
                    closing_consensus_combined_odds=?,
                    closing_best_common_book_odds=?,
                    closing_best_common_bookmaker_key=?,
                    closing_common_bookmaker_count=?,
                    closing_reference_quality=?,
                    clv_vs_closing_consensus_pct=?,
                    clv_vs_closing_best_common_pct=?
                WHERE id=?
                """,
                (
                    close_product,clv,quality,
                    consensus_product if broad_ready else None,
                    best_common_odds,best_common_book,common_close_count,
                    broad_quality,clv_consensus,clv_best_common,
                    multiple["id"],
                ),
            )
            updated += 1
    return updated


def settle_multiple_shadows(db: Database) -> int:
    multiples = db.fetchall(
        "SELECT * FROM multiple_shadow_bets WHERE status='OPEN' ORDER BY id ASC"
    )
    settled = 0
    for multiple in multiples:
        legs = db.fetchall(
            """
            SELECT l.*,e.home_team,e.away_team,r.home_score,r.away_score
            FROM multiple_shadow_legs l
            JOIN events e ON e.event_id=l.event_id
            LEFT JOIN event_results r ON r.event_id=l.event_id
            WHERE l.multiple_bet_id=? ORDER BY l.leg_order ASC
            """,
            (multiple["id"],),
        )
        if not legs or any(l.get("home_score") is None or l.get("away_score") is None for l in legs):
            continue

        results: List[str] = []
        effective_odds = 1.0
        for leg in legs:
            result = grade_signal(
                leg,
                home_team=str(leg["home_team"]),
                away_team=str(leg["away_team"]),
                home_score=int(leg["home_score"]),
                away_score=int(leg["away_score"]),
            )
            results.append(result)
            db.execute(
                "UPDATE multiple_shadow_legs SET result=? WHERE id=?",
                (result, leg["id"]),
            )
            if result == "WIN":
                effective_odds *= float(leg["entry_odds"])
            elif result in {"PUSH", "VOID"}:
                effective_odds *= 1.0

        if "LOSS" in results:
            final_result = "LOSS"
            pnl = -1.0
            settled_odds = 0.0
        elif all(r in {"PUSH", "VOID"} for r in results):
            final_result = "PUSH"
            pnl = 0.0
            settled_odds = 1.0
        else:
            final_result = "WIN"
            pnl = effective_odds - 1.0
            settled_odds = effective_odds

        db.execute(
            """
            UPDATE multiple_shadow_bets
            SET result=?,settled_combined_odds=?,pnl_units=?,status='SETTLED',settled_at=?
            WHERE id=?
            """,
            (final_result, settled_odds, pnl, utc_now_iso(), multiple["id"]),
        )
        settled += 1
    return settled


def _max_drawdown(rows: Sequence[Mapping[str, Any]]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for row in rows:
        equity += float(row.get("pnl_units") or 0.0)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return max_dd


def _metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    rows = list(rows)
    settled = [r for r in rows if r.get("pnl_units") is not None]
    wins = [r for r in settled if str(r.get("result")) == "WIN"]
    pnl = sum(float(r.get("pnl_units") or 0.0) for r in settled)
    headline = [
        r for r in rows
        if r.get("clv_pct") is not None and str(r.get("clv_quality") or "") in {"A", "B"}
    ]
    all_clv = [r for r in rows if r.get("clv_pct") is not None]
    odds = [float(r["combined_odds"]) for r in rows if r.get("combined_odds") is not None]
    return {
        "bets": len(rows),
        "open": len([r for r in rows if str(r.get("status")) == "OPEN"]),
        "settled": len(settled),
        "wins": len(wins),
        "win_rate_pct": (len(wins) / len(settled) * 100.0) if settled else None,
        "pnl_units": pnl,
        "roi_pct": (pnl / len(settled) * 100.0) if settled else None,
        "avg_combined_odds": (sum(odds) / len(odds)) if odds else None,
        "median_combined_odds": median(odds) if odds else None,
        "avg_edge_pct": (
            sum(float(r.get("edge_pct") or 0.0) for r in rows) / len(rows)
            if rows else None
        ),
        "clv_samples": len(headline),
        "avg_clv_pct": (
            sum(float(r["clv_pct"]) for r in headline) / len(headline)
            if headline else None
        ),
        "median_clv_pct": median([float(r["clv_pct"]) for r in headline]) if headline else None,
        "beat_close_pct": (
            sum(1 for r in headline if float(r["clv_pct"]) > 0) / len(headline) * 100.0
            if headline else None
        ),
        "all_clv_samples": len(all_clv),
        "all_avg_clv_pct": (
            sum(float(r["clv_pct"]) for r in all_clv) / len(all_clv)
            if all_clv else None
        ),
        "max_drawdown_units": _max_drawdown(sorted(settled, key=lambda r: (str(r.get("settled_at") or ""), int(r["id"])))),
    }


def _odds_band(value: float) -> str:
    x = float(value)
    if x < 5.0:
        return "<5"
    if x < 10.0:
        return "5-10"
    if x < 25.0:
        return "10-25"
    if x < 100.0:
        return "25-100"
    return "100+"


def multiples_scoreboard(db: Database) -> Dict[str, Any]:
    all_rows = db.fetchall("SELECT * FROM multiple_shadow_bets ORDER BY id ASC")
    rows = [
        r for r in all_rows
        if int(r.get("automation_eligible") or 0) == 1
    ]
    overall = _metrics(rows)
    overall["algorithm_version"] = ALGORITHM_VERSION
    overall["research_only"] = True
    overall["quote_freshness_minutes"] = QUOTE_FRESHNESS_MINUTES
    overall["max_source_legs_per_uk_date"] = MAX_SOURCE_LEGS_PER_UK_DATE
    overall["legacy_non_api_research_bets"] = len(all_rows) - len(rows)
    overall["automation_eligible_bets"] = len(rows)
    overall["venue_policy"] = VENUE_POLICY

    by_leg_count: Dict[str, Any] = {}
    for n in LEG_COUNTS:
        by_leg_count[str(n)] = _metrics([r for r in rows if int(r["leg_count"]) == n])

    by_bookmaker: List[Dict[str, Any]] = []
    for book in sorted({str(r["bookmaker_key"]) for r in rows}):
        sample = [r for r in rows if str(r["bookmaker_key"]) == book]
        item = _metrics(sample)
        item["key"] = book
        item["label"] = str(sample[0].get("bookmaker_title") or book)
        by_bookmaker.append(item)
    by_bookmaker.sort(key=lambda x: (-int(x["settled"]), -int(x["bets"]), str(x["label"])))

    by_market_mix: List[Dict[str, Any]] = []
    for mix in sorted({str(r["market_mix"]) for r in rows}):
        sample = [r for r in rows if str(r["market_mix"]) == mix]
        item = _metrics(sample)
        item["key"] = mix
        item["label"] = mix
        by_market_mix.append(item)
    by_market_mix.sort(key=lambda x: (-int(x["settled"]), -int(x["bets"]), str(x["label"])))

    by_odds_band: List[Dict[str, Any]] = []
    for band in ("<5", "5-10", "10-25", "25-100", "100+"):
        sample = [r for r in rows if _odds_band(float(r["combined_odds"])) == band]
        item = _metrics(sample)
        item["key"] = band
        item["label"] = band
        by_odds_band.append(item)

    overall["segments"] = {
        "leg_count": by_leg_count,
        "bookmaker": by_bookmaker,
        "market_mix": by_market_mix,
        "odds_band": by_odds_band,
    }
    state = db.fetchone("SELECT * FROM multiple_shadow_state WHERE singleton_id=1") or {}
    overall["started_at"] = state.get("started_at")
    try:
        from config import settings
        allowed = list(getattr(settings, "multiples_api_bookmaker_keys", ()) or ())
    except Exception:
        allowed = []
    overall["api_bookmaker_keys"] = allowed
    overall["formation_paused_no_verified_api_venue"] = not bool(allowed)
    return overall


def latest_multiple_shadows(
    db: Database,
    limit: int = 100,
    *,
    include_legacy: bool = False,
) -> List[Dict[str, Any]]:
    if include_legacy:
        rows = db.fetchall(
            "SELECT * FROM multiple_shadow_bets ORDER BY id DESC LIMIT ?", (int(limit),)
        )
    else:
        rows = db.fetchall(
            """SELECT * FROM multiple_shadow_bets
               WHERE COALESCE(automation_eligible,0)=1
               ORDER BY id DESC LIMIT ?""",
            (int(limit),),
        )
    for row in rows:
        row["legs"] = db.fetchall(
            """
            SELECT l.*,e.league,e.home_team,e.away_team,e.commence_time
            FROM multiple_shadow_legs l
            JOIN events e ON e.event_id=l.event_id
            WHERE l.multiple_bet_id=? ORDER BY l.leg_order ASC
            """,
            (row["id"],),
        )
    return rows
