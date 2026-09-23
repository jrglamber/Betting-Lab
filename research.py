from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional

from db import Database, utc_now_iso
from fair_value import clv_pct


def _nullable_match_sql(column: str, value: Any):
    if value is None or value == "":
        return f"{column} IS NULL", []
    return f"{column}=?", [value]


def record_evaluation(
    db: Database,
    *,
    evaluated_at: str,
    event_id: str,
    strategy: str,
    market_key: str,
    selection: Optional[str] = None,
    outcome_description: Optional[str] = None,
    point: Optional[float] = None,
    bookmaker_key: Optional[str] = None,
    bookmaker_title: Optional[str] = None,
    offered_odds: Optional[float] = None,
    fair_odds: Optional[float] = None,
    fair_probability: Optional[float] = None,
    edge_pct: Optional[float] = None,
    peer_books: Optional[int] = None,
    decision: str,
    reason: str,
    metadata: Optional[Mapping[str, Any]] = None,
) -> None:
    # Avoid duplicate audit rows if the same snapshot is reprocessed.
    existing = db.fetchone(
        """
        SELECT id FROM candidate_evaluations
        WHERE evaluated_at=? AND event_id=? AND strategy=? AND market_key=?
          AND COALESCE(selection,'')=COALESCE(?, '')
          AND COALESCE(bookmaker_key,'')=COALESCE(?, '')
          AND decision=? AND reason=?
        LIMIT 1
        """,
        (
            evaluated_at, event_id, strategy, market_key,
            selection, bookmaker_key, decision, reason,
        ),
    )
    if existing:
        return
    db.execute(
        """
        INSERT INTO candidate_evaluations(
            evaluated_at,event_id,strategy,market_key,selection,
            outcome_description,point,bookmaker_key,bookmaker_title,
            offered_odds,fair_odds,fair_probability,edge_pct,peer_books,
            decision,reason,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            evaluated_at,event_id,strategy,market_key,selection,
            outcome_description,point,bookmaker_key,bookmaker_title,
            offered_odds,fair_odds,fair_probability,edge_pct,peer_books,
            decision,reason,json.dumps(dict(metadata or {})),
        ),
    )


def _matching_latest_quote(db: Database, signal: Mapping[str, Any], *, before: Optional[str] = None):
    params: List[Any] = [
        signal["event_id"], signal["bookmaker_key"],
        signal["market_key"], signal["selection"],
    ]
    clauses = [
        "event_id=?", "bookmaker_key=?", "market_key=?", "outcome_name=?",
    ]
    if signal.get("outcome_description"):
        clauses.append("outcome_description=?")
        params.append(signal["outcome_description"])
    else:
        clauses.append("outcome_description IS NULL")
    if signal.get("point") is not None:
        clauses.append("point=?")
        params.append(signal["point"])
    else:
        clauses.append("point IS NULL")
    if before:
        clauses.append("captured_at<=?")
        params.append(before)
    clauses.append("captured_at>=?")
    params.append(signal["created_at"])
    return db.fetchone(
        f"""
        SELECT price,captured_at FROM odds_snapshots
        WHERE {' AND '.join(clauses)}
        ORDER BY captured_at DESC LIMIT 1
        """,
        params,
    )


def track_signal_prices(db: Database) -> int:
    """Capture current price evolution for open signals without calling it CLV."""
    signals = db.fetchall(
        """
        SELECT s.* FROM signals s
        WHERE s.status='OPEN'
        ORDER BY s.id ASC
        """
    )
    inserted = 0
    for sig in signals:
        row = _matching_latest_quote(db, sig)
        if not row:
            continue
        already = db.fetchone(
            """
            SELECT id FROM signal_price_observations
            WHERE signal_id=? AND source_snapshot_at=?
            LIMIT 1
            """,
            (sig["id"], row["captured_at"]),
        )
        if already:
            continue
        offered = float(sig["offered_odds"])
        price = float(row["price"])
        # Positive means the market shortened after our entry price.
        move = (offered / price - 1.0) * 100.0
        db.execute(
            """
            INSERT INTO signal_price_observations(
                signal_id,observed_at,source_snapshot_at,bookmaker_key,
                price,move_vs_entry_pct
            ) VALUES(?,?,?,?,?,?)
            """,
            (sig["id"], utc_now_iso(), row["captured_at"], sig["bookmaker_key"], price, move),
        )
        inserted += 1
    return inserted


def repair_premature_clv(db: Database, now: Optional[datetime] = None) -> int:
    """Undo pre-v0.5 CLV values that were written before a future fixture kicked off."""
    now = now or datetime.now(timezone.utc)
    rows = db.fetchall(
        """
        SELECT s.id FROM signals s
        JOIN events e ON e.event_id=s.event_id
        WHERE s.status='OPEN' AND s.closing_odds IS NOT NULL AND e.commence_time>?
        """,
        (now.isoformat(),),
    )
    for row in rows:
        db.execute("UPDATE signals SET closing_odds=NULL,clv_pct=NULL WHERE id=?", (row["id"],))
    return len(rows)


def finalize_closing_lines(db: Database, now: Optional[datetime] = None) -> int:
    """Only finalise CLV after kickoff; pre-kickoff prices remain observations."""
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    signals = db.fetchall(
        """
        SELECT s.*,e.commence_time
        FROM signals s
        JOIN events e ON e.event_id=s.event_id
        WHERE s.closing_odds IS NULL AND e.commence_time<=?
        ORDER BY s.id ASC
        """,
        (now_iso,),
    )
    updated = 0
    for sig in signals:
        row = _matching_latest_quote(db, sig, before=sig["commence_time"])
        if not row:
            continue
        closing = float(row["price"])
        value = clv_pct(float(sig["offered_odds"]), closing)
        db.execute(
            """
            UPDATE signals SET closing_odds=?,clv_pct=? WHERE id=?
            """,
            (closing, value, sig["id"]),
        )
        updated += 1
    return updated


def signal_price_history(db: Database, signal_id: int) -> List[Dict[str, Any]]:
    return db.fetchall(
        """
        SELECT * FROM signal_price_observations
        WHERE signal_id=? ORDER BY source_snapshot_at ASC
        """,
        (signal_id,),
    )


def strategy_scoreboard(db: Database) -> List[Dict[str, Any]]:
    rows = db.fetchall("SELECT * FROM signals ORDER BY created_at ASC,id ASC")
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row["strategy"]].append(row)

    out = []
    for strategy, items in sorted(groups.items()):
        settled = [x for x in items if x.get("pnl_units") is not None]
        clvs = [float(x["clv_pct"]) for x in items if x.get("clv_pct") is not None]
        edges = [float(x["edge_pct"]) for x in items if x.get("edge_pct") is not None]
        pnl = sum(float(x["pnl_units"]) for x in settled)
        wins = sum(1 for x in settled if float(x["pnl_units"]) > 0)
        peak = 0.0
        equity = 0.0
        max_dd = 0.0
        for x in settled:
            equity += float(x["pnl_units"])
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
        out.append({
            "strategy": strategy,
            "signals": len(items),
            "settled": len(settled),
            "pnl_units": pnl,
            "roi_pct": (pnl / len(settled) * 100.0) if settled else None,
            "win_rate_pct": (wins / len(settled) * 100.0) if settled else None,
            "avg_edge_pct": (sum(edges)/len(edges)) if edges else None,
            "avg_clv_pct": (sum(clvs)/len(clvs)) if clvs else None,
            "median_clv_pct": median(clvs) if clvs else None,
            "beat_close_pct": (sum(1 for x in clvs if x > 0)/len(clvs)*100.0) if clvs else None,
            "clv_samples": len(clvs),
            "max_drawdown_units": max_dd,
        })
    return out


def rejection_summary(db: Database) -> List[Dict[str, Any]]:
    rows = db.fetchall(
        """
        SELECT strategy,reason,COUNT(*) AS count
        FROM candidate_evaluations
        WHERE decision='REJECT'
        GROUP BY strategy,reason
        ORDER BY count DESC,strategy,reason
        """
    )
    return rows


def latest_evaluations(db: Database, limit: int = 100) -> List[Dict[str, Any]]:
    return db.fetchall(
        """
        SELECT c.*,e.league,e.home_team,e.away_team,e.commence_time
        FROM candidate_evaluations c
        JOIN events e ON e.event_id=c.event_id
        ORDER BY c.id DESC LIMIT ?
        """,
        (limit,),
    )


def event_market_snapshot(db: Database, event_id: str) -> Dict[str, Any]:
    event = db.fetchone("SELECT * FROM events WHERE event_id=?", (event_id,))
    latest = db.fetchone(
        "SELECT MAX(captured_at) AS captured_at FROM odds_snapshots WHERE event_id=?",
        (event_id,),
    )
    captured_at = (latest or {}).get("captured_at")
    rows = []
    if captured_at:
        rows = db.fetchall(
            """
            SELECT * FROM odds_snapshots
            WHERE event_id=? AND captured_at=?
            ORDER BY market_key,point,outcome_name,bookmaker_title
            """,
            (event_id, captured_at),
        )
    return {"event": event, "captured_at": captured_at, "quotes": rows}


def event_price_history(db: Database, event_id: str, limit: int = 2000) -> List[Dict[str, Any]]:
    return db.fetchall(
        """
        SELECT captured_at,bookmaker_key,bookmaker_title,market_key,
               outcome_name,outcome_description,point,price
        FROM odds_snapshots
        WHERE event_id=?
        ORDER BY captured_at ASC,id ASC LIMIT ?
        """,
        (event_id, limit),
    )
