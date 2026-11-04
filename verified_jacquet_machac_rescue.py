from __future__ import annotations

"""One-off, evidence-backed 2 Nov 2026 Jacquet/Machac settlement repair.

Never manufactures an event, changes a frozen selection or overwrites an
existing contradictory result. The exact tennis event identity and date must
match before a canonical result can be added. Safe to run on every restart.
"""

from datetime import date, datetime
import json
import re
import unicodedata

from db import utc_now_iso
from tennis_shadow import commission_adjusted_pnl

VERSION = "JACQUET_MACHAC_VERIFIED_RESCUE_V1_20261104"
MATCH_DATE = date(2026, 11, 2)
JACQUET = "kyrianjacquet"
MACHAC = "tomasmachac"
SOURCE_URLS = [
    "https://www.tennis.com/tournaments/rolex-paris-masters/matches/k-jacquet-vs-t-machac-2026-11-02",
    "https://www.flashscore.com/match/tennis/jacquet-kyrian-KGKot0sk/machac-tomas-S44E7UYr/",
]


def _key(value):
    ascii_text = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = "".join(ch for ch in ascii_text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", ascii_text.lower())


def _is_fixture(event):
    try:
        date_value = datetime.fromisoformat(
            str(event.get("commence_time") or "")[:10]
        ).date()
    except ValueError:
        return False
    if date_value not in {MATCH_DATE, date(2026, 11, 3)}:
        return False
    if str(event.get("tour") or "").lower() != "atp":
        return False
    return {_key(event.get("player_one")), _key(event.get("player_two"))} == {
        JACQUET, MACHAC
    }


def _settle_target_execution(db, event_id):
    """Respect recorded selection/odds, normalising accents only for this event."""
    updated = 0
    unknown = []
    rows = db.fetchall(
        "SELECT * FROM tennis_execution_bets WHERE event_id=? AND status='OPEN' ORDER BY id",
        (event_id,),
    )
    for row in rows:
        selected = _key(row.get("selection"))
        if selected not in {JACQUET, MACHAC}:
            unknown.append({"id": row["id"], "selection": row.get("selection")})
            continue
        won = selected == MACHAC
        result = "WIN" if won else "LOSS"
        gross = float(row["offered_odds"]) - 1.0 if won else -1.0
        rate, commission, net = commission_adjusted_pnl(
            gross, str(row["bookmaker_key"])
        )
        db.execute(
            """UPDATE tennis_execution_bets
               SET status='SETTLED',result=?,pnl_units=?,commission_rate_pct=?,
                   commission_units=?,net_pnl_units=?,settled_at=?
               WHERE id=? AND status='OPEN'""",
            (result, gross, rate, commission, net, utc_now_iso(), row["id"]),
        )
        updated += 1
    return {"settled": updated, "unrecognised_selections": unknown}


def run_once(db):
    out = {
        "version": VERSION, "fixture": "Kyrian Jacquet v Tomas Machac",
        "match_date": MATCH_DATE.isoformat(), "verified_winner": "Tomas Machac",
        "provider_calls": 0, "matched_ids": [], "inserted_results": 0,
        "already_verified_results": 0, "conflicts": [],
        "execution_settled": 0, "reconciliation": {},
    }
    rows = db.fetchall(
        """SELECT event_id,tour,commence_time,player_one,player_two
           FROM tennis_events
           WHERE commence_time>=? AND commence_time<?
           ORDER BY commence_time,event_id""",
        ("2026-11-01", "2026-11-04"),
    )
    matches = [dict(event) for event in rows if _is_fixture(event)]
    if len(matches) > 8:
        out["conflicts"].append("unexpected_match_count_gt_8")
        print("VERIFIED_TENNIS_RESCUE " + json.dumps(out, sort_keys=True), flush=True)
        return out

    for event in matches:
        event_id = str(event["event_id"])
        existing = db.fetchone(
            "SELECT winner FROM tennis_results WHERE event_id=?", (event_id,)
        )
        if existing and _key(existing.get("winner")) != MACHAC:
            out["conflicts"].append({
                "event_id": event_id, "stored_winner": existing.get("winner"),
            })
            continue

        # Reuse the name as recorded in tennis_events so the original execution
        # ledger, which compares selected names verbatim, stays compatible.
        winner = (
            event["player_one"] if _key(event["player_one"]) == MACHAC
            else event["player_two"]
        )
        score_one = 2 if _key(event["player_one"]) == MACHAC else 0
        score_two = 2 if _key(event["player_two"]) == MACHAC else 0
        if not existing:
            provenance = {
                "version": VERSION, "sources": SOURCE_URLS,
                "event_date": MATCH_DATE.isoformat(),
                "result": "Tomas Machac def. Kyrian Jacquet 7-6(7), 6-1",
                "match_completed_normally": True,
            }
            db.execute(
                """INSERT INTO tennis_results(
                   event_id,fetched_at,completed_at,winner,player_one_score,
                   player_two_score,source,settlement_quality,
                   settlement_provenance,raw_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(event_id) DO NOTHING""",
                (
                    event_id, utc_now_iso(), None, winner, score_one, score_two,
                    "cross_verified_historical_result", "INDEPENDENT_SOURCES_VERIFIED",
                    json.dumps(provenance, sort_keys=True),
                    json.dumps(provenance, sort_keys=True),
                ),
            )
            out["inserted_results"] += 1
        else:
            out["already_verified_results"] += 1
        out["matched_ids"].append(event_id)
        db.execute(
            "UPDATE tennis_events SET status='COMPLETED' WHERE event_id=?",
            (event_id,),
        )
        result = _settle_target_execution(db, event_id)
        out["execution_settled"] += result["settled"]
        if result["unrecognised_selections"]:
            out["conflicts"].append({
                "event_id": event_id,
                "unrecognised_selections": result["unrecognised_selections"],
            })

    # Only propagate an independently verified canonical result, never an
    # unrecognised or contradictory fixture.
    if out["matched_ids"]:
        import tennis_forward_readiness_patch as tennis_forward
        import hit_rate_multiples_lab as hit_rate
        import hr_portfolio_shadow as portfolio
        import hr_rolling_quality_challenger as quality

        reconcilers = [
            ("tennis_forward", tennis_forward.reconcile_stored_results),
            ("hit_rate_cards", hit_rate.settle_cards),
            ("portfolio_shadow", portfolio._settle),
            ("quality_challenger", quality._settle),
        ]
        for name, fn in reconcilers:
            try:
                out["reconciliation"][name] = fn(db)
            except Exception as exc:
                out["reconciliation"][name] = {
                    "error": f"{type(exc).__name__}: {exc}"
                }
        # The remaining shadows can also share the same underlying tennis
        # result. Use their normal idempotent settlement functions.
        optional = [
            ("manual_systems", "manual_systems_shadow", "settle_manual_system_shadows"),
            ("cohort_systems", "cohort_systems_shadow", "settle_cohort_system_shadows"),
            ("cross_sport_systems", "cross_sport_systems_shadow", "settle_cross_sport_systems"),
        ]
        for label, module_name, function_name in optional:
            try:
                from importlib import import_module
                fn = getattr(import_module(module_name), function_name)
                out["reconciliation"][label] = fn(db)
            except Exception as exc:
                out["reconciliation"][label] = {
                    "error": f"{type(exc).__name__}: {exc}"
                }

    try:
        db.record_collector_run(
            "VERIFIED_TENNIS_RESULT_RESCUE", not bool(out["conflicts"]),
            detail=json.dumps(out, sort_keys=True, default=str),
        )
    except Exception:
        pass
    print("VERIFIED_TENNIS_RESCUE " + json.dumps(out, sort_keys=True, default=str), flush=True)
    return out
