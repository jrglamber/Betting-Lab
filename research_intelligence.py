from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone, timedelta
import json
from statistics import median
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from db import Database, utc_now_iso


MARKET_LABELS = {
    "h2h": "1X2",
    "totals": "O/U",
    "btts": "BTTS",
    "draw_no_bet": "DNB",
}


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))



def _headline_clv_row(row: Mapping[str, Any]) -> bool:
    return (
        row.get("clv_pct") is not None
        and str(row.get("clv_quality") or "").upper() in {"A", "B"}
    )


def _metrics(items: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    rows = list(items)
    settled = [x for x in rows if x.get("pnl_units") is not None]
    all_clv_rows = [x for x in rows if x.get("clv_pct") is not None]
    clv_rows = [x for x in rows if _headline_clv_row(x)]
    edges = [float(x["edge_pct"]) for x in rows if x.get("edge_pct") is not None]

    pnl = sum(float(x["pnl_units"]) for x in settled)
    net_pnl = sum(
        float(x["net_pnl_units"])
        if x.get("net_pnl_units") is not None else float(x["pnl_units"])
        for x in settled
    )
    commission = sum(float(x.get("commission_units") or 0.0) for x in settled)
    wins = sum(1 for x in settled if float(x["pnl_units"]) > 0)
    clvs = [float(x["clv_pct"]) for x in clv_rows]
    all_clvs = [float(x["clv_pct"]) for x in all_clv_rows]

    equity = peak = 0.0
    max_dd = 0.0
    net_equity = net_peak = 0.0
    net_max_dd = 0.0
    for x in sorted(settled, key=lambda r: (str(r["created_at"]), int(r.get("id") or 0))):
        gross = float(x["pnl_units"])
        net = float(x["net_pnl_units"]) if x.get("net_pnl_units") is not None else gross
        equity += gross
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
        net_equity += net
        net_peak = max(net_peak, net_equity)
        net_max_dd = min(net_max_dd, net_equity - net_peak)

    return {
        "bets": len(rows),
        "settled": len(settled),
        "wins": wins,
        "pnl_units": pnl,
        "roi_pct": (pnl / len(settled) * 100.0) if settled else None,
        "commission_units": commission,
        "net_pnl_units": net_pnl,
        "net_roi_pct": (net_pnl / len(settled) * 100.0) if settled else None,
        "win_rate_pct": (wins / len(settled) * 100.0) if settled else None,
        "avg_edge_pct": (sum(edges) / len(edges)) if edges else None,
        "avg_clv_pct": (sum(clvs) / len(clvs)) if clvs else None,
        "median_clv_pct": median(clvs) if clvs else None,
        "beat_close_pct": (
            sum(1 for x in clvs if x > 0) / len(clvs) * 100.0
        ) if clvs else None,
        "clv_samples": len(clvs),
        "all_avg_clv_pct": (sum(all_clvs) / len(all_clvs)) if all_clvs else None,
        "all_clv_samples": len(all_clvs),
        "max_drawdown_units": max_dd,
        "net_max_drawdown_units": net_max_dd,
    }

def sample_status(metrics: Mapping[str, Any]) -> Dict[str, str]:
    bets = int(metrics.get("bets") or 0)
    settled = int(metrics.get("settled") or 0)
    clv_n = int(metrics.get("clv_samples") or 0)

    evidence_n = min(settled, clv_n) if settled and clv_n else max(settled, clv_n)
    if bets < 10 or evidence_n < 5:
        level = "VERY EARLY"
        note = "Too little outcome/closing-price evidence for conclusions."
    elif bets < 30 or evidence_n < 15:
        level = "EARLY"
        note = "Directional only; one short run can still dominate the figures."
    elif bets < 75 or evidence_n < 40:
        level = "DEVELOPING"
        note = "Useful patterns may be emerging, but promotion decisions remain premature."
    elif bets < 150 or evidence_n < 80:
        level = "MEANINGFUL"
        note = "Enough evidence for serious comparison, still requiring robustness checks."
    else:
        level = "MATURE"
        note = "Large enough for stronger conclusions if performance is stable across segments."
    return {"level": level, "note": note}


def _segment(
    rows: List[Mapping[str, Any]],
    key_fn: Callable[[Mapping[str, Any]], str],
) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(key_fn(row))].append(row)
    out = []
    for name, items in groups.items():
        m = _metrics(items)
        out.append({
            "segment": name,
            **m,
            "sample_status": sample_status(m)["level"],
        })
    return sorted(out, key=lambda x: (-int(x["bets"]), x["segment"]))


