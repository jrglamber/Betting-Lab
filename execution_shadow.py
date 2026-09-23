from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from db import Database, utc_now_iso
from fair_value import clv_pct, edge_pct
from instrumentation import (
    SINGLES_EXPERIMENT_VERSION, entry_market_metrics, experiment_fingerprint,
)


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def clv_quality(minutes_before_kickoff: float) -> str:
    minutes = max(0.0, float(minutes_before_kickoff))
    if minutes <= 15.0:
        return "A"
    if minutes <= 30.0:
        return "B"
    if minutes <= 60.0:
        return "C"
    return "STALE"


def is_headline_clv_quality(value: Optional[str]) -> bool:
    return str(value or "").upper() in {"A", "B"}


def execution_commission_pct(bookmaker_key: str) -> float:
    from config import settings
    key = str(bookmaker_key)
    if key == "betfair_ex_uk":
        return max(0.0, float(settings.betfair_commission_pct))
    if key == "matchbook":
        return max(0.0, float(settings.matchbook_commission_pct))
    if key == "smarkets":
        return max(0.0, float(settings.smarkets_commission_pct))
    return max(0.0, float(settings.default_execution_commission_pct))


def commission_adjusted_pnl(
    gross_pnl_units: float,
    bookmaker_key: str,
) -> Tuple[float, float, float]:
    """Research estimate only; gross P&L is always retained separately."""
    gross = float(gross_pnl_units)
    rate_pct = execution_commission_pct(bookmaker_key)
    commission = max(0.0, gross) * (rate_pct / 100.0)
    return rate_pct, commission, gross - commission


def backfill_execution_accounting(db: Database) -> int:
    rows = db.fetchall(
        "SELECT * FROM execution_shadow_bets WHERE pnl_units IS NOT NULL ORDER BY id ASC"
    )
    updated = 0
    for bet in rows:
        rate, commission, net = commission_adjusted_pnl(
            float(bet["pnl_units"]), str(bet["bookmaker_key"])
        )
        db.execute(
            """
            UPDATE execution_shadow_bets
            SET commission_rate_pct=?,commission_units=?,net_pnl_units=?
            WHERE id=?
            """,
            (rate, commission, net, bet["id"]),
        )
        updated += 1
    return updated


def execution_key(row: Mapping[str, Any]) -> str:
    desc = row.get("outcome_description") or ""
    point = "" if row.get("point") is None else f"{float(row['point']):.6f}"
    return "|".join([
        str(row["event_id"]), str(row["market_key"]), str(row["selection"]),
        str(desc), point,
    ])


def _row_key(row: Mapping[str, Any], *, quote: bool = False) -> Tuple[str, str, str, Optional[float]]:
    if quote:
        selection = str(row["outcome_name"])
        desc = row.get("outcome_description") or ""
    else:
        selection = str(row["selection"])
        desc = row.get("outcome_description") or ""
    point = None if row.get("point") is None else float(row["point"])
    return str(row["market_key"]), selection, desc, point


def _all_support_metadata(db: Database, example: Mapping[str, Any], through_time: Optional[str] = None) -> Dict[str, Any]:
    rows = db.fetchall(
        "SELECT * FROM signals WHERE event_id=? ORDER BY created_at ASC,id ASC",
        (example["event_id"],),
    )
    key = _row_key(example)
    items = [x for x in rows if _row_key(x) == key and (through_time is None or str(x["created_at"]) <= str(through_time))]
    strategies = sorted({str(x["strategy"]) for x in items})
    books = sorted({str(x["bookmaker_key"]) for x in items})
    return {
        "strategy_count": len(strategies),
        "bookmaker_count": len(books),
        "detection_count": len(items),
        "strategies_json": json.dumps(strategies),
    }


