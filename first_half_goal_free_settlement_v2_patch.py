from __future__ import annotations

"""Timely, research-only FHG1 settlement without a paid current-season feed.

football-data.co.uk remains canonical for training/history and reconciliation.
For already-frozen candidates only, this module adds a conservative read-only
SofaScore scheduled-events fallback when the canonical CSV has not yet published
half-time scores. The fallback never creates forecasts or alters thresholds,
prices, staking or betting authority.

The 25-Oct production export showed 43 candidates from 24-Oct still OPEN while
33 corresponding fixtures already had canonical full-time completion records.
The free CSV refresh was healthy but its newest rows lagged several days behind,
so repeated CSV polling alone could not make settlement timely.
"""

from datetime import datetime, timedelta, timezone
import json
import requests
from typing import Any, Dict, Mapping

import first_half_goal_lab as fhg
import first_half_goal_settlement_patch as legacy
from db import sanitize_sensitive_text

VERSION = "FHG1_FREE_SETTLEMENT_V2_TIMELY"
MIN_AGE_MINUTES = 120
CSV_REFRESH_MINUTES = 60
SOFA_MIN_AGE_MINUTES = 180
SOFA_REFRESH_MINUTES = 60
RECONCILE_LOOKBACK_DAYS = 14
CSV_SOURCE_STATE_PREFIX = "FHG1:FREE_CSV_SETTLEMENT_V2"
SOFA_SOURCE_STATE_PREFIX = "FHG1:SOFASCORE_SETTLEMENT_V1"
SOFA_URL = "https://www.sofascore.com/api/v1/sport/football/scheduled-events/{day}"

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