def edge_bucket(edge: float) -> str:
    edge = float(edge)
    if edge < 4.0:
        return "<4%"
    if edge < 5.0:
        return "4–5%"
    if edge < 7.5:
        return "5–7.5%"
    if edge < 10.0:
        return "7.5–10%"
    return "10%+"


def count_bucket(value: int) -> str:
    value = int(value)
    if value <= 1:
        return "1"
    if value == 2:
        return "2"
    return "3+"


def odds_band(odds: float) -> str:
    odds = float(odds)
    if odds < 2.0:
        return "<2"
    if odds < 3.0:
        return "2–3"
    if odds < 5.0:
        return "3–5"
    if odds < 8.0:
        return "5–8"
    return "8+"


def time_to_kickoff_hours(row: Mapping[str, Any]) -> float:
    return (parse_iso(row["commence_time"]) - parse_iso(row["created_at"])).total_seconds() / 3600.0


def time_to_kickoff_bucket(row: Mapping[str, Any]) -> str:
    hours = time_to_kickoff_hours(row)
    if hours < 2:
        return "<2h"
    if hours < 6:
        return "2–6h"
    if hours < 24:
        return "6–24h"
    if hours < 72:
        return "1–3d"
    return "3d+"


def canonical_rows(db: Database) -> List[Dict[str, Any]]:
    return db.fetchall(
        """
        SELECT c.*,e.league,e.home_team,e.away_team,e.commence_time
        FROM execution_shadow_bets c
        JOIN events e ON e.event_id=c.event_id
        ORDER BY c.created_at ASC,c.id ASC
        """
    )


def segmentation_tables(db: Database) -> Dict[str, List[Dict[str, Any]]]:
    rows = canonical_rows(db)
    return {
        "edge": _segment(rows, lambda x: edge_bucket(float(x["edge_pct"]))),
        "strategy_agreement": _segment(rows, lambda x: count_bucket(int(x["strategy_count"]))),
        "bookmaker_agreement": _segment(rows, lambda x: count_bucket(int(x.get("execution_venue_count") or 1))),
        "market": _segment(rows, lambda x: MARKET_LABELS.get(x["market_key"], x["market_key"])),
        "league": _segment(rows, lambda x: x["league"]),
        "bookmaker": _segment(rows, lambda x: x["bookmaker_title"]),
        "odds_band": _segment(rows, lambda x: odds_band(float(x["offered_odds"]))),
        "time_to_kickoff": _segment(rows, time_to_kickoff_bucket),
        "clv_quality": _segment(
            rows, lambda x: str(x.get("clv_quality") or "PENDING")
        ),
    }


def edge_clv_calibration(db: Database) -> Dict[str, Any]:
    all_rows = [x for x in canonical_rows(db) if x.get("clv_pct") is not None]
    rows = [x for x in all_rows if _headline_clv_row(x)]
    overall_edge = (
        sum(float(x["edge_pct"]) for x in rows) / len(rows)
        if rows else None
    )
    overall_clv = (
        sum(float(x["clv_pct"]) for x in rows) / len(rows)
        if rows else None
    )

    groups: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[edge_bucket(float(row["edge_pct"]))].append(row)

    buckets = []
    order = {"<4%": 0, "4–5%": 1, "5–7.5%": 2, "7.5–10%": 3, "10%+": 4}
    for name, items in groups.items():
        avg_edge = sum(float(x["edge_pct"]) for x in items) / len(items)
        avg_clv = sum(float(x["clv_pct"]) for x in items) / len(items)
        m = _metrics(items)
        buckets.append({
            "segment": name,
            "clv_samples": len(items),
            "avg_model_edge_pct": avg_edge,
            "avg_final_clv_pct": avg_clv,
            "edge_minus_clv_pp": avg_edge - avg_clv,
            "pnl_units": m["pnl_units"],
            "roi_pct": m["roi_pct"],
            "beat_close_pct": m["beat_close_pct"],
        })
    buckets.sort(key=lambda x: order.get(x["segment"], 99))

    return {
        "samples": len(rows),
        "all_clv_samples": len(all_rows),
        "quality_rule": "A+B closes only (<=30 minutes before kickoff)",
        "avg_model_edge_pct": overall_edge,
        "avg_final_clv_pct": overall_clv,
        "edge_minus_clv_pp": (
            overall_edge - overall_clv
            if overall_edge is not None and overall_clv is not None
            else None
        ),
        "buckets": buckets,
    }


