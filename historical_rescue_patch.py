from __future__ import annotations

"""Audited historical rescue for stale primary shadow selections.

This patch clears the known legacy result debt without fabricating outcomes:
- verified completed events are written once at event level and then settled by
  the existing engines;
- postponed/cancelled/pre-match walkover events are marked VOID and excluded
  from ROI/CLV samples;
- any future primary event still OPEN with no stored result after 72h is added
  to a visible Historical Rescue queue.

No model, selection, odds-band, staking or execution-authority logic changes.
"""

from datetime import datetime, timedelta, timezone
import json
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from db import utc_now_iso
from results import settle_event_signals
from tennis_shadow import settle_tennis_event
from multisport_shadow import settle_multisport_event
from multisport_lines_shadow import settle_lines_from_stored_results
import settlement_integrity_v2_patch as integrity

VERSION = "HISTORICAL_RESCUE_V1"
APP_VERSION = "0.19.56"
RESCUE_AFTER_HOURS = 72.0

# One-off verified legacy repairs. Evidence was checked independently on
# 2026-10-12. Event IDs are provider-stable keys already stored in the DB.
FOOTBALL_VOID = {
    "4ef67376b662c2859f73bde7f7210388": ("POSTPONED", "Levante v Athletic Bilbao postponed to 21 Oct 2026"),
    "6a63e1f6d87ea3609a8706e70f11c909": ("POSTPONED", "Bromley v Wycombe postponed"),
    "8aca943e9201ced10ad488d028c05ef3": ("POSTPONED", "Wigan v Mansfield postponed"),
    "26e3612cb3a4a4cd144af369bd303dcd": ("POSTPONED", "Sabadell v Andorra postponed from 3 Oct to 4 Oct"),
}

FOOTBALL_RESULTS = {
    "71df73801eb691fef039b6a34b076df3": (0, 0, "Sabadell 0-0 Andorra, played 4 Oct 2026"),
}

TENNIS_VOID = {
    "f2503c5b229ca3baa26c482a338a5517": "Marta Kostyuk v Taylor Townsend - pre-match walkover",
    "d970c335660e8c64598b7f88e378dc76": "Elise Mertens v Barbora Krejcikova - pre-match walkover",
    "dd870a99aafe250eeb6eb9e769dccbea": "Viktorija Golubic v Alina Korneeva - cancelled",
    "025d305a5487d45a414b2e270fa4754b": "Petra Marcinko v Magdalena Frech - cancelled",
    "2ef1631ed3c54fa2c2d6903629201493": "Mayar Sherif v Polina Kudermetova - cancelled",
    "94cdbcf900c531f58cb48d03b93e7b26": "Lorenzo Musetti v Arthur Fery - cancelled before play",
    "c83a68420401e6b8f8010e3050bed2ea": "Alex Molcan v Arthur Rinderknech - cancelled",
    "7fa3afd6824f6f399b190a8b95e17b57": "Alex de Minaur v Alex Molcan - pre-match walkover",
}

TENNIS_RESULTS = {
    "0bff2782ee07c23760cbe4d2877ea6b4": ("Jiri Lehecka", "Lehecka beat Bergs 6-3 6-7 6-3"),
    "2380d192906d145f72f594e7106b0471": ("Sinja Kraus", "Kraus advanced after Potapova retired after play began"),
    "ecfecd80927a50500f83832f5cb4bdcc": ("Qinwen Zheng", "Zheng beat Kalinskaya 6-4 3-6 6-0"),
    "fd98debe847b0a1e0eb6de258518fba3": ("Leylah Fernandez", "Fernandez beat Alexandrova 6-4 3-6 6-3"),
    "861dc414e5e4a52bbe292d7f258cdc32": ("Janice Tjen", "Tjen beat Ito 6-3 6-1"),
}

