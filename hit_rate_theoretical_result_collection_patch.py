from __future__ import annotations

"""Close the result-collection gap for theoretical Hit Rate Lab cards.

The pre-price HR queue can form placeable cards from theoretical tennis and
multisport evaluation rows without creating an upstream execution bet. The
legacy result collectors only poll events referenced by open execution bets, so
those HR-only events could remain PENDING forever even after the event had
finished.

This patch extends the existing tennis/multisport result cycle with a bounded,
exact-event-id pass for unresolved HR-only theoretical legs. It uses the same
provider client, quota guard, canonical result tables and settlement functions
as the existing collectors. No selection, pricing, card construction, staking,
alerting or betting authority is changed.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Dict, Mapping, Optional, Sequence

import hit_rate_multiples_lab as hr
import tennis_shadow
import multisport_shadow
from db import utc_now_iso
from odds_api import provider_actual_cost
from security import sanitize_sensitive_text

VERSION = "HR_THEORETICAL_RESULT_COLLECTION_V1"
POLL_INTERVAL_SECONDS = 15 * 60
MAX_EVENTS_PER_SPORT_CALL = 50

_INSTALLED = False
_ORIGINAL_TENNIS_COLLECT_RESULTS = None
_ORIGINAL_MULTISPORT_COLLECT_RESULTS = None


def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _recently_polled(db, run_type: str, sport_key: str, now: datetime) -> bool:
    try:
        row = db.fetchone(
            """SELECT started_at FROM collector_runs
               WHERE run_type=? AND sport_key=?
               ORDER BY id DESC LIMIT 1""",
            (run_type, sport_key),
        ) or {}
    except Exception:
        return False
    last = _parse(row.get("started_at"))
    return bool(last and (now - last).total_seconds() < POLL_INTERVAL_SECONDS)


def _pending_hr_tennis(db, now: datetime) -> list[dict[str, Any]]:
    cutoff = now - timedelta(minutes=30)
    try:
        rows = db.fetchall(
            """SELECT DISTINCT e.*
               FROM hit_rate_legs l
               JOIN hit_rate_cards c ON c.id=l.card_id
               JOIN tennis_events e ON e.event_id=l.event_id
               LEFT JOIN tennis_results r ON r.event_id=e.event_id
               WHERE c.status!='SETTLED'
                 AND l.result IS NULL
                 AND l.source_table='THEORETICAL_TENNIS_H2H'
                 AND r.event_id IS NULL
                 AND e.commence_time<=?
               ORDER BY e.commence_time""",
            (cutoff.isoformat(),),
        )
    except Exception:
        rows = []
    return [dict(x) for x in rows]


def _pending_hr_multisport(db, now: datetime) -> list[dict[str, Any]]:
    try:
        rows = db.fetchall(
            """SELECT DISTINCT e.*
               FROM hit_rate_legs l
               JOIN hit_rate_cards c ON c.id=l.card_id
               JOIN multisport_events e ON e.event_id=l.event_id
               LEFT JOIN multisport_results r ON r.event_id=e.event_id
               WHERE c.status!='SETTLED'
                 AND l.result IS NULL
                 AND l.source_table='THEORETICAL_MULTISPORT_H2H'
                 AND r.event_id IS NULL
               ORDER BY e.commence_time"""
        )
    except Exception:
        rows = []

    due = []
    for raw in rows:
        e = dict(raw)
        start = _parse(e.get("commence_time"))
        if start is None:
            continue
        delay = int(multisport_shadow.RESULT_DELAY_MINUTES.get(str(e.get("sport_family") or ""), 240))
        if start + timedelta(minutes=delay) <= now:
            due.append(e)
    return due


def _chunks(rows: Sequence[Mapping[str, Any]], size: int = MAX_EVENTS_PER_SPORT_CALL):
    for i in range(0, len(rows), size):
        yield [dict(x) for x in rows[i:i + size]]


def _collect_hr_tennis(engine, now: datetime) -> Dict[str, Any]:
    due = _pending_hr_tennis(engine.db, now)
    if not due:
        return {"checked": 0, "completed_events": 0, "hr_cards_settled": 0, "provider_credits": 0, "reason": "no_due_hr_theoretical_tennis"}

    grouped: Dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in due:
        grouped[str(event.get("sport_key") or "")].append(event)

    checked = completed = provider_credits = 0
    touched_before = int((hr.settle_cards(engine.db) or {}).get("settled") or 0)
    for sport_key, events in grouped.items():
        if not sport_key or _recently_polled(engine.db, "HR_TENNIS_RESULTS", sport_key, now):
            continue
        for batch in _chunks(events):
            allowed, reason = engine.quota.decide(2)
            if not allowed:
                engine.db.record_collector_run(
                    "HR_TENNIS_RESULTS", False, sport_key=sport_key,
                    requested_markets="scores", estimated_cost=2, actual_cost=0,
                    detail=f"quota_block:{reason}; hr_only_events={len(batch)}",
                )
                break
            actual = 0
            try:
                result = engine.api.scores(
                    sport_key,
                    event_ids=[str(e.get("event_id") or "") for e in batch],
                    days_from=3,
                )
                actual = provider_actual_cost(result.last_cost, 2)
                provider_credits += actual
                engine.quota.update(remaining=result.remaining, used=result.used, last_cost=result.last_cost)
                payload = result.data if isinstance(result.data, list) else []
                wanted = {str(e.get("event_id") or ""): e for e in batch}
                found = 0
                for item in payload:
                    eid = str(item.get("id") or "")
                    if eid not in wanted or not item.get("completed"):
                        continue
                    score_map = {}
                    for score in item.get("scores") or []:
                        try:
                            score_map[str(score["name"])] = int(float(score["score"]))
                        except Exception:
                            continue
                    event = wanted[eid]
                    p1 = str(event.get("player_one") or "")
                    p2 = str(event.get("player_two") or "")
                    if p1 not in score_map or p2 not in score_map:
                        continue
                    s1 = int(score_map[p1]); s2 = int(score_map[p2])
                    if s1 == s2:
                        continue
                    winner = p1 if s1 > s2 else p2
                    engine.db.execute(
                        """INSERT INTO tennis_results(
                               event_id,fetched_at,completed_at,winner,player_one_score,
                               player_two_score,source,settlement_quality,
                               settlement_provenance,raw_json
                           ) VALUES(?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(event_id) DO UPDATE SET
                               fetched_at=excluded.fetched_at,
                               completed_at=excluded.completed_at,
                               winner=excluded.winner,
                               player_one_score=excluded.player_one_score,
                               player_two_score=excluded.player_two_score,
                               source=excluded.source,
                               settlement_quality=excluded.settlement_quality,
                               settlement_provenance=excluded.settlement_provenance,
                               raw_json=excluded.raw_json""",
                        (
                            eid, utc_now_iso(), item.get("commence_time"), winner, s1, s2,
                            "the_odds_api", "PROVIDER_COMPLETED",
                            "HR theoretical exact-event result collection; provider completed match winner",
                            json.dumps(item, separators=(",", ":")),
                        ),
                    )
                    engine.db.execute("UPDATE tennis_events SET status='COMPLETED' WHERE event_id=?", (eid,))
                    tennis_shadow.settle_tennis_event(engine.db, eid, winner)
                    found += 1
                checked += len(batch)
                completed += found
                engine.db.record_collector_run(
                    "HR_TENNIS_RESULTS", True, sport_key=sport_key,
                    requested_markets="scores", estimated_cost=2, actual_cost=actual,
                    detail=f"hr_only_events={len(batch)}; completed_events={found}",
                )
            except Exception as exc:
                engine.db.record_collector_run(
                    "HR_TENNIS_RESULTS", False, sport_key=sport_key,
                    requested_markets="scores", estimated_cost=2, actual_cost=actual,
                    detail=f"hr_only_events={len(batch)}; error={sanitize_sensitive_text(exc)}",
                )

    repair = hr.settle_cards(engine.db) or {}
    return {
        "checked": checked,
        "completed_events": completed,
        "hr_cards_settled": int(repair.get("settled") or 0) + touched_before,
        "hr_legs_touched": int(repair.get("touched") or 0),
        "provider_credits": provider_credits,
        "reason": "ok",
    }


def _collect_hr_multisport(engine, now: datetime) -> Dict[str, Any]:
    due = _pending_hr_multisport(engine.db, now)
    if not due:
        return {"checked": 0, "completed_events": 0, "hr_cards_settled": 0, "provider_credits": 0, "reason": "no_due_hr_theoretical_multisport"}

    grouped: Dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in due:
        grouped[str(event.get("sport_key") or "")].append(event)

    checked = completed = provider_credits = 0
    for sport_key, events in grouped.items():
        if not sport_key or _recently_polled(engine.db, "HR_MULTISPORT_RESULTS", sport_key, now):
            continue
        for batch in _chunks(events):
            allowed, reason = engine.quota.decide(2)
            if not allowed:
                engine.db.record_collector_run(
                    "HR_MULTISPORT_RESULTS", False, sport_key=sport_key,
                    requested_markets="scores", estimated_cost=2, actual_cost=0,
                    detail=f"quota_block:{reason}; hr_only_events={len(batch)}",
                )
                break
            actual = 0
            try:
                result = engine.api.scores(
                    sport_key,
                    event_ids=[str(e.get("event_id") or "") for e in batch],
                    days_from=3,
                )
                actual = provider_actual_cost(result.last_cost, 2)
                provider_credits += actual
                engine.quota.update(remaining=result.remaining, used=result.used, last_cost=result.last_cost)
                payload = result.data if isinstance(result.data, list) else []
                wanted = {str(e.get("event_id") or ""): e for e in batch}
                found = 0
                for item in payload:
                    eid = str(item.get("id") or "")
                    if eid not in wanted or not item.get("completed"):
                        continue
                    score_map = {}
                    for score in item.get("scores") or []:
                        try:
                            score_map[str(score["name"])] = int(float(score["score"]))
                        except Exception:
                            continue
                    event = wanted[eid]
                    home = str(event.get("home_team") or "")
                    away = str(event.get("away_team") or "")
                    if home not in score_map or away not in score_map:
                        continue
                    hs = int(score_map[home]); aw = int(score_map[away])
                    tie = hs == aw
                    winner = None if tie else (home if hs > aw else away)
                    result_kind = "TIE" if tie else "WINNER"
                    provenance = (
                        "HR theoretical exact-event collection; provider completed tied final score"
                        if tie else
                        "HR theoretical exact-event collection; provider completed final score"
                    )
                    engine.db.execute(
                        """INSERT INTO multisport_results(
                               event_id,fetched_at,completed_at,home_score,away_score,winner,
                               result_kind,source,settlement_quality,settlement_provenance,raw_json
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(event_id) DO UPDATE SET
                               fetched_at=excluded.fetched_at,
                               completed_at=excluded.completed_at,
                               home_score=excluded.home_score,
                               away_score=excluded.away_score,
                               winner=excluded.winner,
                               result_kind=excluded.result_kind,
                               source=excluded.source,
                               settlement_quality=excluded.settlement_quality,
                               settlement_provenance=excluded.settlement_provenance,
                               raw_json=excluded.raw_json""",
                        (
                            eid, utc_now_iso(), item.get("commence_time"), hs, aw, winner,
                            result_kind, "the_odds_api", "PROVIDER_COMPLETED", provenance,
                            json.dumps(item, separators=(",", ":")),
                        ),
                    )
                    engine.db.execute("UPDATE multisport_events SET status='COMPLETED' WHERE event_id=?", (eid,))
                    multisport_shadow.settle_multisport_event(engine.db, eid, home_score=hs, away_score=aw)
                    found += 1
                checked += len(batch)
                completed += found
                engine.db.record_collector_run(
                    "HR_MULTISPORT_RESULTS", True, sport_key=sport_key,
                    requested_markets="scores", estimated_cost=2, actual_cost=actual,
                    detail=f"hr_only_events={len(batch)}; completed_events={found}",
                )
            except Exception as exc:
                engine.db.record_collector_run(
                    "HR_MULTISPORT_RESULTS", False, sport_key=sport_key,
                    requested_markets="scores", estimated_cost=2, actual_cost=actual,
                    detail=f"hr_only_events={len(batch)}; error={sanitize_sensitive_text(exc)}",
                )

    repair = hr.settle_cards(engine.db) or {}
    return {
        "checked": checked,
        "completed_events": completed,
        "hr_cards_settled": int(repair.get("settled") or 0),
        "hr_legs_touched": int(repair.get("touched") or 0),
        "provider_credits": provider_credits,
        "reason": "ok",
    }


def _tennis_collect_results(self, now: datetime) -> Dict[str, Any]:
    base = _ORIGINAL_TENNIS_COLLECT_RESULTS(self, now)
    extra = _collect_hr_tennis(self, now)
    if extra.get("checked") or extra.get("completed_events") or extra.get("hr_cards_settled"):
        print(f"HR_TENNIS_RESULT_COLLECTION {extra}", flush=True)
    return {"base": base, "hr_theoretical": extra}


def _multisport_collect_results(self, now: datetime) -> Dict[str, Any]:
    base = _ORIGINAL_MULTISPORT_COLLECT_RESULTS(self, now)
    extra = _collect_hr_multisport(self, now)
    if extra.get("checked") or extra.get("completed_events") or extra.get("hr_cards_settled"):
        print(f"HR_MULTISPORT_RESULT_COLLECTION {extra}", flush=True)
    return {"base": base, "hr_theoretical": extra}


def install() -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_TENNIS_COLLECT_RESULTS, _ORIGINAL_MULTISPORT_COLLECT_RESULTS
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    _ORIGINAL_TENNIS_COLLECT_RESULTS = tennis_shadow.TennisShadowEngine.collect_results
    _ORIGINAL_MULTISPORT_COLLECT_RESULTS = multisport_shadow.MultisportShadowEngine.collect_results
    tennis_shadow.TennisShadowEngine.collect_results = _tennis_collect_results
    multisport_shadow.MultisportShadowEngine.collect_results = _multisport_collect_results
    _INSTALLED = True
    state = {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "sources": ["THEORETICAL_TENNIS_H2H", "THEORETICAL_MULTISPORT_H2H"],
        "exact_event_id_only": True,
        "poll_interval_minutes": POLL_INTERVAL_SECONDS / 60,
        "max_events_per_sport_call": MAX_EVENTS_PER_SPORT_CALL,
        "canonical_tables": ["tennis_results", "multisport_results"],
        "uses_existing_provider_quota": True,
        "selection_rules_changed": False,
        "pricing_rules_changed": False,
        "card_rules_changed": False,
        "staking_changed": False,
        "alerting_changed": False,
        "betting_authority_changed": False,
    }
    print(f"HR_THEORETICAL_RESULT_COLLECTION_INSTALL {state}", flush=True)
    return state