def fixture_exposure_analysis(db: Database) -> Dict[str, Any]:
    rows = canonical_rows(db)
    groups: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row["event_id"])].append(row)

    fixture_rows = []
    for event_id, items in groups.items():
        m = _metrics(items)
        first = items[0]
        fixture_rows.append({
            "event_id": event_id,
            "league": first["league"],
            "fixture": f"{first['home_team']} v {first['away_team']}",
            "commence_time": first["commence_time"],
            "bets": len(items),
            "gross_exposure_units": float(len(items)),
            "markets": ", ".join(sorted({
                MARKET_LABELS.get(x["market_key"], x["market_key"]) for x in items
            })),
            "selections": " | ".join(str(x["selection"]) for x in items),
            "settled": m["settled"],
            "pnl_units": m["pnl_units"],
            "roi_pct": m["roi_pct"],
            "avg_clv_pct": m["avg_clv_pct"],
            "max_drawdown_units": m["max_drawdown_units"],
        })

    fixture_rows.sort(
        key=lambda x: (-int(x["bets"]), str(x["commence_time"]), x["fixture"])
    )
    multi = [x for x in fixture_rows if int(x["bets"]) > 1]
    total_bets = len(rows)
    multi_bets = sum(int(x["bets"]) for x in multi)

    exposure_buckets = _segment(
        rows,
        lambda x: count_bucket(len(groups[str(x["event_id"])])),
    )

    return {
        "fixtures": len(fixture_rows),
        "multi_bet_fixtures": len(multi),
        "max_bets_one_fixture": max((int(x["bets"]) for x in fixture_rows), default=0),
        "bets_on_multi_bet_fixtures": multi_bets,
        "pct_bets_on_multi_bet_fixtures": (
            multi_bets / total_bets * 100.0 if total_bets else None
        ),
        "exposure_buckets": exposure_buckets,
        "top_fixtures": fixture_rows[:30],
    }


def clv_vs_results(db: Database) -> List[Dict[str, Any]]:
    rows = canonical_rows(db)
    def bucket(x):
        clv = x.get("clv_pct")
        if clv is None:
            return "CLV pending"
        if not _headline_clv_row(x):
            return "Low-quality close (C/STALE)"
        return "Positive CLV" if float(clv) > 0 else "Flat/negative CLV"
    return _segment(rows, bucket)


def _checkpoint_for_bet(
    db: Database,
    bet: Mapping[str, Any],
    minimum_elapsed: Optional[timedelta],
    *,
    latest_before_kickoff: bool = False,
) -> Optional[Dict[str, Any]]:
    observations = db.fetchall(
        """
        SELECT * FROM execution_price_observations
        WHERE execution_bet_id=?
        ORDER BY source_snapshot_at ASC,id ASC
        """,
        (bet["id"],),
    )
    if not observations:
        return None

    created = parse_iso(bet["created_at"])
    kickoff = parse_iso(bet["commence_time"])
    valid = []
    for obs in observations:
        ts = parse_iso(obs["source_snapshot_at"])
        if ts > kickoff:
            continue
        if minimum_elapsed is not None and ts < created + minimum_elapsed:
            continue
        valid.append(obs)

    if not valid:
        return None
    return valid[-1] if latest_before_kickoff else valid[0]


def price_checkpoint_summary(db: Database) -> List[Dict[str, Any]]:
    bets = canonical_rows(db)
    checkpoints = [
        ("30m", timedelta(minutes=30), False),
        ("2h", timedelta(hours=2), False),
        ("pre-kickoff", None, True),
    ]
    out = []
    for label, elapsed, latest in checkpoints:
        values = []
        for bet in bets:
            obs = _checkpoint_for_bet(
                db, bet, elapsed, latest_before_kickoff=latest
            )
            if obs:
                values.append(float(obs["move_vs_entry_pct"]))
        out.append({
            "checkpoint": label,
            "samples": len(values),
            "avg_move_pct": (sum(values) / len(values)) if values else None,
            "median_move_pct": median(values) if values else None,
            "positive_move_pct": (
                sum(1 for x in values if x > 0) / len(values) * 100.0
            ) if values else None,
        })
    return out