# event_id: home_score, away_score, note. Scores are the provider-facing final
# scores used only to determine H2H outcome (and totals for the MLB line event).
MULTISPORT_RESULTS = {
    "35bfc320944a291e2f7dbcd64d062d13": (406, 405, "India beat West Indies by 8 wickets, 30 Sep ODI"),
    "17c8929c3b1213c56e8e413ddb8c9e9e": (145, 322, "Australia beat South Africa by DLS, 30 Sep ODI"),
    "3fbdf5ef9e517e3250f7eda297024ef9": (351, 352, "West Indies beat India by 5 wickets, 3 Oct ODI"),
    "e3cb10ced3e8190f557f9ac8ed99d966": (172, 171, "India beat West Indies by 8 wickets, 6 Oct T20I"),
    "e33d4772daf4acf3265b763829c9cb12": (187, 182, "Pakistan beat Sri Lanka by 4 wickets, 9 Oct T20I"),
    "fdbaa7e6deca4fb654328b0ff837418e": (249, 252, "West Indies beat India by 6 wickets, 9 Oct T20I"),
    "1212aa43d192423c6c1cda27800c8342": (215, 133, "Pakistan beat Sri Lanka by 82 runs, 11 Oct T20I"),
    "35d3bfa7ed91441f9c3a617d9a61b31e": (223, 209, "India beat West Indies by 14 runs, 11 Oct T20I"),
    "7fafa79a573ac098f38c8c6c4293b93e": (4, 2, "Baltimore Orioles beat Toronto Blue Jays 4-2, 22 Sep MLB"),
}

FOOTBALL_TABLES = (
    "signals", "canonical_bets", "execution_shadow_bets",
    "football_predictive_bets", "football_predictive_market_bets",
    "football_predictive2_bets", "football_predictive2_market_bets",
    "football_predictive3_bets", "football_predictive3_market_bets",
    "football_predictive4_bets", "football_predictive4_market_bets",
)

PRIMARY_LANES = integrity.LANES


