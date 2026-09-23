from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from statistics import mean, median
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


APP_VERSION = "0.6.10"
SINGLES_EXPERIMENT_VERSION = "SINGLES_CORE"

KNOCKOUT_CAPABLE_TOKENS = (
    "cup",
    "champs_league",
    "champions_league",
    "europa_league",
    "conference_league",
    "world_cup",
    "european_championship",
    "nations_league",
    "qualification",
    "qualifier",
)


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _same_point(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) < 1e-9
    except Exception:
        return str(a) == str(b)


def safe_experiment_config(settings, *, extra: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "sport_keys": list(settings.sport_keys),
        "odds_region": settings.odds_region,
        "odds_markets": list(settings.odds_markets),
        "enable_dnb_market": bool(getattr(settings, "enable_dnb_market", False)),
        "execution_bookmaker_keys": list(settings.execution_bookmaker_keys),
        "execution_shadow_enabled": bool(settings.execution_shadow_enabled),
        "enable_live_betting": bool(settings.enable_live_betting),
        "min_consensus_books": int(settings.min_consensus_books),
        "min_edge_pct": float(settings.min_edge_pct),
        "min_cross_market_edge_pct": float(settings.min_cross_market_edge_pct),
        "min_slow_book_gap_pct": float(settings.min_slow_book_gap_pct),
        "daily_paid_credit_budget": int(settings.daily_paid_credit_budget),
        "quota_reserve_credits": int(settings.quota_reserve_credits),
        "max_events_per_odds_cycle": int(settings.max_events_per_odds_cycle),
        "breadth_polls_per_day": int(settings.breadth_polls_per_day),
        "worker_tick_seconds": int(settings.worker_tick_seconds),
        "discovery_interval_seconds": int(settings.discovery_interval_seconds),
        "betfair_commission_pct": float(getattr(settings, "betfair_commission_pct", 5.0)),
        "matchbook_commission_pct": float(getattr(settings, "matchbook_commission_pct", 2.0)),
        "smarkets_commission_pct": float(getattr(settings, "smarkets_commission_pct", 2.0)),
        "default_execution_commission_pct": float(getattr(settings, "default_execution_commission_pct", 5.0)),
        "result_min_minutes_after_kickoff": int(settings.result_min_minutes_after_kickoff),
        "result_poll_min_interval_seconds": int(settings.result_poll_min_interval_seconds),
    }
    if extra:
        data["experiment_extra"] = dict(extra)
    return data


