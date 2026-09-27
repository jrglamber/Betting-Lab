from __future__ import annotations

from collections import defaultdict
import json
from typing import Any, Dict, Iterable, List, Mapping

from db import Database
from fair_value import consensus_probabilities, edge_pct, fair_odds, min_odds_for_probability
from cross_market import fit_score_model
from slow_book import detect_slow_book
from research import record_evaluation


def _group_key(row: Mapping[str, Any]):
    return (row["market_key"], row.get("outcome_description") or "", row.get("point"))


def latest_quote_rows(db: Database, event_id: str) -> List[Dict[str, Any]]:
    latest = db.fetchone(
        "SELECT MAX(captured_at) AS captured_at FROM odds_snapshots WHERE event_id=?",
        (event_id,),
    )
    if not latest or not latest.get("captured_at"):
        return []
    return db.fetchall(
        "SELECT * FROM odds_snapshots WHERE event_id=? AND captured_at=?",
        (event_id, latest["captured_at"]),
    )


def _book_prices(rows: Iterable[Mapping[str, Any]]):
    grouped = defaultdict(lambda: defaultdict(dict))
    for row in rows:
        grouped[_group_key(row)][row["bookmaker_key"]][row["outcome_name"]] = float(row["price"])
    return grouped


def _title_map(rows, group_key):
    return {r["bookmaker_key"]: r["bookmaker_title"] for r in rows if _group_key(r) == group_key}