def _record_execution_evaluation(
    db: Database, *, evaluated_at: str, event_id: str, key: str,
    market_key: str, selection: str, outcome_description: Optional[str], point: Optional[float],
    approved_books_seen: int, best_executable_odds: Optional[float], min_required_odds: Optional[float],
    fair_odds: Optional[float], decision: str, reason: str, metadata: Optional[Mapping[str, Any]] = None,
) -> None:
    existing = db.fetchone(
        """
        SELECT id FROM execution_evaluations
        WHERE evaluated_at=? AND execution_key=? AND decision=? AND reason=?
        LIMIT 1
        """,
        (evaluated_at, key, decision, reason),
    )
    if existing:
        return
    db.execute(
        """
        INSERT INTO execution_evaluations(
            evaluated_at,event_id,execution_key,market_key,selection,outcome_description,point,
            approved_books_seen,best_executable_odds,min_required_odds,fair_odds,decision,reason,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            evaluated_at,event_id,key,market_key,selection,outcome_description,point,
            approved_books_seen,best_executable_odds,min_required_odds,fair_odds,decision,reason,
            json.dumps(dict(metadata or {}), separators=(",", ":")),
        ),
    )


def evaluate_execution_wave_at(
    db: Database,
    event_id: str,
    captured_at: str,
    approved_bookmaker_keys: Sequence[str],
) -> int:
    approved = set(str(x) for x in approved_bookmaker_keys)
    signals = db.fetchall(
        "SELECT * FROM signals WHERE event_id=? AND created_at=? ORDER BY id ASC",
        (event_id, captured_at),
    )
    if not signals:
        return 0
    quotes = db.fetchall(
        "SELECT * FROM odds_snapshots WHERE event_id=? AND captured_at=? ORDER BY id ASC",
        (event_id, captured_at),
    )
    quote_groups: Dict[Tuple[str,str,str,Optional[float]], List[Mapping[str,Any]]] = defaultdict(list)
    for q in quotes:
        quote_groups[_row_key(q, quote=True)].append(q)

    groups: Dict[str, List[Mapping[str,Any]]] = defaultdict(list)
    for sig in signals:
        groups[execution_key(sig)].append(sig)

    created = 0
    for key, wave in groups.items():
        existing = db.fetchone("SELECT * FROM execution_shadow_bets WHERE execution_key=?", (key,))
        meta = _all_support_metadata(db, wave[0], through_time=captured_at)
        if existing:
            db.execute(
                """
                UPDATE execution_shadow_bets
                SET strategy_count=?,bookmaker_count=?,detection_count=?,strategies_json=?
                WHERE id=?
                """,
                (meta["strategy_count"],meta["bookmaker_count"],meta["detection_count"],meta["strategies_json"],existing["id"]),
            )
            continue

        qgroup = quote_groups.get(_row_key(wave[0]), [])
        approved_quotes = [q for q in qgroup if str(q["bookmaker_key"]) in approved]
        best_ref = max(qgroup, key=lambda q: float(q["price"])) if qgroup else None
        min_required = min(float(s["min_odds"]) for s in wave)
        fair_for_display = min(float(s["fair_odds"]) for s in wave)
        if not approved_quotes:
            _record_execution_evaluation(
                db,evaluated_at=captured_at,event_id=event_id,key=key,market_key=wave[0]["market_key"],
                selection=wave[0]["selection"],outcome_description=wave[0].get("outcome_description"),point=wave[0].get("point"),
                approved_books_seen=0,best_executable_odds=None,min_required_odds=min_required,fair_odds=fair_for_display,
                decision="REJECT",reason="NO_APPROVED_VENUE_QUOTE",
                metadata={"approved_keys": sorted(approved)},
            )
            continue

        best_exec = max(approved_quotes, key=lambda q: float(q["price"]))
        acceptable_pairs = []
        for q in approved_quotes:
            qprice = float(q["price"])
            for sig in wave:
                if qprice + 1e-12 >= float(sig["min_odds"]):
                    execution_edge = edge_pct(float(sig["fair_probability"]), qprice)
                    acceptable_pairs.append((qprice, execution_edge, -int(sig["id"]), q, sig))
        if not acceptable_pairs:
            _record_execution_evaluation(
                db,evaluated_at=captured_at,event_id=event_id,key=key,market_key=wave[0]["market_key"],
                selection=wave[0]["selection"],outcome_description=wave[0].get("outcome_description"),point=wave[0].get("point"),
                approved_books_seen=len({str(q["bookmaker_key"]) for q in approved_quotes}),
                best_executable_odds=float(best_exec["price"]),min_required_odds=min_required,fair_odds=fair_for_display,
                decision="REJECT",reason="EXECUTABLE_PRICE_BELOW_MIN",
                metadata={"best_reference_odds": float(best_ref["price"]) if best_ref else None},
            )
            continue

        _, exec_edge, _, chosen_quote, chosen_sig = sorted(acceptable_pairs, reverse=True, key=lambda x: (x[0],x[1],x[2]))[0]
        canonical = db.fetchone("SELECT id FROM canonical_bets WHERE bet_key=?", (key,))
        reference_odds = float(best_ref["price"]) if best_ref else None
        gap = ((reference_odds / float(chosen_quote["price"]) - 1.0) * 100.0) if reference_odds else None
        from config import settings
        strategy_version = "+".join(json.loads(meta["strategies_json"]) or []) or str(chosen_sig["strategy"])
        fingerprint = experiment_fingerprint(
            settings,
            experiment_version=SINGLES_EXPERIMENT_VERSION,
            strategy_version=strategy_version,
        )
        market_metrics = entry_market_metrics(
            db,
            event_id=event_id,
            captured_at=captured_at,
            market_key=str(chosen_sig["market_key"]),
            selection=str(chosen_sig["selection"]),
            point=chosen_sig.get("point"),
            outcome_description=chosen_sig.get("outcome_description"),
            chosen_odds=float(chosen_quote["price"]),
        )
        db.execute(
            """
            INSERT INTO execution_shadow_bets(
                execution_key,canonical_bet_id,created_at,event_id,market_key,selection,outcome_description,point,
                source_signal_id,bookmaker_key,bookmaker_title,offered_odds,fair_odds,fair_probability,edge_pct,min_odds,execution_venue_count,
                strategy_count,bookmaker_count,detection_count,strategies_json,
                reference_best_odds,reference_best_bookmaker_key,reference_best_bookmaker_title,gap_to_reference_pct,status,
                app_version,experiment_version,strategy_version,config_hash,
                entry_consensus_bookmaker_count,entry_consensus_median_odds,entry_consensus_best_odds,
                entry_price_dispersion_pct,entry_chosen_vs_median_pct,entry_mean_overround_pct
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                key,(canonical or {}).get("id"),captured_at,event_id,chosen_sig["market_key"],chosen_sig["selection"],
                chosen_sig.get("outcome_description"),chosen_sig.get("point"),chosen_sig["id"],chosen_quote["bookmaker_key"],
                chosen_quote["bookmaker_title"],float(chosen_quote["price"]),float(chosen_sig["fair_odds"]),
                float(chosen_sig["fair_probability"]),float(exec_edge),float(chosen_sig["min_odds"]),
                len({str(pair[3]["bookmaker_key"]) for pair in acceptable_pairs}),
                meta["strategy_count"],meta["bookmaker_count"],meta["detection_count"],meta["strategies_json"],
                reference_odds,(best_ref or {}).get("bookmaker_key"),(best_ref or {}).get("bookmaker_title"),gap,"OPEN",
                fingerprint["app_version"],fingerprint["experiment_version"],fingerprint["strategy_version"],fingerprint["config_hash"],
                market_metrics["bookmaker_count"],market_metrics["median_odds"],market_metrics["best_odds"],
                market_metrics["price_dispersion_pct"],market_metrics["chosen_vs_median_pct"],market_metrics["mean_overround_pct"],
            ),
        )
        _record_execution_evaluation(
            db,evaluated_at=captured_at,event_id=event_id,key=key,market_key=chosen_sig["market_key"],selection=chosen_sig["selection"],
            outcome_description=chosen_sig.get("outcome_description"),point=chosen_sig.get("point"),
            approved_books_seen=len({str(q["bookmaker_key"]) for q in approved_quotes}),
            best_executable_odds=float(chosen_quote["price"]),min_required_odds=float(chosen_sig["min_odds"]),fair_odds=float(chosen_sig["fair_odds"]),
            decision="ACCEPT",reason="APPROVED_VENUE_PRICE_PASSED",
            metadata={"bookmaker_key":chosen_quote["bookmaker_key"],"reference_best_odds":reference_odds,"execution_edge_pct":exec_edge},
        )
        created += 1
    return created