def experiment_config_hash(settings, *, extra: Optional[Mapping[str, Any]] = None) -> str:
    raw = json.dumps(
        safe_experiment_config(settings, extra=extra),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def experiment_fingerprint(
    settings,
    *,
    experiment_version: str,
    strategy_version: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    merged = dict(extra or {})
    merged["experiment_version"] = str(experiment_version)
    if strategy_version:
        merged["strategy_version"] = str(strategy_version)
    return {
        "app_version": APP_VERSION,
        "experiment_version": str(experiment_version),
        "strategy_version": strategy_version,
        "config_hash": experiment_config_hash(settings, extra=merged),
    }


def _matching_market_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    point: Optional[float],
    outcome_description: Optional[str],
) -> List[Mapping[str, Any]]:
    want_desc = str(outcome_description or "")
    out: List[Mapping[str, Any]] = []
    for row in rows:
        if not _same_point(row.get("point"), point):
            continue
        desc = str(row.get("outcome_description") or "")
        if want_desc and desc != want_desc:
            continue
        out.append(row)
    return out


def market_wave_metrics(
    rows: Sequence[Mapping[str, Any]],
    *,
    market_key: str,
    selection: str,
    point: Optional[float],
    outcome_description: Optional[str],
    chosen_odds: Optional[float] = None,
) -> Dict[str, Any]:
    rows = _matching_market_rows(
        list(rows), point=point, outcome_description=outcome_description
    )
    target = [r for r in rows if str(r.get("outcome_name")) == str(selection)]

    by_book: Dict[str, float] = {}
    for row in target:
        key = str(row.get("bookmaker_key") or "")
        if not key:
            continue
        price = float(row["price"])
        if price > 1.0:
            by_book[key] = price

    prices = list(by_book.values())
    if not prices:
        return {
            "bookmaker_count": 0,
            "median_odds": None,
            "best_odds": None,
            "price_dispersion_pct": None,
            "chosen_vs_median_pct": None,
            "mean_overround_pct": None,
        }

    med = median(prices)
    best = max(prices)
    dispersion = ((max(prices) - min(prices)) / med * 100.0) if med > 0 else None
    chosen_vs_median = (
        (float(chosen_odds) / med - 1.0) * 100.0
        if chosen_odds is not None and med > 0
        else None
    )

    expected_outcomes = {
        "h2h": 3,
        "totals": 2,
        "btts": 2,
        "draw_no_bet": 2,
    }.get(str(market_key), 2)

    outcome_prices: Dict[str, Dict[str, float]] = defaultdict(dict)
    for row in rows:
        book = str(row.get("bookmaker_key") or "")
        outcome = str(row.get("outcome_name") or "")
        price = float(row.get("price") or 0.0)
        if book and outcome and price > 1.0:
            outcome_prices[book][outcome] = price

    overrounds: List[float] = []
    for book_prices in outcome_prices.values():
        if len(book_prices) < expected_outcomes:
            continue
        raw = sum(1.0 / p for p in book_prices.values())
        overrounds.append((raw - 1.0) * 100.0)

    return {
        "bookmaker_count": len(by_book),
        "median_odds": med,
        "best_odds": best,
        "price_dispersion_pct": dispersion,
        "chosen_vs_median_pct": chosen_vs_median,
        "mean_overround_pct": mean(overrounds) if overrounds else None,
    }


def entry_market_metrics(
    db,
    *,
    event_id: str,
    captured_at: str,
    market_key: str,
    selection: str,
    point: Optional[float],
    outcome_description: Optional[str],
    chosen_odds: float,
) -> Dict[str, Any]:
    rows = db.fetchall(
        """
        SELECT * FROM odds_snapshots
        WHERE event_id=? AND captured_at=? AND market_key=?
        ORDER BY id ASC
        """,
        (event_id, captured_at, market_key),
    )
    return market_wave_metrics(
        rows,
        market_key=market_key,
        selection=selection,
        point=point,
        outcome_description=outcome_description,
        chosen_odds=chosen_odds,
    )


def backfill_entry_market_metrics(db) -> int:
    rows = db.fetchall(
        """
        SELECT * FROM execution_shadow_bets
        WHERE entry_consensus_bookmaker_count IS NULL
           OR entry_consensus_median_odds IS NULL
        ORDER BY id ASC
        """
    )
    updated = 0
    for bet in rows:
        metrics = entry_market_metrics(
            db,
            event_id=str(bet["event_id"]),
            captured_at=str(bet["created_at"]),
            market_key=str(bet["market_key"]),
            selection=str(bet["selection"]),
            point=bet.get("point"),
            outcome_description=bet.get("outcome_description"),
            chosen_odds=float(bet["offered_odds"]),
        )
        if not metrics["bookmaker_count"]:
            continue
        db.execute(
            """
            UPDATE execution_shadow_bets
            SET entry_consensus_bookmaker_count=?,
                entry_consensus_median_odds=?,
                entry_consensus_best_odds=?,
                entry_price_dispersion_pct=?,
                entry_chosen_vs_median_pct=?,
                entry_mean_overround_pct=?
            WHERE id=?
            """,
            (
                metrics["bookmaker_count"],
                metrics["median_odds"],
                metrics["best_odds"],
                metrics["price_dispersion_pct"],
                metrics["chosen_vs_median_pct"],
                metrics["mean_overround_pct"],
                bet["id"],
            ),
        )
        updated += 1
    return updated


def _latest_broad_wave_for_bet(
    db,
    bet: Mapping[str, Any],
    *,
    kickoff: datetime,
    min_non_exchange_books: int = 3,
    excluded_books: Sequence[str] = ("betfair_ex_uk", "matchbook", "smarkets"),
) -> Optional[Tuple[datetime, List[Mapping[str, Any]]]]:
    rows = db.fetchall(
        """
        SELECT * FROM odds_snapshots
        WHERE event_id=? AND market_key=? AND captured_at>=?
        ORDER BY id DESC
        LIMIT 8000
        """,
        (bet["event_id"], bet["market_key"], bet["created_at"]),
    )
    by_time: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        try:
            ts = parse_iso(str(row["captured_at"]))
        except Exception:
            continue
        if ts > kickoff:
            continue
        if not _same_point(row.get("point"), bet.get("point")):
            continue
        want_desc = str(bet.get("outcome_description") or "")
        desc = str(row.get("outcome_description") or "")
        if want_desc and desc != want_desc:
            continue
        by_time[str(row["captured_at"])].append(row)

    excluded = set(str(x) for x in excluded_books)
    waves: List[Tuple[datetime, List[Mapping[str, Any]]]] = []
    for raw_ts, wave in by_time.items():
        target_books = {
            str(r.get("bookmaker_key") or "")
            for r in wave
            if str(r.get("outcome_name")) == str(bet["selection"])
            and str(r.get("bookmaker_key") or "") not in excluded
            and "_ex_" not in str(r.get("bookmaker_key") or "")
        }
        if len(target_books) >= int(min_non_exchange_books):
            waves.append((parse_iso(raw_ts), wave))
    if not waves:
        return None
    return max(waves, key=lambda x: x[0])


def finalize_reference_closes(
    db,
    *,
    now: Optional[datetime] = None,
    excluded_books: Sequence[str] = ("betfair_ex_uk", "matchbook", "smarkets"),
) -> int:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    bets = db.fetchall(
        """
        SELECT x.*,e.commence_time
        FROM execution_shadow_bets x
        JOIN events e ON e.event_id=x.event_id
        WHERE COALESCE(x.closing_reference_status,'PENDING')='PENDING'
        ORDER BY x.id ASC
        """
    )
    updated = 0
    for bet in bets:
        try:
            kickoff = parse_iso(str(bet["commence_time"]))
        except Exception:
            continue
        if kickoff > now:
            continue
        found = _latest_broad_wave_for_bet(
            db, bet, kickoff=kickoff, excluded_books=excluded_books
        )
        if not found:
            db.execute(
                "UPDATE execution_shadow_bets SET closing_reference_status='UNAVAILABLE' WHERE id=?",
                (bet["id"],),
            )
            continue
        observed_at, wave = found
        metrics = market_wave_metrics(
            wave,
            market_key=str(bet["market_key"]),
            selection=str(bet["selection"]),
            point=bet.get("point"),
            outcome_description=bet.get("outcome_description"),
            chosen_odds=float(bet["offered_odds"]),
        )
        median_close = metrics["median_odds"]
        best_close = metrics["best_odds"]
        if median_close is None or best_close is None:
            db.execute(
                "UPDATE execution_shadow_bets SET closing_reference_status='UNAVAILABLE' WHERE id=?",
                (bet["id"],),
            )
            continue
        minutes = max(0.0, (kickoff - observed_at).total_seconds() / 60.0)
        if minutes <= 15:
            quality = "A"
        elif minutes <= 30:
            quality = "B"
        elif minutes <= 60:
            quality = "C"
        else:
            quality = "STALE"
        db.execute(
            """
            UPDATE execution_shadow_bets
            SET closing_consensus_median_odds=?,
                closing_reference_best_odds=?,
                closing_reference_observed_at=?,
                closing_reference_minutes_before_kickoff=?,
                closing_reference_quality=?,
                closing_reference_status='FINALIZED',
                clv_vs_consensus_pct=?,
                clv_vs_best_reference_pct=?
            WHERE id=?
            """,
            (
                median_close,
                best_close,
                observed_at.isoformat(),
                minutes,
                quality,
                (float(bet["offered_odds"]) / median_close - 1.0) * 100.0,
                (float(bet["offered_odds"]) / best_close - 1.0) * 100.0,
                bet["id"],
            ),
        )
        updated += 1
    return updated


def settlement_provenance_for_sport(sport_key: str) -> Tuple[str, str]:
    key = str(sport_key or "").lower()
    if any(token in key for token in KNOCKOUT_CAPABLE_TOKENS):
        return (
            "UNVERIFIED_REGULATION_TIME",
            "provider_completed_score; competition can contain extra-time/playoff matches",
        )
    return (
        "STANDARD_LEAGUE",
        "provider_completed_score; standard league fixture",
    )


def refresh_settlement_provenance(db) -> int:
    rows = db.fetchall(
        """
        SELECT r.event_id,e.sport_key,r.settlement_quality,r.settlement_provenance
        FROM event_results r
        JOIN events e ON e.event_id=r.event_id
        ORDER BY r.event_id
        """
    )
    updated = 0
    for row in rows:
        quality, provenance = settlement_provenance_for_sport(str(row["sport_key"]))
        if (
            str(row.get("settlement_quality") or "") == quality
            and str(row.get("settlement_provenance") or "") == provenance
        ):
            continue
        db.execute(
            """
            UPDATE event_results
            SET settlement_quality=?,settlement_provenance=?
            WHERE event_id=?
            """,
            (quality, provenance, row["event_id"]),
        )
        updated += 1
    return updated


def _probability_metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    usable = []
    for row in rows:
        result = str(row.get("result") or "")
        if result not in {"WIN", "LOSS"}:
            continue
        try:
            p = float(row["fair_probability"])
        except Exception:
            continue
        if not (0.0 < p < 1.0):
            continue
        y = 1.0 if result == "WIN" else 0.0
        usable.append((p, y))
    if not usable:
        return {
            "samples": 0,
            "expected_win_pct": None,
            "observed_win_pct": None,
            "calibration_gap_pp": None,
            "brier_score": None,
            "log_loss": None,
            "bands": [],
        }

    eps = 1e-12
    expected = mean(p for p, _ in usable)
    observed = mean(y for _, y in usable)
    brier = mean((p - y) ** 2 for p, y in usable)
    logloss = -mean(
        y * math.log(max(eps, min(1 - eps, p)))
        + (1 - y) * math.log(max(eps, min(1 - eps, 1 - p)))
        for p, y in usable
    )

    bands = []
    for low in range(0, 100, 10):
        high = low + 10
        sample = [
            (p, y) for p, y in usable
            if p * 100.0 >= low and (p * 100.0 < high or high == 100)
        ]
        if not sample:
            continue
        exp = mean(p for p, _ in sample) * 100.0
        obs = mean(y for _, y in sample) * 100.0
        bands.append({
            "label": f"{low}-{high}%",
            "samples": len(sample),
            "expected_win_pct": exp,
            "observed_win_pct": obs,
            "calibration_gap_pp": obs - exp,
        })

    return {
        "samples": len(usable),
        "expected_win_pct": expected * 100.0,
        "observed_win_pct": observed * 100.0,
        "calibration_gap_pp": (observed - expected) * 100.0,
        "brier_score": brier,
        "log_loss": logloss,
        "bands": bands,
    }


def probability_calibration_report(db) -> Dict[str, Any]:
    singles = db.fetchall(
        """
        SELECT x.fair_probability,x.result,r.settlement_quality
        FROM execution_shadow_bets x
        JOIN event_results r ON r.event_id=x.event_id
        WHERE x.status='SETTLED'
          AND x.result IN ('WIN','LOSS')
          AND COALESCE(r.settlement_quality,'STANDARD_LEAGUE')='STANDARD_LEAGUE'
        ORDER BY x.id
        """
    )

    multiples = db.fetchall(
        """
        SELECT m.id,m.fair_probability,m.result
        FROM multiple_shadow_bets m
        WHERE m.status='SETTLED' AND m.result IN ('WIN','LOSS')
        ORDER BY m.id
        """
    )
    clean_multiple_rows: List[Dict[str, Any]] = []
    for m in multiples:
        legs = db.fetchall(
            """
            SELECT l.result,r.settlement_quality
            FROM multiple_shadow_legs l
            LEFT JOIN event_results r ON r.event_id=l.event_id
            WHERE l.multiple_bet_id=?
            ORDER BY l.leg_order
            """,
            (m["id"],),
        )
        if not legs:
            continue
        if any(str(l.get("result") or "") not in {"WIN", "LOSS"} for l in legs):
            continue
        if any(
            str(l.get("settlement_quality") or "STANDARD_LEAGUE") != "STANDARD_LEAGUE"
            for l in legs
        ):
            continue
        clean_multiple_rows.append(dict(m))

    return {
        "singles": _probability_metrics(singles),
        "multiples": _probability_metrics(clean_multiple_rows),
    }


def multiples_overlap_report(db) -> Dict[str, Any]:
    rows = db.fetchall(
        """
        SELECT l.execution_bet_id,l.event_id,l.multiple_bet_id,m.kickoff_date
        FROM multiple_shadow_legs l
        JOIN multiple_shadow_bets m ON m.id=l.multiple_bet_id
        ORDER BY l.id
        """
    )
    if not rows:
        return {
            "multiples": 0,
            "total_leg_slots": 0,
            "unique_source_legs": 0,
            "unique_fixtures": 0,
            "kickoff_dates": 0,
            "mean_reuse_per_source_leg": None,
            "median_reuse_per_source_leg": None,
            "max_reuse_one_source_leg": 0,
            "effective_source_leg_count": 0.0,
            "unique_leg_ratio_pct": None,
        }

    reuse = Counter(int(r["execution_bet_id"]) for r in rows)
    counts = list(reuse.values())
    total_slots = len(rows)
    effective = (
        (sum(counts) ** 2) / sum(c * c for c in counts)
        if counts and sum(c * c for c in counts)
        else 0.0
    )
    return {
        "multiples": len({int(r["multiple_bet_id"]) for r in rows}),
        "total_leg_slots": total_slots,
        "unique_source_legs": len(reuse),
        "unique_fixtures": len({str(r["event_id"]) for r in rows}),
        "kickoff_dates": len({str(r["kickoff_date"]) for r in rows}),
        "mean_reuse_per_source_leg": mean(counts) if counts else None,
        "median_reuse_per_source_leg": median(counts) if counts else None,
        "max_reuse_one_source_leg": max(counts) if counts else 0,
        "effective_source_leg_count": effective,
        "unique_leg_ratio_pct": (len(reuse) / total_slots * 100.0) if total_slots else None,
    }


def experiment_coverage_report(db) -> Dict[str, Any]:
    def one(table: str) -> Dict[str, Any]:
        rows = db.fetchall(
            f"""
            SELECT app_version,experiment_version,config_hash
            FROM {table}
            ORDER BY id
            """
        )
        instrumented = [
            r for r in rows
            if r.get("app_version") and r.get("experiment_version") and r.get("config_hash")
        ]
        return {
            "rows": len(rows),
            "instrumented_rows": len(instrumented),
            "coverage_pct": (len(instrumented) / len(rows) * 100.0) if rows else None,
            "distinct_config_hashes": len({
                str(r["config_hash"]) for r in instrumented if r.get("config_hash")
            }),
            "app_versions": sorted({
                str(r["app_version"]) for r in instrumented if r.get("app_version")
            }),
            "experiment_versions": sorted({
                str(r["experiment_version"]) for r in instrumented if r.get("experiment_version")
            }),
        }
    return {
        "execution_shadow_bets": one("execution_shadow_bets"),
        "multiple_shadow_bets": one("multiple_shadow_bets"),
    }


def instrumentation_report(db) -> Dict[str, Any]:
    refresh_settlement_provenance(db)
    calibration = probability_calibration_report(db)
    overlap = multiples_overlap_report(db)
    coverage = experiment_coverage_report(db)

    result_quality = db.fetchall(
        """
        SELECT settlement_quality,COUNT(*) AS n
        FROM event_results
        GROUP BY settlement_quality
        ORDER BY settlement_quality
        """
    )
    market_quality = db.fetchone(
        """
        SELECT
          COUNT(*) AS rows,
          SUM(CASE WHEN entry_consensus_bookmaker_count IS NOT NULL THEN 1 ELSE 0 END) AS measured,
          AVG(entry_consensus_bookmaker_count) AS avg_books,
          AVG(entry_price_dispersion_pct) AS avg_dispersion_pct,
          AVG(entry_mean_overround_pct) AS avg_overround_pct
        FROM execution_shadow_bets
        """
    ) or {}

    multiple_realism = db.fetchone(
        """
        SELECT
          COUNT(*) AS rows,
          AVG(common_bookmaker_count) AS avg_common_books,
          AVG(entry_quote_time_spread_minutes) AS avg_quote_spread_minutes,
          MAX(entry_quote_time_spread_minutes) AS max_quote_spread_minutes
        FROM multiple_shadow_bets
        """
    ) or {}

    close_benchmarks = db.fetchone(
        """
        SELECT
          COUNT(*) AS rows,
          SUM(CASE WHEN closing_consensus_median_odds IS NOT NULL THEN 1 ELSE 0 END) AS measured,
          AVG(clv_vs_consensus_pct) AS avg_clv_vs_consensus_pct,
          AVG(clv_vs_best_reference_pct) AS avg_clv_vs_best_reference_pct
        FROM execution_shadow_bets
        WHERE status='SETTLED'
        """
    ) or {}

    return {
        "app_version": APP_VERSION,
        "provider_calls_added": 0,
        "experiment_coverage": coverage,
        "entry_market_quality": dict(market_quality),
        "closing_reference_benchmarks": dict(close_benchmarks),
        "probability_calibration": calibration,
        "multiple_execution_realism": dict(multiple_realism),
        "multiples_overlap": overlap,
        "settlement_quality": [dict(r) for r in result_quality],
    }


def run_measurement_maintenance(
    db,
    *,
    now: Optional[datetime] = None,
    excluded_books: Sequence[str] = ("betfair_ex_uk", "matchbook", "smarkets"),
) -> Dict[str, int]:
    return {
        "entry_market_backfilled": backfill_entry_market_metrics(db),
        "reference_closes_finalized": finalize_reference_closes(
            db, now=now, excluded_books=excluded_books
        ),
        "settlement_provenance_refreshed": refresh_settlement_provenance(db),
    }
