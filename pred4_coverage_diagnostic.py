from __future__ import annotations

"""Exact, research-only PRED4 coverage diagnostics.

Uses the live PRED4 engine's own fit_event() method so diagnostic rejection
reasons are identical to the production shadow model. It never changes model
parameters, creates bets, calls providers, or affects execution authority.
"""

from datetime import datetime, timedelta, timezone
import json
import threading
import time
from typing import Any, Dict

from fastapi import FastAPI

_STARTED = False
_LOCK = threading.Lock()
_INTERVAL_SECONDS = 600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def diagnose(db, engine) -> Dict[str, Any]:
    now = _now()
    forecast_h = float(engine.settings.predictive_football_forecast_hours_before)
    horizon = now + timedelta(hours=forecast_h)
    events = db.fetchall(
        """SELECT * FROM events
           WHERE status='UPCOMING' AND commence_time>? AND commence_time<=?
             AND sport_key LIKE ?
           ORDER BY commence_time,event_id""",
        (now.isoformat(), horizon.isoformat(), "soccer_%"),
    )
    reasons: Dict[str, int] = {}
    examples: Dict[str, list] = {}
    existing = 0
    fit_ok = 0
    missing_fit_ok = 0
    for event in events:
        pred = db.fetchone(
            "SELECT id FROM football_predictive4_predictions WHERE event_id=?",
            (event["event_id"],),
        )
        if pred:
            existing += 1
            continue
        fit, reason = engine.fit_event(event, now=now)
        reason = str(reason or "UNKNOWN")
        if fit:
            fit_ok += 1
            missing_fit_ok += 1
            reason = "FIT_OK_BUT_PREDICTION_MISSING"
        reasons[reason] = reasons.get(reason, 0) + 1
        bucket = examples.setdefault(reason, [])
        if len(bucket) < 5:
            bucket.append({
                "event_id": str(event.get("event_id") or ""),
                "sport_key": str(event.get("sport_key") or ""),
                "league": str(event.get("league") or ""),
                "home_team": str(event.get("home_team") or ""),
                "away_team": str(event.get("away_team") or ""),
                "commence_time": str(event.get("commence_time") or ""),
            })
    payload = {
        "captured_at": now.isoformat(),
        "forecast_hours": forecast_h,
        "due_events": len(events),
        "existing_predictions": existing,
        "unpredicted_events": len(events) - existing,
        "fit_ok_missing_predictions": missing_fit_ok,
        "reasons": reasons,
        "examples": examples,
        "mechanical_gap": bool(missing_fit_ok),
    }
    try:
        db.record_collector_run(
            "PRED4_EXACT_COVERAGE_DIAGNOSTIC",
            not bool(missing_fit_ok),
            detail=json.dumps(payload, sort_keys=True),
        )
    except Exception:
        pass
    print("PRED4_EXACT_COVERAGE " + json.dumps(payload, sort_keys=True), flush=True)
    return payload


def latest(db) -> Dict[str, Any]:
    try:
        row = db.fetchone(
            """SELECT * FROM collector_runs
               WHERE run_type='PRED4_EXACT_COVERAGE_DIAGNOSTIC'
               ORDER BY id DESC LIMIT 1"""
        )
    except Exception:
        row = None
    if not row:
        return {"status": "WAITING"}
    raw = row.get("detail")
    try:
        payload = json.loads(raw or "{}")
    except Exception:
        payload = {"detail": raw}
    payload["status"] = "GAP" if payload.get("mechanical_gap") else "OK"
    return payload


def _loop(db, engine):
    time.sleep(10)
    while True:
        try:
            diagnose(db, engine)
        except Exception as exc:
            print(f"PRED4_EXACT_COVERAGE_ERROR {type(exc).__name__}: {exc}", flush=True)
        time.sleep(_INTERVAL_SECONDS)


def install(app: FastAPI, db, engine) -> None:
    @app.get("/api/pred4-coverage-diagnostic")
    def pred4_coverage_diagnostic_api():
        return latest(db)

    global _STARTED
    with _LOCK:
        if not _STARTED:
            _STARTED = True
            threading.Thread(
                target=_loop,
                args=(db, engine),
                daemon=True,
                name="pred4-coverage-diagnostic",
            ).start()