def evaluate_latest_execution_wave(db: Database, event_id: str, approved_bookmaker_keys: Sequence[str]) -> int:
    row = db.fetchone("SELECT MAX(created_at) AS captured_at FROM signals WHERE event_id=?", (event_id,))
    if not row or not row.get("captured_at"):
        return 0
    return evaluate_execution_wave_at(db,event_id,row["captured_at"],approved_bookmaker_keys)


def backfill_execution_shadows(db: Database, approved_bookmaker_keys: Sequence[str]) -> int:
    rows = db.fetchall("SELECT DISTINCT event_id,created_at FROM signals ORDER BY created_at ASC,event_id ASC")
    created = 0
    for row in rows:
        created += evaluate_execution_wave_at(db,row["event_id"],row["created_at"],approved_bookmaker_keys)
    return created


def _matching_quotes(db: Database, bet: Mapping[str,Any]) -> List[Dict[str,Any]]:
    rows = db.fetchall(
        """
        SELECT * FROM odds_snapshots
        WHERE event_id=? AND bookmaker_key=? AND market_key=? AND outcome_name=?
          AND captured_at>=?
        ORDER BY captured_at ASC,id ASC
        """,
        (bet["event_id"],bet["bookmaker_key"],bet["market_key"],bet["selection"],bet["created_at"]),
    )
    want_desc = bet.get("outcome_description") or ""
    want_point = None if bet.get("point") is None else float(bet["point"])
    out=[]
    for row in rows:
        desc=row.get("outcome_description") or ""
        point=None if row.get("point") is None else float(row["point"])
        if desc==want_desc and point==want_point:
            out.append(row)
    return out