def rolling_metrics(
    db: Database,
    *,
    days: int = 7,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    rows = [
        x for x in canonical_rows(db)
        if start <= parse_iso(x["created_at"]) <= now
    ]
    m = _metrics(rows)
    return {
        "period_start": start.isoformat(),
        "period_end": now.isoformat(),
        **m,
        "sample_status": sample_status(m),
    }


def data_quality_alerts(
    db: Database,
    *,
    now: Optional[datetime] = None,
) -> List[Dict[str, str]]:
    now = now or datetime.now(timezone.utc)
    alerts: List[Dict[str, str]] = []

    orphan = db.fetchone(
        """
        SELECT COUNT(*) AS n
        FROM execution_shadow_bets c
        LEFT JOIN signals s ON s.id=c.source_signal_id
        WHERE s.id IS NULL
        """
    )
    if int((orphan or {}).get("n") or 0):
        alerts.append({
            "severity": "ERROR",
            "code": "ORPHAN_CANONICAL",
            "message": f"{int(orphan['n'])} execution-shadow bets have no source detection.",
        })

    no_clv = db.fetchone(
        """
        SELECT COUNT(*) AS n
        FROM execution_shadow_bets c
        JOIN events e ON e.event_id=c.event_id
        WHERE e.commence_time<=? AND c.clv_pct IS NULL
        """,
        (now.isoformat(),),
    )
    if int((no_clv or {}).get("n") or 0):
        alerts.append({
            "severity": "WARN",
            "code": "PAST_KICKOFF_NO_CLV",
            "message": f"{int(no_clv['n'])} execution-shadow bets are past kickoff without final CLV.",
        })

    stale = db.fetchone(
        """
        SELECT COUNT(*) AS n
        FROM execution_shadow_bets
        WHERE clv_quality='STALE' AND clv_pct IS NOT NULL
        """
    )
    if int((stale or {}).get("n") or 0):
        alerts.append({
            "severity": "INFO",
            "code": "STALE_CLV_SAMPLE",
            "message": (
                f"{int(stale['n'])} execution-shadow CLV samples are >60 minutes "
                "before kickoff and are excluded from headline CLV."
            ),
        })

    unsettled = db.fetchone(
        """
        SELECT COUNT(*) AS n
        FROM execution_shadow_bets c
        JOIN event_results r ON r.event_id=c.event_id
        WHERE c.status<>'SETTLED'
        """
    )
    if int((unsettled or {}).get("n") or 0):
        alerts.append({
            "severity": "WARN",
            "code": "RESULT_NOT_SETTLED",
            "message": f"{int(unsettled['n'])} execution-shadow bets have a stored result but remain unsettled.",
        })

    no_obs = db.fetchone(
        """
        SELECT COUNT(*) AS n
        FROM execution_shadow_bets c
        LEFT JOIN execution_price_observations p ON p.execution_bet_id=c.id
        WHERE p.id IS NULL
        """
    )
    if int((no_obs or {}).get("n") or 0):
        alerts.append({
            "severity": "INFO",
            "code": "NO_PRICE_FOLLOWUP",
            "message": f"{int(no_obs['n'])} execution-shadow bets do not yet have a follow-up price observation.",
        })

    if not db.is_postgres:
        alerts.append({
            "severity": "WARN",
            "code": "NON_PERSISTENT_DB",
            "message": "Database backend is SQLite; Railway research should use persistent Postgres.",
        })

    if not alerts:
        alerts.append({
            "severity": "OK",
            "code": "DATA_QUALITY_OK",
            "message": "No current integrity alerts.",
        })
    return alerts


def research_intelligence(
    db: Database,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    overall = _metrics(canonical_rows(db))
    return {
        "generated_at": now.isoformat(),
        "overall": overall,
        "sample_status": sample_status(overall),
        "rolling_7d": rolling_metrics(db, days=7, now=now),
        "segments": segmentation_tables(db),
        "edge_clv_calibration": edge_clv_calibration(db),
        "fixture_exposure": fixture_exposure_analysis(db),
        "clv_vs_results": clv_vs_results(db),
        "price_checkpoints": price_checkpoint_summary(db),
        "data_quality_alerts": data_quality_alerts(db, now=now),
        "research_governance": research_governance(db),
    }


def _iso_week_window(now: datetime) -> Tuple[datetime, datetime, str]:
    now = now.astimezone(timezone.utc)
    start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = start + timedelta(days=7)
    iso = start.isocalendar()
    key = f"WEEKLY:{iso.year}-W{iso.week:02d}"
    return start, end, key


def upsert_weekly_report(
    db: Database,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    start, end, key = _iso_week_window(now)
    rows = [
        x for x in canonical_rows(db)
        if start <= parse_iso(x["created_at"]) < end
    ]
    metrics = _metrics(rows)
    status = sample_status(metrics)
    intelligence = {
        "metrics": metrics,
        "sample_status": status,
        "segments": {
            name: _segment(rows, {
                "edge": lambda x: edge_bucket(float(x["edge_pct"])),
                "strategy_agreement": lambda x: count_bucket(int(x["strategy_count"])),
                "bookmaker_agreement": lambda x: count_bucket(int(x.get("execution_venue_count") or 1)),
                "market": lambda x: MARKET_LABELS.get(x["market_key"], x["market_key"]),
                "league": lambda x: x["league"],
                "bookmaker": lambda x: x["bookmaker_title"],
                "odds_band": lambda x: odds_band(float(x["offered_odds"])),
                "time_to_kickoff": time_to_kickoff_bucket,
                "clv_quality": lambda x: str(x.get("clv_quality") or "PENDING"),
            }[name])
            for name in (
                "edge","strategy_agreement","bookmaker_agreement",
                "market","league","bookmaker","odds_band","time_to_kickoff","clv_quality"
            )
        },
    }
    payload = json.dumps(intelligence, separators=(",", ":"))
    params = (
        key, start.isoformat(), end.isoformat(), utc_now_iso(), "WEEKLY",
        metrics["bets"], metrics["settled"], metrics["pnl_units"],
        metrics["roi_pct"], metrics["commission_units"], metrics["net_pnl_units"],
        metrics["net_roi_pct"], metrics["avg_clv_pct"], metrics["beat_close_pct"],
        status["level"], payload,
    )

    if db.is_postgres:
        db.execute(
            """
            INSERT INTO research_reports(
                report_key,period_start,period_end,generated_at,report_type,
                canonical_bets,settled_bets,pnl_units,roi_pct,
                commission_units,net_pnl_units,net_roi_pct,avg_clv_pct,
                beat_close_pct,sample_status,payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(report_key) DO UPDATE SET
                period_start=excluded.period_start,
                period_end=excluded.period_end,
                generated_at=excluded.generated_at,
                report_type=excluded.report_type,
                canonical_bets=excluded.canonical_bets,
                settled_bets=excluded.settled_bets,
                pnl_units=excluded.pnl_units,
                roi_pct=excluded.roi_pct,
                commission_units=excluded.commission_units,
                net_pnl_units=excluded.net_pnl_units,
                net_roi_pct=excluded.net_roi_pct,
                avg_clv_pct=excluded.avg_clv_pct,
                beat_close_pct=excluded.beat_close_pct,
                sample_status=excluded.sample_status,
                payload_json=excluded.payload_json
            """,
            params,
        )
    else:
        db.execute(
            """
            INSERT INTO research_reports(
                report_key,period_start,period_end,generated_at,report_type,
                canonical_bets,settled_bets,pnl_units,roi_pct,
                commission_units,net_pnl_units,net_roi_pct,avg_clv_pct,
                beat_close_pct,sample_status,payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(report_key) DO UPDATE SET
                period_start=excluded.period_start,
                period_end=excluded.period_end,
                generated_at=excluded.generated_at,
                report_type=excluded.report_type,
                canonical_bets=excluded.canonical_bets,
                settled_bets=excluded.settled_bets,
                pnl_units=excluded.pnl_units,
                roi_pct=excluded.roi_pct,
                commission_units=excluded.commission_units,
                net_pnl_units=excluded.net_pnl_units,
                net_roi_pct=excluded.net_roi_pct,
                avg_clv_pct=excluded.avg_clv_pct,
                beat_close_pct=excluded.beat_close_pct,
                sample_status=excluded.sample_status,
                payload_json=excluded.payload_json
            """,
            params,
        )
    return {
        "report_key": key,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        **metrics,
        "sample_status": status,
    }


def latest_weekly_reports(db: Database, limit: int = 12) -> List[Dict[str, Any]]:
    return db.fetchall(
        """
        SELECT * FROM research_reports
        WHERE report_type='WEEKLY'
        ORDER BY period_start DESC
        LIMIT ?
        """,
        (limit,),
    )


# Frozen research-governance thresholds. These are evidence labels only: they
# never alter collection, selection, staking or execution.
MATURITY_EARLY_MAX = 49
MATURITY_DEVELOPING_MAX = 149
PROMOTION_MIN_SETTLED = 150
PROMOTION_MIN_CLV = 100
PROMOTION_MIN_BEAT_CLOSE_PCT = 52.0


def _research_maturity(settled: int) -> str:
    n = int(settled or 0)
    if n <= MATURITY_EARLY_MAX:
        return "EARLY"
    if n <= MATURITY_DEVELOPING_MAX:
        return "DEVELOPING"
    return "MATURE"


def _governance_lane(
    name: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    pnl_field: str = "net_pnl_units",
    clv_field: str = "clv_pct",
    clv_quality_field: Optional[str] = "clv_quality",
    settled_field: Optional[str] = None,
    singles_pnl_field: Optional[str] = None,
    promotion_eligible: bool = True,
) -> Dict[str, Any]:
    items = list(rows)
    settled = []
    for r in items:
        if settled_field:
            if str(r.get(settled_field) or "").upper() != "SETTLED":
                continue
        elif r.get(pnl_field) is None:
            continue
        settled.append(r)
    settled = sorted(
        settled,
        key=lambda r: (str(r.get("settled_at") or r.get("created_at") or ""), int(r.get("id") or 0)),
    )
    pnls = [float(r.get(pnl_field) or 0.0) for r in settled]
    total = sum(pnls)
    roi = (total / len(pnls) * 100.0) if pnls else None

    clvs = []
    for r in items:
        if r.get(clv_field) is None:
            continue
        if clv_quality_field and str(r.get(clv_quality_field) or "").upper() not in {"A", "B"}:
            continue
        try:
            clvs.append(float(r[clv_field]))
        except Exception:
            pass
    clvs_sorted = sorted(clvs)
    nclv = len(clvs_sorted)
    med = (
        clvs_sorted[nclv // 2]
        if nclv % 2
        else ((clvs_sorted[nclv // 2 - 1] + clvs_sorted[nclv // 2]) / 2.0)
    ) if nclv else None
    beat = (sum(1 for v in clvs if v > 0) / nclv * 100.0) if nclv else None

    rolling = {}
    for window in (20, 50, 100):
        if len(pnls) >= window:
            sample = pnls[-window:]
            rolling[str(window)] = {
                "n": window,
                "pnl_units": sum(sample),
                "roi_pct": sum(sample) / window * 100.0,
            }
        else:
            rolling[str(window)] = {"n": len(pnls), "pnl_units": None, "roi_pct": None}

    positive = sorted((x for x in pnls if x > 0), reverse=True)
    positive_total = sum(positive)
    top1 = sum(positive[:1])
    top3 = sum(positive[:3])
    best1 = max(pnls) if pnls else 0.0
    top3_realized = sum(sorted(pnls, reverse=True)[:3]) if pnls else 0.0
    concentration = {
        "positive_pnl_units": positive_total,
        "top1_share_positive_pct": (top1 / positive_total * 100.0) if positive_total > 0 else None,
        "top3_share_positive_pct": (top3 / positive_total * 100.0) if positive_total > 0 else None,
        "net_pnl_without_best1": total - best1 if pnls else None,
        "net_pnl_without_best3": total - top3_realized if pnls else None,
    }

    gates = {
        "settled_150": len(settled) >= PROMOTION_MIN_SETTLED,
        "clv_100": nclv >= PROMOTION_MIN_CLV,
        "median_clv_positive": med is not None and med > 0,
        "beat_close_52": beat is not None and beat >= PROMOTION_MIN_BEAT_CLOSE_PCT,
        "lifetime_roi_positive": roi is not None and roi > 0,
        "rolling_50_roi_positive": rolling["50"]["roi_pct"] is not None and rolling["50"]["roi_pct"] > 0,
    }
    return {
        "name": name,
        "bets": len(items),
        "settled": len(settled),
        "maturity": _research_maturity(len(settled)),
        "net_pnl_units": total,
        "roi_pct": roi,
        "clv_samples": nclv,
        "median_clv_pct": med,
        "beat_close_pct": beat,
        "rolling": rolling,
        "concentration": concentration,
        "promotion_eligible": bool(promotion_eligible),
        "promotion_gates": gates if promotion_eligible else {},
        "promotion_gates_passed": sum(1 for v in gates.values() if v) if promotion_eligible else None,
        "promotion_gates_total": len(gates) if promotion_eligible else None,
        "candidate_ready": bool(promotion_eligible and all(gates.values())),
        "singles_control_pnl_units": (
            sum(float(r.get(singles_pnl_field) or 0.0) for r in settled)
            if singles_pnl_field else None
        ),
    }


def research_governance(db: Database) -> Dict[str, Any]:
    """Cross-lab stability, concentration and promotion-governance snapshot."""
    lanes: List[Dict[str, Any]] = []
    try:
        lanes.append(_governance_lane(
            "Football core",
            db.fetchall("SELECT * FROM execution_shadow_bets ORDER BY id"),
        ))
    except Exception:
        pass
    try:
        lanes.append(_governance_lane(
            "Tennis",
            db.fetchall("SELECT * FROM tennis_execution_bets ORDER BY id"),
        ))
    except Exception:
        pass
    try:
        ms = db.fetchall(
            """SELECT b.*,e.sport_family,e.league_title
               FROM multisport_execution_bets b
               JOIN multisport_events e ON e.event_id=b.event_id
               ORDER BY b.id"""
        )
        lanes.append(_governance_lane("Multi-Sport overall", ms))
        for family in sorted({str(r.get("sport_family") or "") for r in ms if r.get("sport_family")}):
            lanes.append(_governance_lane(
                str(family).replace("_", " ").title(),
                [r for r in ms if str(r.get("sport_family") or "") == family],
            ))
        for key, label in (
            ("americanfootball_nfl", "NFL"),
            ("basketball_euroleague", "EuroLeague"),
        ):
            sample = [r for r in ms if str(r.get("sport_key") or "") == key]
            if sample:
                lanes.append(_governance_lane(label, sample))
    except Exception:
        pass

    systems: List[Dict[str, Any]] = []
    try:
        manual = db.fetchall("SELECT * FROM manual_system_shadow_bets ORDER BY id")
        for system in ("HEINZ", "GOLIATH", "YANKEE"):
            sample = [r for r in manual if str(r.get("system_type") or "").upper() == system]
            if sample:
                systems.append(_governance_lane(
                    f"Manual {system}", sample,
                    pnl_field="system_pnl_units", settled_field="status",
                    singles_pnl_field="singles_pnl_units", promotion_eligible=False,
                ))
    except Exception:
        pass
    try:
        xs = db.fetchall("SELECT * FROM cross_sport_system_bets ORDER BY id")
        for system in ("HEINZ", "GOLIATH", "YANKEE"):
            sample = [r for r in xs if str(r.get("system_type") or "").upper() == system]
            if sample:
                systems.append(_governance_lane(
                    f"Cross-sport {system}", sample,
                    pnl_field="system_pnl_units", clv_field="avg_leg_clv_pct",
                    clv_quality_field=None, settled_field="status",
                    singles_pnl_field="singles_pnl_units", promotion_eligible=False,
                ))
    except Exception:
        pass

    return {
        "feature_freeze": True,
        "feature_freeze_policy": (
            "No new strategies, sports, odds bands, models or multiple types during the observation period. "
            "Only bug fixes, data-quality repairs and previously agreed research measurement are in scope."
        ),
        "maturity_thresholds": {
            "EARLY": "0-49 settled",
            "DEVELOPING": "50-149 settled",
            "MATURE": "150+ settled",
        },
        "promotion_policy": {
            "auto_promote": False,
            "min_settled": PROMOTION_MIN_SETTLED,
            "min_ab_clv_samples": PROMOTION_MIN_CLV,
            "median_clv_must_be_positive": True,
            "min_beat_close_pct": PROMOTION_MIN_BEAT_CLOSE_PCT,
            "lifetime_roi_must_be_positive": True,
            "rolling_50_roi_must_be_positive": True,
            "note": "Passing every gate makes a lane review-eligible only; live promotion still requires explicit approval.",
        },
        "lanes": lanes,
        "systems": systems,
    }