def _overdue_open(db, now: datetime, minutes: int = MIN_AGE_MINUTES):
    cutoff = now - timedelta(minutes=minutes)
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
    rows = _overdue_open(db, now, MIN_AGE_MINUTES)
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
        key = f"{CSV_SOURCE_STATE_PREFIX}:{sport}:{season}"
        if not fhg._source_due(db, key, now, CSV_REFRESH_MINUTES / 60.0):
            continue
        out["attempted"] += 1
        url = f"{fhg.FOOTBALL_DATA_BASE_URL}/{season}/{code}.csv"
        try:
            res = fhg._fetch_football_data_csv(
                session,
                url,
                timeout=20,
                headers={"User-Agent": "Project-Exit-Plan-Betting-Lab/FHG1-free-settlement-v2"},
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


def _sofa_fixture_rows(payload: Any) -> list[dict[str, Any]]:
    events = payload.get("events") if isinstance(payload, Mapping) else []
    if not isinstance(events, list):
        return []

    out: list[dict[str, Any]] = []
    for item in events:
        if not isinstance(item, Mapping):
            continue
        status = item.get("status") if isinstance(item.get("status"), Mapping) else {}
        status_type = str(status.get("type") or "").lower()
        status_code = status.get("code")
        if status_type not in {"finished", "ended"} and status_code not in {100}:
            continue

        home = item.get("homeTeam") if isinstance(item.get("homeTeam"), Mapping) else {}
        away = item.get("awayTeam") if isinstance(item.get("awayTeam"), Mapping) else {}
        hs = item.get("homeScore") if isinstance(item.get("homeScore"), Mapping) else {}
        aas = item.get("awayScore") if isinstance(item.get("awayScore"), Mapping) else {}
        home_name = str(home.get("name") or "").strip()
        away_name = str(away.get("name") or "").strip()
        if not home_name or not away_name:
            continue

        try:
            hh = int(hs.get("period1"))
            ha = int(aas.get("period1"))
        except (TypeError, ValueError):
            continue

        kickoff = None
        try:
            ts = int(item.get("startTimestamp"))
            kickoff = datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            kickoff = None

        out.append(
            {
                "home": home_name,
                "away": away_name,
                "home_key": fhg._normalize_text(home_name),
                "away_key": fhg._normalize_text(away_name),
                "kickoff": kickoff,
                "ht_home": hh,
                "ht_away": ha,
            }
        )
    return out


def _settle_from_sofascore(db, now: datetime) -> Dict[str, Any]:
    rows = _overdue_open(db, now, SOFA_MIN_AGE_MINUTES)
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        ko = fhg._dt(row.get("commence_time"))
        if ko is None:
            continue
        day = ko.astimezone(fhg.LONDON).date().isoformat()
        grouped.setdefault(day, []).append(row)

    out = {
        "open_overdue": len(rows),
        "dates": len(grouped),
        "attempted": 0,
        "ok": 0,
        "failed": 0,
        "api_rows": 0,
        "usable_rows": 0,
        "settled": 0,
        "exact": 0,
        "alias": 0,
        "unmatched": 0,
    }
    if not grouped:
        return out

    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Project-Exit-Plan-Betting-Lab/FHG1-settlement; +research-only)"
    }
    for day, candidates in grouped.items():
        key = f"{SOFA_SOURCE_STATE_PREFIX}:{day}"
        if not fhg._source_due(db, key, now, SOFA_REFRESH_MINUTES / 60.0):
            continue

        out["attempted"] += 1
        try:
            response = session.get(
                SOFA_URL.format(day=day),
                headers=headers,
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json() if response.content else {}
            raw_events = payload.get("events") if isinstance(payload, Mapping) else []
            raw_count = len(raw_events) if isinstance(raw_events, list) else 0
            fixtures = _sofa_fixture_rows(payload)
            out["api_rows"] += raw_count
            out["usable_rows"] += len(fixtures)
            fhg._source_state(db, key, "soccer_all", day, now, True, raw_count)

            examples: list[str] = []
            exact = alias = unmatched = settled = 0
            for candidate in candidates:
                fixture, mode = legacy._match_fixture(candidate, fixtures)
                if fixture is None:
                    unmatched += 1
                    if len(examples) < 8:
                        examples.append(
                            f"{candidate.get('home_team')} v {candidate.get('away_team')}[{mode}]"
                        )
                    continue

                hh = int(fixture["ht_home"])
                ha = int(fixture["ht_away"])
                hit = 1 if hh + ha > 0 else 0
                changed = db.execute(
                    """UPDATE first_half_goal_candidates
                       SET status='SETTLED',ht_home=?,ht_away=?,fhg_hit=?,settled_at=?
                       WHERE id=? AND status='OPEN'""",
                    (hh, ha, hit, now.isoformat(), int(candidate["id"])),
                )
                if int(changed or 0) <= 0:
                    continue
                settled += 1
                if mode == "exact":
                    exact += 1
                else:
                    alias += 1

            out["settled"] += settled
            out["exact"] += exact
            out["alias"] += alias
            out["unmatched"] += unmatched
            out["ok"] += 1
            print(
                "FHG1_SOFASCORE_SETTLEMENT "
                f"day={day} candidates={len(candidates)} api_rows={raw_count} "
                f"usable={len(fixtures)} settled={settled} exact={exact} alias={alias} "
                f"unmatched={unmatched} examples={examples}",
                flush=True,
            )
        except Exception as exc:
            safe = sanitize_sensitive_text(str(exc))
            fhg._source_state(db, key, "soccer_all", day, now, False, error=safe)
            out["failed"] += 1
            print(
                f"FHG1_SOFASCORE_SETTLEMENT_ERROR day={day} error={safe[:300]}",
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
        detail = (
            "canonical football-data.co.uk half-time score agrees"
            if status == "MATCH"
            else "FLAG: recorded half-time score disagrees with canonical football-data.co.uk"
        )
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
    if _INSTALLED or getattr(fhg, "_free_settlement_patch_v2", False):
        return {"installed": True, "already_installed": True, "version": VERSION}

    fhg.ensure_schema(db)
    _ensure_schema(db)

    # dashboard_app installs the inaccessible API-Football fallback before the
    # pooled production entrypoint reaches us. Keep the canonical CSV wrapper but
    # suppress that paid/current-season request path.
    _ORIGINAL_API_FALLBACK = legacy._settle_from_api
    legacy._settle_from_api = lambda _db, _now: 0
    _ORIGINAL_SETTLER = fhg.settle_candidates

    def settle_candidates_free(db_obj, now=None):
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)

        settled = int(_ORIGINAL_SETTLER(db_obj, now) or 0)

        refresh = _refresh_due_csvs(db_obj, now)
        if int(refresh.get("ok") or 0) > 0:
            settled += int(_ORIGINAL_SETTLER(db_obj, now) or 0)

        sofa = _settle_from_sofascore(db_obj, now)
        settled += int(sofa.get("settled") or 0)

        recon = _reconcile(db_obj, now)
        state = {
            "settled": settled,
            "refresh": refresh,
            "sofascore": sofa,
            "reconciliation": recon,
        }
        try:
            db_obj.record_collector_run(
                "FHG1_SETTLEMENT_HEALTH",
                bool(not sofa.get("failed")),
                detail=json.dumps(state, sort_keys=True, default=str),
            )
        except Exception:
            pass

        if (
            refresh.get("attempted")
            or sofa.get("attempted")
            or recon.get("mismatched")
            or settled
        ):
            print(f"FHG1_FREE_SETTLEMENT state={state}", flush=True)
        return settled

    fhg.settle_candidates = settle_candidates_free
    fhg._free_settlement_patch_v2 = True
    _INSTALLED = True

    # Immediate pass means a deploy can clear overdue rows without waiting for
    # the next heartbeat.
    now = datetime.now(timezone.utc)
    immediate_settled = int(settle_candidates_free(db, now) or 0)
    state = {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "api_football_disabled": True,
        "canonical_source": "football-data.co.uk",
        "timely_fallback": "sofascore_public_scheduled_events",
        "fallback_training_source": False,
        "overdue_after_minutes": MIN_AGE_MINUTES,
        "sofascore_after_minutes": SOFA_MIN_AGE_MINUTES,
        "csv_refresh_minutes": CSV_REFRESH_MINUTES,
        "sofascore_refresh_minutes": SOFA_REFRESH_MINUTES,
        "reconciliation_lookback_days": RECONCILE_LOOKBACK_DAYS,
        "immediate_settled": immediate_settled,
        "provider_credits": 0,
        "betting_authority": False,
    }
    print(f"FHG1_FREE_SETTLEMENT_INSTALL {state}", flush=True)
    return state


__all__ = ["VERSION", "install"]
