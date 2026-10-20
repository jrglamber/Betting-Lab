from __future__ import annotations

"""No-subscription FHG1 settlement acceleration.

Disables the paid/current-season API-Football fallback that the account cannot
use, refreshes only the relevant football-data.co.uk current-season CSVs when an
already-frozen FHG1 candidate is overdue for settlement, and reconciles settled
half-time scores against the canonical CSV once it is available.

Forecasts, thresholds, prices and betting authority are unchanged.
"""

from datetime import datetime, timedelta, timezone
import requests
from typing import Any, Dict, Mapping

import first_half_goal_lab as fhg
import first_half_goal_settlement_patch as legacy
from db import sanitize_sensitive_text

VERSION = "FHG1_FREE_SETTLEMENT_V1"
MIN_AGE_MINUTES = 120
CSV_REFRESH_MINUTES = 60
RECONCILE_LOOKBACK_DAYS = 14
SOURCE_STATE_PREFIX = "FHG1:FREE_CSV_SETTLEMENT_V1"

_INSTALLED = False
_ORIGINAL_SETTLER = None
_ORIGINAL_API_FALLBACK = None


def _ensure_schema(db) -> None:
    iid = "BIGSERIAL PRIMARY KEY" if getattr(db, "is_postgres", False) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db.execute(
        f"""CREATE TABLE IF NOT EXISTS first_half_goal_settlement_reconciliation(
            id {iid},
            candidate_id INTEGER NOT NULL UNIQUE,
            checked_at TEXT NOT NULL,
            sport_key TEXT NOT NULL,
            commence_time TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            recorded_ht_home INTEGER,
            recorded_ht_away INTEGER,
            canonical_ht_home INTEGER,
            canonical_ht_away INTEGER,
            status TEXT NOT NULL,
            detail TEXT
        )"""
    )


def _overdue_open(db, now: datetime):
    cutoff = now - timedelta(minutes=MIN_AGE_MINUTES)
    try:
        return db.fetchall(
            """SELECT * FROM first_half_goal_candidates
               WHERE status='OPEN' AND commence_time<?
               ORDER BY commence_time,id""",
            (cutoff.isoformat(),),
        )
    except Exception:
        return []


def _refresh_due_csvs(db, now: datetime) -> Dict[str, Any]:
    rows = _overdue_open(db, now)
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        sport = str(row.get("sport_key") or "")
        code = fhg.LEAGUES.get(sport)
        ko = fhg._dt(row.get("commence_time"))
        if not code or ko is None:
            continue
        season = str(fhg._season_code(ko, 0))
        grouped.setdefault((sport, season), []).append(row)

    out = {"open_overdue": len(rows), "attempted": 0, "ok": 0, "failed": 0, "rows_seen": 0}
    if not grouped:
        return out

    session = requests.Session()
    for (sport, season), candidates in grouped.items():
        code = fhg.LEAGUES[sport]
        key = f"{SOURCE_STATE_PREFIX}:{sport}:{season}"
        if not fhg._source_due(db, key, now, CSV_REFRESH_MINUTES / 60.0):
            continue
        out["attempted"] += 1
        url = f"{fhg.FOOTBALL_DATA_BASE_URL}/{season}/{code}.csv"
        try:
            res = fhg._fetch_football_data_csv(
                session,
                url,
                timeout=20,
                headers={"User-Agent": "Project-Exit-Plan-Betting-Lab/FHG1-free-settlement"},
            )
            text = res.content.decode("utf-8-sig", errors="replace")
            n = int(fhg._import_csv(db, sport, code, season, text) or 0)
            fhg._source_state(db, key, sport, season, now, True, n)
            out["ok"] += 1
            out["rows_seen"] += n
            print(
                "FHG1_FREE_CSV_REFRESH "
                f"sport={sport} season={season} candidates={len(candidates)} rows_seen={n}",
                flush=True,
            )
        except Exception as exc:
            safe = sanitize_sensitive_text(str(exc))
            fhg._source_state(db, key, sport, season, now, False, error=safe)
            out["failed"] += 1
            print(
                f"FHG1_FREE_CSV_REFRESH_ERROR sport={sport} season={season} error={safe[:300]}",
                flush=True,
            )
    return out


def _canonical_match(db, candidate: Mapping[str, Any]):
    ko = fhg._dt(candidate.get("commence_time"))
    if ko is None:
        return None
    start = (ko - timedelta(hours=12)).isoformat()
    end = (ko + timedelta(hours=18)).isoformat()
    try:
        rows = db.fetchall(
            """SELECT * FROM first_half_goal_history
               WHERE sport_key=? AND home_key=? AND away_key=?
                 AND played_at>=? AND played_at<=?
               ORDER BY played_at,id""",
            (
                str(candidate.get("sport_key") or ""),
                str(candidate.get("home_key") or ""),
                str(candidate.get("away_key") or ""),
                start,
                end,
            ),
        )
    except Exception:
        return None
    return rows[0] if len(rows) == 1 else None