def write_consensus_snapshots_only(db: Database, event_id: str, *, min_books: int = 3) -> int:
    """Persist fair-probability consensus from the latest wave without creating signals.

    v0.13 uses this for dense near-kickoff PRED market benchmarking. It keeps
    the extra close-capture lane measurement-only so more frequent polling does
    not create a burst of new strategy signals or execution candidates.
    """
    rows = latest_quote_rows(db, event_id)
    if not rows:
        return 0
    groups = _book_prices(rows)
    capture_time = rows[0]["captured_at"]
    written = 0
    for group_key, bookmaker_prices in groups.items():
        market_key, desc, point = group_key
        consensus = consensus_probabilities(bookmaker_prices, min_books=min_books)
        if not consensus:
            continue
        for selection, prob in consensus.items():
            clauses = ["event_id=?", "captured_at=?", "market_key=?", "selection=?"]
            params = [event_id, capture_time, market_key, selection]
            if desc:
                clauses.append("outcome_description=?"); params.append(desc)
            else:
                clauses.append("outcome_description IS NULL")
            if point is None:
                clauses.append("point IS NULL")
            else:
                clauses.append("point=?"); params.append(point)
            exists = db.fetchone(
                f"SELECT id FROM consensus_snapshots WHERE {' AND '.join(clauses)} LIMIT 1",
                params,
            )
            if exists:
                continue
            db.execute(
                """INSERT INTO consensus_snapshots(
                    event_id,captured_at,market_key,selection,outcome_description,
                    point,fair_probability,fair_odds,num_books,model
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (event_id,capture_time,market_key,selection,desc or None,point,
                 prob,fair_odds(prob),len(bookmaker_prices),
                 "bookmaker_consensus_close_capture"),
            )
            written += 1
    return written


def write_consensus_and_value_signals(db: Database, event_id: str, *, min_books: int = 3, min_edge_pct: float = 3.0) -> int:
    rows = latest_quote_rows(db, event_id)
    if not rows:
        return 0
    groups = _book_prices(rows)
    capture_time = rows[0]["captured_at"]
    signals_written = 0

    for group_key, bookmaker_prices in groups.items():
        market_key, desc, point = group_key
        title_by_key = _title_map(rows, group_key)
        consensus = consensus_probabilities(bookmaker_prices, min_books=min_books)
        if consensus:
            for selection, prob in consensus.items():
                clauses = [
                    "event_id=?", "captured_at=?", "market_key=?", "selection=?"
                ]
                params = [event_id, capture_time, market_key, selection]
                if desc:
                    clauses.append("outcome_description=?")
                    params.append(desc)
                else:
                    clauses.append("outcome_description IS NULL")
                if point is None:
                    clauses.append("point IS NULL")
                else:
                    clauses.append("point=?")
                    params.append(point)
                exists = db.fetchone(
                    f"SELECT id FROM consensus_snapshots WHERE {' AND '.join(clauses)} LIMIT 1",
                    params,
                )
                if not exists:
                    db.execute(
                        """
                        INSERT INTO consensus_snapshots(
                            event_id,captured_at,market_key,selection,outcome_description,
                            point,fair_probability,fair_odds,num_books,model
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (event_id,capture_time,market_key,selection,desc or None,point,
                         prob,fair_odds(prob),len(bookmaker_prices),"bookmaker_consensus"),
                    )

        for book_key, prices in bookmaker_prices.items():
            fair = consensus_probabilities(bookmaker_prices, exclude_bookmaker=book_key, min_books=min_books)
            for selection, offered in prices.items():
                if not fair or selection not in fair:
                    record_evaluation(
                        db,evaluated_at=capture_time,event_id=event_id,strategy="CONSENSUS_VALUE",
                        market_key=market_key,selection=selection,outcome_description=desc or None,
                        point=point,bookmaker_key=book_key,bookmaker_title=title_by_key.get(book_key,book_key),
                        offered_odds=offered,peer_books=max(0,len(bookmaker_prices)-1),
                        decision="REJECT",reason="INSUFFICIENT_PEER_BOOKS",
                    )
                    continue
                prob = fair[selection]
                candidate_fair_odds = fair_odds(prob)
                candidate_edge = edge_pct(prob, offered)
                if candidate_edge < min_edge_pct:
                    record_evaluation(
                        db,evaluated_at=capture_time,event_id=event_id,strategy="CONSENSUS_VALUE",
                        market_key=market_key,selection=selection,outcome_description=desc or None,
                        point=point,bookmaker_key=book_key,bookmaker_title=title_by_key.get(book_key,book_key),
                        offered_odds=offered,fair_odds=candidate_fair_odds,fair_probability=prob,
                        edge_pct=candidate_edge,peer_books=max(0,len(bookmaker_prices)-1),
                        decision="REJECT",reason="EDGE_BELOW_THRESHOLD",
                        metadata={"threshold_pct": min_edge_pct},
                    )
                    continue
                already = db.fetchone(
                    """
                    SELECT id FROM signals WHERE event_id=? AND strategy='CONSENSUS_VALUE'
                      AND market_key=? AND selection=? AND bookmaker_key=? AND created_at=?
                    """,
                    (event_id,market_key,selection,book_key,capture_time),
                )
                if not already:
                    db.execute(
                        """
                        INSERT INTO signals(
                            created_at,event_id,strategy,market_key,selection,outcome_description,
                            point,bookmaker_key,bookmaker_title,offered_odds,fair_odds,
                            fair_probability,edge_pct,min_odds,status,metadata_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (capture_time,event_id,"CONSENSUS_VALUE",market_key,selection,desc or None,
                         point,book_key,title_by_key.get(book_key,book_key),offered,candidate_fair_odds,
                         prob,candidate_edge,min_odds_for_probability(prob,min_edge_pct),"OPEN",
                         json.dumps({"consensus_books_ex_target": len(bookmaker_prices)-1})),
                    )
                    signals_written += 1
                record_evaluation(
                    db,evaluated_at=capture_time,event_id=event_id,strategy="CONSENSUS_VALUE",
                    market_key=market_key,selection=selection,outcome_description=desc or None,
                    point=point,bookmaker_key=book_key,bookmaker_title=title_by_key.get(book_key,book_key),
                    offered_odds=offered,fair_odds=candidate_fair_odds,fair_probability=prob,
                    edge_pct=candidate_edge,peer_books=max(0,len(bookmaker_prices)-1),
                    decision="SIGNAL",reason="EDGE_PASSED",
                    metadata={"threshold_pct": min_edge_pct},
                )
    return signals_written


def write_slow_book_signals(db: Database, event_id: str, *, min_books: int = 3, min_gap_pct: float = 4.0, min_edge_pct: float = 3.0) -> int:
    rows = latest_quote_rows(db, event_id)
    if not rows:
        return 0
    groups = _book_prices(rows)
    capture_time = rows[0]["captured_at"]
    written = 0
    for group_key, bookmaker_prices in groups.items():
        market_key, desc, point = group_key
        title_by_key = _title_map(rows, group_key)
        for book_key, prices in bookmaker_prices.items():
            fair = consensus_probabilities(bookmaker_prices, exclude_bookmaker=book_key, min_books=min_books)
            for selection, offered in prices.items():
                peer_odds = [p[selection] for other,p in bookmaker_prices.items() if other != book_key and selection in p]
                slow = detect_slow_book(offered, peer_odds, minimum_gap_pct=min_gap_pct)
                if not slow:
                    record_evaluation(
                        db,evaluated_at=capture_time,event_id=event_id,strategy="SLOW_BOOK",
                        market_key=market_key,selection=selection,outcome_description=desc or None,
                        point=point,bookmaker_key=book_key,bookmaker_title=title_by_key.get(book_key,book_key),
                        offered_odds=offered,peer_books=len(peer_odds),decision="REJECT",
                        reason="PRICE_GAP_BELOW_THRESHOLD",metadata={"threshold_pct": min_gap_pct},
                    )
                    continue
                if not fair or selection not in fair:
                    record_evaluation(
                        db,evaluated_at=capture_time,event_id=event_id,strategy="SLOW_BOOK",
                        market_key=market_key,selection=selection,outcome_description=desc or None,
                        point=point,bookmaker_key=book_key,bookmaker_title=title_by_key.get(book_key,book_key),
                        offered_odds=offered,peer_books=len(peer_odds),decision="REJECT",
                        reason="INSUFFICIENT_PEER_BOOKS",metadata={"gap_pct": slow.relative_price_gap_pct},
                    )
                    continue
                prob = fair[selection]
                candidate_edge = edge_pct(prob, offered)
                candidate_fair = fair_odds(prob)
                if candidate_edge < min_edge_pct:
                    record_evaluation(
                        db,evaluated_at=capture_time,event_id=event_id,strategy="SLOW_BOOK",
                        market_key=market_key,selection=selection,outcome_description=desc or None,
                        point=point,bookmaker_key=book_key,bookmaker_title=title_by_key.get(book_key,book_key),
                        offered_odds=offered,fair_odds=candidate_fair,fair_probability=prob,
                        edge_pct=candidate_edge,peer_books=len(peer_odds),decision="REJECT",
                        reason="FAIR_VALUE_CHECK_FAILED",
                        metadata={"gap_pct": slow.relative_price_gap_pct,"market_median_odds": slow.market_median_odds},
                    )
                    continue
                already = db.fetchone(
                    """
                    SELECT id FROM signals WHERE event_id=? AND strategy='SLOW_BOOK'
                      AND market_key=? AND selection=? AND bookmaker_key=? AND created_at=?
                    """,
                    (event_id,market_key,selection,book_key,capture_time),
                )
                if not already:
                    db.execute(
                        """
                        INSERT INTO signals(
                            created_at,event_id,strategy,market_key,selection,outcome_description,
                            point,bookmaker_key,bookmaker_title,offered_odds,fair_odds,
                            fair_probability,edge_pct,min_odds,status,metadata_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (capture_time,event_id,"SLOW_BOOK",market_key,selection,desc or None,point,
                         book_key,title_by_key.get(book_key,book_key),offered,candidate_fair,prob,
                         candidate_edge,min_odds_for_probability(prob,min_edge_pct),"OPEN",
                         json.dumps({"gap_pct": slow.relative_price_gap_pct,"market_median_odds": slow.market_median_odds})),
                    )
                    written += 1
                record_evaluation(
                    db,evaluated_at=capture_time,event_id=event_id,strategy="SLOW_BOOK",
                    market_key=market_key,selection=selection,outcome_description=desc or None,
                    point=point,bookmaker_key=book_key,bookmaker_title=title_by_key.get(book_key,book_key),
                    offered_odds=offered,fair_odds=candidate_fair,fair_probability=prob,
                    edge_pct=candidate_edge,peer_books=len(peer_odds),decision="SIGNAL",reason="STALE_PRICE_PASSED",
                    metadata={"gap_pct": slow.relative_price_gap_pct,"market_median_odds": slow.market_median_odds},
                )
    return written


def _latest_consensus_map(db: Database, event_id: str):
    latest = db.fetchone("SELECT MAX(captured_at) AS captured_at FROM consensus_snapshots WHERE event_id=?", (event_id,))
    if not latest or not latest.get("captured_at"):
        return {}, None
    rows = db.fetchall("SELECT * FROM consensus_snapshots WHERE event_id=? AND captured_at=?", (event_id,latest["captured_at"]))
    return {(r["market_key"],r["selection"],r.get("point")):float(r["fair_probability"]) for r in rows}, latest["captured_at"]


def write_cross_market_signals(db: Database, event_id: str, *, min_edge_pct: float = 4.0) -> int:
    event = db.fetchone("SELECT * FROM events WHERE event_id=?", (event_id,))
    if not event:
        return 0
    consensus,captured_at = _latest_consensus_map(db,event_id)
    if not consensus or not captured_at:
        return 0
    home,away=event["home_team"],event["away_team"]
    p_home=consensus.get(("h2h",home,None)); p_draw=consensus.get(("h2h","Draw",None)); p_away=consensus.get(("h2h",away,None)); p_over=consensus.get(("totals","Over",2.5))
    if not all(x is not None for x in (p_home,p_draw,p_away,p_over)):
        record_evaluation(
            db,evaluated_at=captured_at,event_id=event_id,strategy="CROSS_MARKET_RV",
            market_key="cross_market",decision="REJECT",reason="MISSING_MODEL_INPUTS",
            metadata={"has_home":p_home is not None,"has_draw":p_draw is not None,"has_away":p_away is not None,"has_over25":p_over is not None},
        )
        return 0
    model=fit_score_model(p_home,p_draw,p_away,p_over)
    model_probs={("btts","Yes"):model.btts_yes,("btts","No"):1-model.btts_yes,("draw_no_bet",home):model.home_dnb,("draw_no_bet",away):model.away_dnb}
    rows=latest_quote_rows(db,event_id); written=0
    for row in rows:
        key=(row["market_key"],row["outcome_name"]); prob=model_probs.get(key)
        if prob is None: continue
        offered=float(row["price"]); candidate_edge=edge_pct(prob,offered); candidate_fair=fair_odds(prob)
        metadata={"lambda_home":model.lambda_home,"lambda_away":model.lambda_away,"fit_loss":model.loss,"source":"1X2+O/U2.5"}
        if candidate_edge < min_edge_pct:
            record_evaluation(
                db,evaluated_at=row["captured_at"],event_id=event_id,strategy="CROSS_MARKET_RV",
                market_key=row["market_key"],selection=row["outcome_name"],outcome_description=row.get("outcome_description"),
                point=row.get("point"),bookmaker_key=row["bookmaker_key"],bookmaker_title=row["bookmaker_title"],
                offered_odds=offered,fair_odds=candidate_fair,fair_probability=prob,edge_pct=candidate_edge,
                decision="REJECT",reason="EDGE_BELOW_THRESHOLD",metadata={**metadata,"threshold_pct":min_edge_pct},
            )
            continue
        exists=db.fetchone(
            """SELECT id FROM signals WHERE event_id=? AND strategy='CROSS_MARKET_RV' AND market_key=? AND selection=? AND bookmaker_key=? AND created_at=?""",
            (event_id,row["market_key"],row["outcome_name"],row["bookmaker_key"],row["captured_at"]),
        )
        if not exists:
            db.execute(
                """INSERT INTO signals(created_at,event_id,strategy,market_key,selection,outcome_description,point,bookmaker_key,bookmaker_title,offered_odds,fair_odds,fair_probability,edge_pct,min_odds,status,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["captured_at"],event_id,"CROSS_MARKET_RV",row["market_key"],row["outcome_name"],row.get("outcome_description"),row.get("point"),row["bookmaker_key"],row["bookmaker_title"],offered,candidate_fair,prob,candidate_edge,min_odds_for_probability(prob,min_edge_pct),"OPEN",json.dumps(metadata)),
            ); written+=1
        record_evaluation(
            db,evaluated_at=row["captured_at"],event_id=event_id,strategy="CROSS_MARKET_RV",
            market_key=row["market_key"],selection=row["outcome_name"],outcome_description=row.get("outcome_description"),point=row.get("point"),
            bookmaker_key=row["bookmaker_key"],bookmaker_title=row["bookmaker_title"],offered_odds=offered,fair_odds=candidate_fair,
            fair_probability=prob,edge_pct=candidate_edge,decision="SIGNAL",reason="MODEL_EDGE_PASSED",metadata=metadata,
        )
    return written