def track_execution_prices(db: Database, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    bets = db.fetchall(
        """
        SELECT x.*,e.commence_time FROM execution_shadow_bets x
        JOIN events e ON e.event_id=x.event_id
        ORDER BY x.id ASC
        """
    )
    inserted=0
    for bet in bets:
        kickoff=parse_iso(bet["commence_time"])
        for q in _matching_quotes(db,bet):
            ts=parse_iso(q["captured_at"])
            if ts <= parse_iso(bet["created_at"]) or ts > min(now,kickoff):
                continue
            exists=db.fetchone(
                "SELECT id FROM execution_price_observations WHERE execution_bet_id=? AND source_snapshot_at=? LIMIT 1",
                (bet["id"],q["captured_at"]),
            )
            if exists: continue
            move=(float(bet["offered_odds"])/float(q["price"])-1.0)*100.0
            db.execute(
                """
                INSERT INTO execution_price_observations(
                    execution_bet_id,observed_at,source_snapshot_at,bookmaker_key,price,move_vs_entry_pct
                ) VALUES(?,?,?,?,?,?)
                """,
                (bet["id"],utc_now_iso(),q["captured_at"],bet["bookmaker_key"],q["price"],move),
            )
            inserted+=1
    return inserted



def finalize_execution_clv(db: Database, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    bets=db.fetchall(
        """
        SELECT x.*,e.commence_time FROM execution_shadow_bets x
        JOIN events e ON e.event_id=x.event_id
        WHERE x.clv_pct IS NULL
           OR x.clv_quality IS NULL
           OR x.closing_observed_at IS NULL
           OR x.closing_minutes_before_kickoff IS NULL
        ORDER BY x.id ASC
        """
    )
    updated=0
    for bet in bets:
        kickoff=parse_iso(bet["commence_time"])
        if kickoff > now:
            continue
        quotes=[
            q for q in _matching_quotes(db,bet)
            if parse_iso(q["captured_at"]) <= kickoff
        ]
        if not quotes:
            continue
        close=max(quotes,key=lambda q: parse_iso(q["captured_at"]))
        close_at=parse_iso(close["captured_at"])
        minutes=max(0.0,(kickoff-close_at).total_seconds()/60.0)
        closing=float(close["price"])
        db.execute(
            """
            UPDATE execution_shadow_bets
            SET closing_odds=?,clv_pct=?,closing_observed_at=?,
                closing_minutes_before_kickoff=?,clv_quality=?
            WHERE id=?
            """,
            (
                closing,
                clv_pct(float(bet["offered_odds"]),closing),
                close["captured_at"],
                minutes,
                clv_quality(minutes),
                bet["id"],
            ),
        )
        updated+=1
    return updated


def settle_execution_event(db: Database, event_id: str, *, home_score: int, away_score: int) -> int:
    from results import grade_signal
    event=db.fetchone("SELECT * FROM events WHERE event_id=?",(event_id,))
    if not event:
        return 0
    bets=db.fetchall(
        "SELECT * FROM execution_shadow_bets WHERE event_id=? AND status='OPEN' ORDER BY id ASC",
        (event_id,),
    )
    settled=0
    for bet in bets:
        result=grade_signal(
            bet,
            home_team=event["home_team"],
            away_team=event["away_team"],
            home_score=home_score,
            away_score=away_score,
        )
        gross=float(bet["offered_odds"])-1.0 if result=="WIN" else (-1.0 if result=="LOSS" else 0.0)
        rate, commission, net = commission_adjusted_pnl(
            gross, str(bet["bookmaker_key"])
        )
        db.execute(
            """
            UPDATE execution_shadow_bets
            SET result=?,pnl_units=?,commission_rate_pct=?,
                commission_units=?,net_pnl_units=?,status='SETTLED'
            WHERE id=?
            """,
            (result,gross,rate,commission,net,bet["id"]),
        )
        settled+=1
    return settled

def settle_execution_from_stored_results(db: Database) -> int:
    rows=db.fetchall(
        """
        SELECT r.event_id,r.home_score,r.away_score FROM event_results r
        WHERE EXISTS(SELECT 1 FROM execution_shadow_bets x WHERE x.event_id=r.event_id AND x.status='OPEN')
        """
    )
    total=0
    for r in rows:
        total+=settle_execution_event(db,r["event_id"],home_score=int(r["home_score"]),away_score=int(r["away_score"]))
    return total



def execution_scoreboard(db: Database) -> Dict[str,Any]:
    rows=db.fetchall("SELECT * FROM execution_shadow_bets ORDER BY created_at ASC,id ASC")
    settled=[x for x in rows if x.get("pnl_units") is not None]
    pnl=sum(float(x["pnl_units"]) for x in settled)
    net_pnl=sum(
        float(x["net_pnl_units"]) if x.get("net_pnl_units") is not None else float(x["pnl_units"])
        for x in settled
    )
    commission=sum(float(x.get("commission_units") or 0.0) for x in settled)
    wins=sum(1 for x in settled if float(x["pnl_units"])>0)
    all_clvs=[float(x["clv_pct"]) for x in rows if x.get("clv_pct") is not None]
    headline=[
        x for x in rows
        if x.get("clv_pct") is not None and is_headline_clv_quality(x.get("clv_quality"))
    ]
    clvs=[float(x["clv_pct"]) for x in headline]
    edges=[float(x["edge_pct"]) for x in rows if x.get("edge_pct") is not None]

    equity=peak=0.0; max_dd=0.0
    net_equity=net_peak=0.0; net_max_dd=0.0
    for x in settled:
        gross=float(x["pnl_units"])
        net=float(x["net_pnl_units"]) if x.get("net_pnl_units") is not None else gross
        equity+=gross; peak=max(peak,equity); max_dd=min(max_dd,equity-peak)
        net_equity+=net; net_peak=max(net_peak,net_equity); net_max_dd=min(net_max_dd,net_equity-net_peak)

    quality_counts={q:0 for q in ("A","B","C","STALE")}
    for x in rows:
        q=x.get("clv_quality")
        if q in quality_counts and x.get("clv_pct") is not None:
            quality_counts[q]+=1

    return {
        "bets":len(rows),"settled":len(settled),"wins":wins,"pnl_units":pnl,
        "roi_pct":(pnl/len(settled)*100.0) if settled else None,
        "commission_units":commission,
        "net_pnl_units":net_pnl,
        "net_roi_pct":(net_pnl/len(settled)*100.0) if settled else None,
        "win_rate_pct":(wins/len(settled)*100.0) if settled else None,
        "avg_edge_pct":sum(edges)/len(edges) if edges else None,
        "avg_clv_pct":sum(clvs)/len(clvs) if clvs else None,
        "median_clv_pct":median(clvs) if clvs else None,
        "beat_close_pct":sum(1 for x in clvs if x>0)/len(clvs)*100.0 if clvs else None,
        "clv_samples":len(clvs),
        "headline_clv_quality":"A+B (<=30m pre-kickoff)",
        "all_avg_clv_pct":sum(all_clvs)/len(all_clvs) if all_clvs else None,
        "all_median_clv_pct":median(all_clvs) if all_clvs else None,
        "all_clv_samples":len(all_clvs),
        "clv_quality_counts":quality_counts,
        "max_drawdown_units":max_dd,
        "net_max_drawdown_units":net_max_dd,
    }

def latest_execution_bets(db: Database, limit: int = 50) -> List[Dict[str,Any]]:
    return db.fetchall(
        """
        SELECT x.*,e.league,e.home_team,e.away_team,e.commence_time,
               (SELECT p.move_vs_entry_pct FROM execution_price_observations p
                WHERE p.execution_bet_id=x.id ORDER BY p.source_snapshot_at DESC LIMIT 1) AS latest_move_pct
        FROM execution_shadow_bets x JOIN events e ON e.event_id=x.event_id
        ORDER BY x.id DESC LIMIT ?
        """,
        (limit,),
    )


def execution_funnel(db: Database) -> Dict[str,Any]:
    theoretical=int((db.fetchone("SELECT COUNT(*) AS n FROM canonical_bets") or {}).get("n") or 0)
    executable=int((db.fetchone("SELECT COUNT(*) AS n FROM execution_shadow_bets") or {}).get("n") or 0)
    accepts=int((db.fetchone("SELECT COUNT(*) AS n FROM execution_evaluations WHERE decision='ACCEPT'") or {}).get("n") or 0)
    rejects=int((db.fetchone("SELECT COUNT(*) AS n FROM execution_evaluations WHERE decision='REJECT'") or {}).get("n") or 0)
    return {
        "theoretical_canonical":theoretical,"executable_shadow":executable,
        "execution_accept_rate_pct":(executable/theoretical*100.0) if theoretical else None,
        "execution_accept_audits":accepts,"execution_reject_audits":rejects,
    }