def _reconcile(db, now: datetime) -> Dict[str, int]:
    since = (now - timedelta(days=RECONCILE_LOOKBACK_DAYS)).isoformat()
    try:
        candidates = db.fetchall(
            """SELECT * FROM first_half_goal_candidates
               WHERE status='SETTLED' AND commence_time>=?
               ORDER BY commence_time,id""",
            (since,),
        )
    except Exception:
        candidates = []

    checked = matched = mismatched = 0
    for candidate in candidates:
        canonical = _canonical_match(db, candidate)
        if canonical is None:
            continue
        checked += 1
        rh = candidate.get("ht_home")
        ra = candidate.get("ht_away")
        ch = canonical.get("ht_home")
        ca = canonical.get("ht_away")
        status = "MATCH" if rh == ch and ra == ca else "MISMATCH"
        matched += int(status == "MATCH")
        mismatched += int(status == "MISMATCH")
        detail = "canonical football-data.co.uk half-time score agrees" if status == "MATCH" else "FLAG: recorded half-time score disagrees with canonical football-data.co.uk"
        db.execute(
            """INSERT INTO first_half_goal_settlement_reconciliation(
                   candidate_id,checked_at,sport_key,commence_time,home_team,away_team,
                   recorded_ht_home,recorded_ht_away,canonical_ht_home,canonical_ht_away,status,detail
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(candidate_id) DO UPDATE SET
                   checked_at=excluded.checked_at,
                   recorded_ht_home=excluded.recorded_ht_home,
                   recorded_ht_away=excluded.recorded_ht_away,
                   canonical_ht_home=excluded.canonical_ht_home,
                   canonical_ht_away=excluded.canonical_ht_away,
                   status=excluded.status,
                   detail=excluded.detail""",
            (
                int(candidate["id"]),
                now.isoformat(),
                str(candidate.get("sport_key") or ""),
                str(candidate.get("commence_time") or ""),
                str(candidate.get("home_team") or ""),
                str(candidate.get("away_team") or ""),
                rh,
                ra,
                ch,
                ca,
                status,
                detail,
            ),
        )
        if status == "MISMATCH":
            print(
                "FHG1_SETTLEMENT_RECONCILIATION_MISMATCH "
                f"candidate_id={candidate.get('id')} "
                f"event={candidate.get('home_team')} v {candidate.get('away_team')} "
                f"recorded={rh}-{ra} canonical={ch}-{ca}",
                flush=True,
            )
    return {"checked": checked, "matched": matched, "mismatched": mismatched}


def install(db) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_SETTLER, _ORIGINAL_API_FALLBACK
    if _INSTALLED or getattr(fhg, "_free_settlement_patch", False):
        return {"installed": True, "already_installed": True, "version": VERSION}

    fhg.ensure_schema(db)
    _ensure_schema(db)

    # dashboard_app installs the legacy fallback before pooled_dashboard_entrypoint
    # reaches us. Keep its canonical CSV settlement wrapper, but replace only the
    # inaccessible API-Football request path with a no-op.
    _ORIGINAL_API_FALLBACK = legacy._settle_from_api
    legacy._settle_from_api = lambda _db, _now: 0
    _ORIGINAL_SETTLER = fhg.settle_candidates

    def settle_candidates_free(db_obj, now=None):
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        settled = int(_ORIGINAL_SETTLER(db_obj, now) or 0)
        refresh = _refresh_due_csvs(db_obj, now)
        if int(refresh.get("ok") or 0) > 0:
            settled += int(_ORIGINAL_SETTLER(db_obj, now) or 0)
        recon = _reconcile(db_obj, now)
        if refresh.get("attempted") or recon.get("mismatched"):
            print(
                f"FHG1_FREE_SETTLEMENT state={{'settled': {settled}, 'refresh': {refresh}, 'reconciliation': {recon}}}",
                flush=True,
            )
        return settled

    fhg.settle_candidates = settle_candidates_free
    fhg._free_settlement_patch = True
    _INSTALLED = True

    # One immediate pass means an overdue candidate does not have to wait for the
    # next five-minute heartbeat after a deploy.
    now = datetime.now(timezone.utc)
    immediate_settled = int(settle_candidates_free(db, now) or 0)
    state = {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "api_football_disabled": True,
        "canonical_source": "football-data.co.uk",
        "overdue_after_minutes": MIN_AGE_MINUTES,
        "csv_refresh_minutes": CSV_REFRESH_MINUTES,
        "reconciliation_lookback_days": RECONCILE_LOOKBACK_DAYS,
        "immediate_settled": immediate_settled,
        "provider_credits": 0,
        "betting_authority": False,
    }
    print(f"FHG1_FREE_SETTLEMENT_INSTALL {state}", flush=True)
    return state