def _ensure_schema(db) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS settlement_rescue_audit (
            rescue_key TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            family TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT,
            evidence_json TEXT,
            first_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT
        )
        """
    )


def _columns(db, table: str) -> set[str]:
    with db.connection() as conn:
        return db._column_names(conn, table)


def _audit(db, event_id: str, family: str, action: str, status: str,
           note: str = "", evidence: Optional[Mapping[str, Any]] = None,
           resolved: bool = False) -> None:
    stamp = utc_now_iso()
    key = f"{family}|{event_id}"
    db.execute(
        """
        INSERT INTO settlement_rescue_audit(
            rescue_key,event_id,family,action,status,note,evidence_json,
            first_seen_at,updated_at,resolved_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(rescue_key) DO UPDATE SET
            action=excluded.action,status=excluded.status,note=excluded.note,
            evidence_json=excluded.evidence_json,updated_at=excluded.updated_at,
            resolved_at=excluded.resolved_at
        """,
        (key,event_id,family,action,status,note,
         json.dumps(dict(evidence or {}), sort_keys=True, separators=(",", ":")),
         stamp,stamp,stamp if resolved else None),
    )


def _void_rows(db, table: str, event_id: str) -> int:
    try:
        cols = _columns(db, table)
    except Exception:
        return 0
    if "status" not in cols or "event_id" not in cols:
        return 0
    assignments = ["status='VOID'"]
    if "result" in cols:
        assignments.append("result='VOID'")
    if "bet_result" in cols:
        assignments.append("bet_result='VOID'")
    # Void events must not count as settled ROI/CLV evidence.
    for col in (
        "pnl_units", "net_pnl_units", "commission_units",
        "closing_odds", "clv_pct", "clv_vs_consensus_pct",
        "clv_vs_best_reference_pct", "clv_vs_closing_consensus_pct",
        "clv_vs_closing_best_common_pct",
    ):
        if col in cols:
            assignments.append(f"{col}=NULL")
    return db.execute(
        f"UPDATE {table} SET {','.join(assignments)} WHERE event_id=? AND status='OPEN'",
        (event_id,),
    )


def _void_football(db, event_id: str, kind: str, note: str) -> int:
    changed = sum(_void_rows(db, table, event_id) for table in FOOTBALL_TABLES)
    db.execute("UPDATE events SET status=? WHERE event_id=?", (kind, event_id))
    _audit(db,event_id,"FOOTBALL","VOID","RESOLVED",note,
           {"verified_on":"2026-10-12","resolution":kind},True)
    return changed


def _void_tennis(db, event_id: str, note: str) -> int:
    changed = _void_rows(db,"tennis_execution_bets",event_id)
    db.execute("UPDATE tennis_events SET status='CANCELLED' WHERE event_id=?", (event_id,))
    _audit(db,event_id,"TENNIS","VOID","RESOLVED",note,
           {"verified_on":"2026-10-12","resolution":"CANCELLED_OR_WALKOVER"},True)
    return changed


def _store_football_result(db, event_id: str, hs: int, aws: int, note: str) -> bool:
    event = db.fetchone("SELECT * FROM events WHERE event_id=?", (event_id,))
    if not event:
        return False
    provenance = {"rescue":VERSION,"verified_on":"2026-10-12","note":note}
    db.execute(
        """
        INSERT INTO event_results(
            event_id,fetched_at,completed_at,home_score,away_score,source,raw_json,
            settlement_quality,settlement_provenance
        ) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO NOTHING
        """,
        (event_id,utc_now_iso(),event.get("commence_time"),hs,aws,
         "historical_rescue",json.dumps(provenance,separators=(",", ":")),
         "A",json.dumps(provenance,separators=(",", ":"))),
    )
    db.execute("UPDATE events SET status='COMPLETED' WHERE event_id=?", (event_id,))
    settle_event_signals(db,event_id,home_score=hs,away_score=aws)
    _audit(db,event_id,"FOOTBALL","RESULT_BACKFILL","RESOLVED",note,provenance,True)
    return True


def _store_tennis_result(db, event_id: str, winner: str, note: str) -> bool:
    event = db.fetchone("SELECT * FROM tennis_events WHERE event_id=?", (event_id,))
    if not event:
        return False
    provenance = {"rescue":VERSION,"verified_on":"2026-10-12","note":note}
    db.execute(
        """
        INSERT INTO tennis_results(
            event_id,fetched_at,completed_at,winner,player_one_score,player_two_score,
            source,settlement_quality,settlement_provenance,raw_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO NOTHING
        """,
        (event_id,utc_now_iso(),event.get("commence_time"),winner,None,None,
         "historical_rescue","HISTORICAL_VERIFIED",
         json.dumps(provenance,separators=(",", ":")),
         json.dumps(provenance,separators=(",", ":"))),
    )
    db.execute("UPDATE tennis_events SET status='COMPLETED' WHERE event_id=?", (event_id,))
    settle_tennis_event(db,event_id,winner)
    _audit(db,event_id,"TENNIS","RESULT_BACKFILL","RESOLVED",note,provenance,True)
    return True


def _store_multisport_result(db, event_id: str, hs: int, aws: int, note: str) -> bool:
    event = db.fetchone("SELECT * FROM multisport_events WHERE event_id=?", (event_id,))
    if not event:
        return False
    home = str(event.get("home_team") or "")
    away = str(event.get("away_team") or "")
    winner = None if hs == aws else (home if hs > aws else away)
    kind = "TIE" if hs == aws else "WINNER"
    provenance = {"rescue":VERSION,"verified_on":"2026-10-12","note":note}
    db.execute(
        """
        INSERT INTO multisport_results(
            event_id,fetched_at,completed_at,home_score,away_score,winner,result_kind,
            source,settlement_quality,settlement_provenance,raw_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO NOTHING
        """,
        (event_id,utc_now_iso(),event.get("commence_time"),hs,aws,winner,kind,
         "historical_rescue","HISTORICAL_VERIFIED",
         json.dumps(provenance,separators=(",", ":")),
         json.dumps(provenance,separators=(",", ":"))),
    )
    db.execute("UPDATE multisport_events SET status='COMPLETED' WHERE event_id=?", (event_id,))
    settle_multisport_event(db,event_id,home_score=hs,away_score=aws)
    settle_lines_from_stored_results(db)
    _audit(db,event_id,"MULTISPORT","RESULT_BACKFILL","RESOLVED",note,provenance,True)
    return True


def refresh_rescue_queue(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Surface future >72h missing-result debt instead of letting it hide OPEN."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=RESCUE_AFTER_HOURS)
    pending_events = set()
    for lane, table, event_table, result_table in PRIMARY_LANES:
        try:
            rows = db.fetchall(
                f"""
                SELECT DISTINCT b.event_id
                FROM {table} b JOIN {event_table} e ON e.event_id=b.event_id
                LEFT JOIN {result_table} r ON r.event_id=b.event_id
                WHERE b.status='OPEN' AND r.event_id IS NULL AND e.commence_time<=?
                """,
                (cutoff.isoformat(),),
            )
        except Exception:
            continue
        family = "TENNIS" if lane == "TENNIS" else "MULTISPORT" if lane.startswith("MULTISPORT") else "FOOTBALL"
        for row in rows:
            event_id = str(row.get("event_id") or "")
            if not event_id:
                continue
            pending_events.add((family,event_id))
            existing = db.fetchone("SELECT status FROM settlement_rescue_audit WHERE rescue_key=?",(f"{family}|{event_id}",))
            if not existing or existing.get("status") != "RESOLVED":
                _audit(db,event_id,family,"HISTORICAL_RESCUE_PENDING","PENDING",
                       f"Missing stored result >{int(RESCUE_AFTER_HOURS)}h after scheduled start",
                       {"queued_at":now.isoformat(),"lane":lane},False)
    pending = db.fetchall(
        """SELECT * FROM settlement_rescue_audit WHERE status='PENDING'
           ORDER BY first_seen_at,event_id"""
    )
    return {"pending":len(pending),"events":pending}


def run_once(db, web_module=None) -> Dict[str, Any]:
    _ensure_schema(db)
    stats = {"voided_rows":0,"football_results":0,"tennis_results":0,"multisport_results":0}

    for event_id,(kind,note) in FOOTBALL_VOID.items():
        stats["voided_rows"] += _void_football(db,event_id,kind,note)
    for event_id,(hs,aws,note) in FOOTBALL_RESULTS.items():
        stats["football_results"] += int(_store_football_result(db,event_id,hs,aws,note))

    for event_id,note in TENNIS_VOID.items():
        stats["voided_rows"] += _void_tennis(db,event_id,note)
    for event_id,(winner,note) in TENNIS_RESULTS.items():
        stats["tennis_results"] += int(_store_tennis_result(db,event_id,winner,note))

    for event_id,(hs,aws,note) in MULTISPORT_RESULTS.items():
        stats["multisport_results"] += int(_store_multisport_result(db,event_id,hs,aws,note))

    # Re-apply stored results to every primary ledger, including PRED1-4.
    if web_module is not None:
        try:
            stats["reapplied"] = integrity._reapply_stored_results(web_module)
        except Exception as exc:
            stats["reapply_error"] = f"{type(exc).__name__}: {exc}"

    stats["queue"] = refresh_rescue_queue(db)
    try:
        stats["health"] = integrity.settlement_health(db)
    except Exception as exc:
        stats["health_error"] = f"{type(exc).__name__}: {exc}"
    try:
        db.record_collector_run("HISTORICAL_RESCUE",True,detail=json.dumps(stats,sort_keys=True,default=str)[:8000])
    except Exception:
        pass
    return stats


def install(app, db, web_module=None) -> Dict[str, Any]:
    result = run_once(db,web_module)

    if not any(getattr(r,"path",None)=="/api/settlement-rescue" for r in app.router.routes):
        @app.get("/api/settlement-rescue")
        def settlement_rescue_api():
            queue = refresh_rescue_queue(db)
            return {"version":VERSION,"rescue_after_hours":RESCUE_AFTER_HOURS,
                    "queue":queue,
                    "audit":db.fetchall("SELECT * FROM settlement_rescue_audit ORDER BY updated_at DESC LIMIT 100")}

    if web_module is not None:
        try:
            web_module.VERSION = APP_VERSION
        except Exception:
            pass
    return {"installed":True,"version":VERSION,"dashboard_version":APP_VERSION,"result":result}
