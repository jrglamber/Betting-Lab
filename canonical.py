from __future__ import annotations

from collections import defaultdict
import json
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional

from db import Database


def canonical_bet_key(signal: Mapping[str, Any]) -> str:
    desc = signal.get("outcome_description") or ""
    point = "" if signal.get("point") is None else f"{float(signal['point']):.6f}"
    return "|".join([
        str(signal["event_id"]),
        str(signal["market_key"]),
        str(signal["selection"]),
        str(desc),
        point,
    ])


def _group_signals(rows: Iterable[Mapping[str, Any]]):
    groups: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[canonical_bet_key(row)].append(row)
    return groups


def _metadata(items: List[Mapping[str, Any]]) -> Dict[str, Any]:
    strategies = sorted({str(x["strategy"]) for x in items})
    bookmakers = sorted({str(x["bookmaker_key"]) for x in items})
    return {
        "strategy_count": len(strategies),
        "bookmaker_count": len(bookmakers),
        "detection_count": len(items),
        "strategies_json": json.dumps(strategies),
    }


def cluster_event_signals(db: Database, event_id: str) -> int:
    """
    Convert overlapping raw strategy/bookmaker detections into one canonical
    theoretical wager per event + market + selection + line.

    Entry is frozen at the best price available at the FIRST signal timestamp.
    Later detections only enrich strategy/bookmaker agreement counts; they do
    not rewrite the historical entry price.
    """
    rows = db.fetchall(
        "SELECT * FROM signals WHERE event_id=? ORDER BY created_at ASC,id ASC",
        (event_id,),
    )
    if not rows:
        return 0

    created = 0
    for bet_key, items in _group_signals(rows).items():
        meta = _metadata(items)
        existing = db.fetchone(
            "SELECT * FROM canonical_bets WHERE bet_key=?",
            (bet_key,),
        )
        if existing:
            db.execute(
                """
                UPDATE canonical_bets
                SET strategy_count=?,bookmaker_count=?,detection_count=?,strategies_json=?
                WHERE id=?
                """,
                (
                    meta["strategy_count"], meta["bookmaker_count"],
                    meta["detection_count"], meta["strategies_json"],
                    existing["id"],
                ),
            )
            continue

        first_time = min(str(x["created_at"]) for x in items)
        first_wave = [x for x in items if str(x["created_at"]) == first_time]
        # Best available price in the first actionable wave. Tie-break on
        # strongest edge, then lowest signal id for deterministic behavior.
        chosen = sorted(
            first_wave,
            key=lambda x: (
                -float(x["offered_odds"]),
                -float(x.get("edge_pct") or 0.0),
                int(x["id"]),
            ),
        )[0]

        db.execute(
            """
            INSERT INTO canonical_bets(
                bet_key,created_at,event_id,market_key,selection,
                outcome_description,point,source_signal_id,
                bookmaker_key,bookmaker_title,offered_odds,fair_odds,
                fair_probability,edge_pct,strategy_count,bookmaker_count,
                detection_count,strategies_json,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                bet_key, chosen["created_at"], chosen["event_id"],
                chosen["market_key"], chosen["selection"],
                chosen.get("outcome_description"), chosen.get("point"),
                chosen["id"], chosen["bookmaker_key"], chosen["bookmaker_title"],
                chosen["offered_odds"], chosen["fair_odds"],
                chosen["fair_probability"], chosen["edge_pct"],
                meta["strategy_count"], meta["bookmaker_count"],
                meta["detection_count"], meta["strategies_json"], "OPEN",
            ),
        )
        created += 1
    return created


def backfill_canonical_bets(db: Database) -> int:
    events = db.fetchall("SELECT DISTINCT event_id FROM signals ORDER BY event_id")
    created = 0
    for row in events:
        created += cluster_event_signals(db, row["event_id"])
    return created


def sync_canonical_bets(db: Database) -> int:
    """Mirror closing/result fields from the frozen source signal."""
    rows = db.fetchall(
        """
        SELECT c.id,c.status AS c_status,s.closing_odds,s.clv_pct,s.result,s.pnl_units,s.status
        FROM canonical_bets c
        JOIN signals s ON s.id=c.source_signal_id
        ORDER BY c.id
        """
    )
    updated = 0
    for row in rows:
        db.execute(
            """
            UPDATE canonical_bets
            SET closing_odds=?,clv_pct=?,
                result=COALESCE(result,?),
                pnl_units=COALESCE(pnl_units,?),
                status=CASE WHEN ?='SETTLED' THEN 'SETTLED' ELSE status END
            WHERE id=?
            """,
            (
                row.get("closing_odds"), row.get("clv_pct"),
                row.get("result"), row.get("pnl_units"),
                row.get("status"), row["id"],
            ),
        )
        updated += 1
    return updated


def canonical_scoreboard(db: Database) -> Dict[str, Any]:
    rows = db.fetchall("SELECT * FROM canonical_bets ORDER BY created_at ASC,id ASC")
    settled = [x for x in rows if x.get("pnl_units") is not None]
    pnl = sum(float(x["pnl_units"]) for x in settled)
    wins = sum(1 for x in settled if float(x["pnl_units"]) > 0)
    clvs = [float(x["clv_pct"]) for x in rows if x.get("clv_pct") is not None]
    edges = [float(x["edge_pct"]) for x in rows if x.get("edge_pct") is not None]

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for x in settled:
        equity += float(x["pnl_units"])
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    return {
        "bets": len(rows),
        "settled": len(settled),
        "wins": wins,
        "pnl_units": pnl,
        "roi_pct": (pnl / len(settled) * 100.0) if settled else None,
        "win_rate_pct": (wins / len(settled) * 100.0) if settled else None,
        "avg_edge_pct": (sum(edges) / len(edges)) if edges else None,
        "avg_clv_pct": (sum(clvs) / len(clvs)) if clvs else None,
        "median_clv_pct": median(clvs) if clvs else None,
        "beat_close_pct": (
            sum(1 for x in clvs if x > 0) / len(clvs) * 100.0
        ) if clvs else None,
        "clv_samples": len(clvs),
        "max_drawdown_units": max_dd,
    }


def latest_canonical_bets(db: Database, limit: int = 50) -> List[Dict[str, Any]]:
    return db.fetchall(
        """
        SELECT c.*,e.league,e.home_team,e.away_team,e.commence_time,
               (SELECT spo.move_vs_entry_pct
                FROM signal_price_observations spo
                WHERE spo.signal_id=c.source_signal_id
                ORDER BY spo.source_snapshot_at DESC LIMIT 1) AS latest_move_pct
        FROM canonical_bets c
        JOIN events e ON e.event_id=c.event_id
        ORDER BY c.id DESC LIMIT ?
        """,
        (limit,),
    )
