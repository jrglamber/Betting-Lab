from __future__ import annotations

"""Broad football research coverage without promoting new lanes to production.

This patch deliberately separates *coverage* from *trust*:

* FHG1 is allowed to research every football-data.co.uk league for which the lab
  already has a safe score/half-time-score parser. Existing thresholds and
  forward-only evidence rules remain unchanged.
* PXG1/API-Football receives additional high-confidence competition mappings so
  domestic cups and more configured leagues can accumulate current proxy-xG
  history under the existing hard daily provider budget.
* XCOMP1 is a zero-provider-call, forward-only cross-competition fallback model.
  It is intended for cups/continental competitions (and other soccer events for
  which PRED1/2/3/4 cannot fit). It borrows each club's already-stored domestic
  result history, writes its own tables, and is always labelled RESEARCH_ONLY.

No real-bet/order authority is added or changed by this module.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

VERSION = "FOOTBALL_COVERAGE_EXPANSION_V1"
MODEL_VERSION = "XCOMP1_CROSS_COMP_POISSON_FORWARD_V1"
HORIZON_HOURS = 96
LOOKBACK_DAYS = 730
HALF_LIFE_DAYS = 180.0
PRIOR_MATCHES = 5.0
MIN_TEAM_MATCHES = 5
MIN_BASELINE_MATCHES = 20
MIN_DERIVED_PROBABILITY = 0.55

_INSTALLED = False
_ORIGINAL_WORKER_ONE_CYCLE = None
_ORIGINAL_CHALLENGE_FUTURE = None
_ORIGINAL_CHALLENGE_SUPPORT = None
_ORIGINAL_CHALLENGE_HEALTH = None


# Free football-data.co.uk score files that FHG1 was not previously using.
# These are all competitions already supported by PRED1/PRED2's historical
# bootstrap and therefore require no additional paid provider traffic.
FHG_EXTRA_LEAGUES: Dict[str, str] = {
    "soccer_germany_bundesliga": "D1",
    "soccer_germany_bundesliga2": "D2",
    "soccer_italy_serie_a": "I1",
    "soccer_italy_serie_b": "I2",
    "soccer_spain_la_liga": "SP1",
    "soccer_spain_segunda_division": "SP2",
    "soccer_france_ligue_one": "F1",
    "soccer_france_ligue_two": "F2",
    "soccer_netherlands_eredivisie": "N1",
    "soccer_belgium_first_div": "B1",
    "soccer_portugal_primeira_liga": "P1",
    "soccer_turkey_super_league": "T1",
    "soccer_greece_super_league": "G1",
}


# High-confidence API-Football competition names. The base PXG1 collector still
# enforces settings.sport_keys plus its existing API daily-call budget/reserve.
PXG_EXTRA_LEAGUE_MAP: Dict[tuple[str, str], str] = {
    # Domestic cups.
    ("england", "league cup"): "soccer_england_efl_cup",
    ("england", "efl cup"): "soccer_england_efl_cup",
    ("england", "fa cup"): "soccer_fa_cup",
    ("france", "coupe de france"): "soccer_france_coupe_de_france",
    ("germany", "dfb pokal"): "soccer_germany_dfb_pokal",
    ("italy", "coppa italia"): "soccer_italy_coppa_italia",
    ("spain", "copa del rey"): "soccer_spain_copa_del_rey",
    # Additional domestic leagues already present in Betting Lab SPORT_KEYS.
    ("usa", "major league soccer"): "soccer_usa_mls",
    ("mexico", "liga mx"): "soccer_mexico_ligamx",
    ("brazil", "serie a"): "soccer_brazil_campeonato",
    ("brazil", "serie b"): "soccer_brazil_serie_b",
    ("argentina", "liga profesional argentina"): "soccer_argentina_primera_division",
    ("argentina", "primera division"): "soccer_argentina_primera_division",
    ("australia", "a league"): "soccer_australia_aleague",
    ("chile", "primera division"): "soccer_chile_campeonato",
    ("china", "super league"): "soccer_china_superleague",
    ("japan", "j1 league"): "soccer_japan_j_league",
    ("south korea", "k league 1"): "soccer_korea_kleague1",
    ("russia", "premier league"): "soccer_russia_premier_league",
    ("saudi arabia", "pro league"): "soccer_saudi_arabia_pro_league",
    # International/continental competitions already in SPORT_KEYS.
    ("world", "conmebol libertadores"): "soccer_conmebol_copa_libertadores",
    ("world", "conmebol sudamericana"): "soccer_conmebol_copa_sudamericana",
    ("world", "copa america"): "soccer_conmebol_copa_america",
    ("world", "leagues cup"): "soccer_concacaf_leagues_cup",
    ("world", "fifa club world cup"): "soccer_fifa_club_world_cup",
    ("world", "world cup"): "soccer_fifa_world_cup",
    ("world", "world cup qualification europe"): "soccer_fifa_world_cup_qualifiers_europe",
    ("world", "world cup qualification south america"): "soccer_fifa_world_cup_qualifiers_south_america",
    ("world", "uefa european championship"): "soccer_uefa_european_championship",
    ("world", "euro championship"): "soccer_uefa_european_championship",
    ("world", "euro championship qualification"): "soccer_uefa_euro_qualification",
}


def _utc(value: Any = None) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse(value: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _number(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _clamp(value: float, low: float = 0.20, high: float = 4.50) -> float:
    return max(low, min(high, float(value)))


def _weight(days_ago: float) -> float:
    return 0.5 ** (max(0.0, float(days_ago)) / HALF_LIFE_DAYS)


def _research_competition(sport_key: str) -> bool:
    key = str(sport_key or "").lower()
    markers = (
        "cup", "uefa_", "fifa_", "conmebol_", "concacaf_",
        "qualification", "qualifiers", "nations_league",
    )
    return any(marker in key for marker in markers)


def _primary_competition_weight(sport_key: str) -> float:
    # Prefer a club's league as its baseline if both league and cup history are
    # available; cup/continental rows still contribute to team form below.
    return 0.25 if _research_competition(sport_key) else 1.0


def _ensure_schema(db) -> None:
    iid = "BIGSERIAL PRIMARY KEY" if getattr(db, "is_postgres", False) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    statements = (
        f"""CREATE TABLE IF NOT EXISTS football_cross_comp_predictions(
            id {iid},
            event_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            sport_key TEXT NOT NULL,
            league TEXT,
            commence_time TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            model_version TEXT NOT NULL,
            expected_home_goals REAL NOT NULL,
            expected_away_goals REAL NOT NULL,
            home_probability REAL NOT NULL,
            draw_probability REAL NOT NULL,
            away_probability REAL NOT NULL,
            btts_yes_probability REAL NOT NULL,
            btts_no_probability REAL NOT NULL,
            over_2_5_probability REAL NOT NULL,
            under_2_5_probability REAL NOT NULL,
            home_fair_odds REAL NOT NULL,
            draw_fair_odds REAL NOT NULL,
            away_fair_odds REAL NOT NULL,
            home_sample INTEGER NOT NULL,
            away_sample INTEGER NOT NULL,
            home_primary_sport_key TEXT,
            away_primary_sport_key TEXT,
            source_competitions_json TEXT,
            evidence_mode TEXT NOT NULL DEFAULT 'FORWARD_RESEARCH_CROSS_COMP',
            status TEXT NOT NULL DEFAULT 'OPEN',
            home_score INTEGER,
            away_score INTEGER,
            brier_score REAL,
            settled_at TEXT
        )""",
        f"""CREATE TABLE IF NOT EXISTS football_cross_comp_candidates(
            id {iid},
            candidate_key TEXT NOT NULL UNIQUE,
            prediction_id INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            commence_time TEXT NOT NULL,
            sport_key TEXT NOT NULL,
            league TEXT,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            market_key TEXT NOT NULL,
            selection TEXT NOT NULL,
            point REAL,
            fair_probability REAL NOT NULL,
            fair_odds REAL NOT NULL,
            evidence_mode TEXT NOT NULL DEFAULT 'FORWARD_RESEARCH_CROSS_COMP',
            status TEXT NOT NULL DEFAULT 'OPEN',
            result TEXT,
            settled_at TEXT
        )""",
    )
    for sql in statements:
        db.execute(sql)
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_xcomp_pred_status_time ON football_cross_comp_predictions(status,commence_time)",
        "CREATE INDEX IF NOT EXISTS idx_xcomp_candidate_status_time ON football_cross_comp_candidates(status,commence_time)",
        "CREATE INDEX IF NOT EXISTS idx_xcomp_candidate_market ON football_cross_comp_candidates(market_key,status)",
    ):
        try:
            db.execute(sql)
        except Exception:
            pass


def _training_snapshot(db, now: datetime) -> Dict[str, Any]:
    from predictive_football import _normalize_text

    start = (now - timedelta(days=LOOKBACK_DAYS)).isoformat()
    rows = [
        dict(r) for r in db.fetchall(
            """SELECT sport_key,played_at,home_team,away_team,home_goals,away_goals
               FROM football_predictive_training_matches
               WHERE played_at>=? AND played_at<?
               ORDER BY played_at""",
            (start, now.isoformat()),
        )
    ]

    baseline_acc: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"n": 0.0, "home_goals": 0.0, "away_goals": 0.0}
    )
    team_rows: Dict[str, list[tuple[dict[str, Any], bool]]] = defaultdict(list)
    names: Dict[str, str] = {}

    for row in rows:
        sport = str(row.get("sport_key") or "")
        hg = _number(row.get("home_goals"))
        ag = _number(row.get("away_goals"))
        if not sport or hg is None or ag is None:
            continue
        acc = baseline_acc[sport]
        acc["n"] += 1.0
        acc["home_goals"] += hg
        acc["away_goals"] += ag
        home = str(row.get("home_team") or "")
        away = str(row.get("away_team") or "")
        hk = _normalize_text(home)
        ak = _normalize_text(away)
        if hk:
            team_rows[hk].append((row, True))
            names.setdefault(hk, home)
        if ak:
            team_rows[ak].append((row, False))
            names.setdefault(ak, away)

    baselines: Dict[str, Dict[str, float]] = {}
    for sport, acc in baseline_acc.items():
        n = int(acc["n"])
        if n < MIN_BASELINE_MATCHES:
            continue
        baselines[sport] = {
            "n": float(n),
            "home": max(0.20, acc["home_goals"] / n),
            "away": max(0.20, acc["away_goals"] / n),
        }
    return {"rows": rows, "baselines": baselines, "team_rows": team_rows, "names": names}


def _team_key(target: str, snapshot: Mapping[str, Any]) -> Optional[str]:
    from predictive_football import _normalize_text, _team_mapping

    key = _normalize_text(target)
    team_rows = snapshot.get("team_rows") or {}
    if key in team_rows:
        return key
    names_map = snapshot.get("names") or {}
    mapped = _team_mapping(str(target), list(names_map.values()))
    if not mapped:
        return None
    mapped_name, score = mapped
    # The generic PRED mapper can go as low as 0.72. Cross-competition research
    # is deliberately stricter because the candidate name universe is larger.
    if float(score) < 0.84:
        return None
    mapped_key = _normalize_text(mapped_name)
    return mapped_key if mapped_key in team_rows else None


def _team_profile(
    team_key: str,
    kickoff: datetime,
    snapshot: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    from predictive_football import _normalize_text

    baselines = snapshot.get("baselines") or {}
    appearances = list((snapshot.get("team_rows") or {}).get(team_key) or [])
    usable: list[tuple[dict[str, Any], bool, float]] = []
    primary_mass: Dict[str, float] = defaultdict(float)

    for row, is_home in appearances:
        played = _parse(row.get("played_at"))
        sport = str(row.get("sport_key") or "")
        baseline = baselines.get(sport)
        if played is None or played >= kickoff or not baseline:
            continue
        w = _weight((kickoff - played).total_seconds() / 86400.0)
        if w <= 0:
            continue
        usable.append((row, is_home, w))
        primary_mass[sport] += w * _primary_competition_weight(sport)

    if len(usable) < MIN_TEAM_MATCHES or not primary_mass:
        return None
    primary = max(primary_mass.items(), key=lambda kv: (kv[1], kv[0]))[0]
    if primary not in baselines:
        return None

    attack_num = defense_num = total_w = 0.0
    competition_mass: Dict[str, float] = defaultdict(float)
    for row, is_home, w in usable:
        sport = str(row.get("sport_key") or "")
        baseline = baselines.get(sport)
        if not baseline:
            continue
        hg = float(row.get("home_goals") or 0.0)
        ag = float(row.get("away_goals") or 0.0)
        if is_home:
            scored = hg
            conceded = ag
            scored_base = float(baseline["home"])
            conceded_base = float(baseline["away"])
        else:
            scored = ag
            conceded = hg
            scored_base = float(baseline["away"])
            conceded_base = float(baseline["home"])
        attack_num += (scored / max(0.20, scored_base)) * w
        defense_num += (conceded / max(0.20, conceded_base)) * w
        total_w += w
        competition_mass[sport] += w

    if total_w <= 0:
        return None
    attack = (attack_num + PRIOR_MATCHES) / (total_w + PRIOR_MATCHES)
    defense = (defense_num + PRIOR_MATCHES) / (total_w + PRIOR_MATCHES)
    source_competitions = [
        sport for sport, _ in sorted(
            competition_mass.items(), key=lambda kv: (-kv[1], kv[0])
        )[:6]
    ]
    return {
        "sample": len(usable),
        "effective_sample": total_w,
        "primary_sport_key": primary,
        "attack": max(0.35, min(2.50, attack)),
        "defense": max(0.35, min(2.50, defense)),
        "source_competitions": source_competitions,
    }


def _main_prediction_exists(db, event_id: str) -> bool:
    for table in (
        "football_predictive_predictions",
        "football_predictive2_predictions",
        "football_predictive3_predictions",
        "football_predictive4_predictions",
    ):
        try:
            if db.fetchone(f"SELECT id FROM {table} WHERE event_id=? LIMIT 1", (event_id,)):
                return True
        except Exception:
            continue
    return False


def _fit_event(event: Mapping[str, Any], snapshot: Mapping[str, Any]) -> tuple[Optional[Dict[str, Any]], str]:
    from predictive_football import score_probabilities, btts_probabilities, total_probabilities

    kickoff = _parse(event.get("commence_time"))
    if kickoff is None:
        return None, "INVALID_KICKOFF"
    home_key = _team_key(str(event.get("home_team") or ""), snapshot)
    away_key = _team_key(str(event.get("away_team") or ""), snapshot)
    if not home_key:
        return None, "HOME_TEAM_UNMAPPED"
    if not away_key:
        return None, "AWAY_TEAM_UNMAPPED"
    hp = _team_profile(home_key, kickoff, snapshot)
    ap = _team_profile(away_key, kickoff, snapshot)
    if not hp:
        return None, "INSUFFICIENT_HOME_CROSS_COMP_HISTORY"
    if not ap:
        return None, "INSUFFICIENT_AWAY_CROSS_COMP_HISTORY"

    baselines = snapshot.get("baselines") or {}
    home_base = baselines.get(hp["primary_sport_key"])
    away_base = baselines.get(ap["primary_sport_key"])
    if not home_base or not away_base:
        return None, "PRIMARY_BASELINE_UNAVAILABLE"

    home_lambda = _clamp(float(home_base["home"]) * float(hp["attack"]) * float(ap["defense"]))
    away_lambda = _clamp(float(away_base["away"]) * float(ap["attack"]) * float(hp["defense"]))
    home_p, draw_p, away_p = score_probabilities(home_lambda, away_lambda)
    btts_yes, btts_no = btts_probabilities(home_lambda, away_lambda)
    over25, under25 = total_probabilities(home_lambda, away_lambda, 2.5)

    comps = sorted(set(hp["source_competitions"] + ap["source_competitions"]))
    return {
        "expected_home_goals": home_lambda,
        "expected_away_goals": away_lambda,
        "home_probability": home_p,
        "draw_probability": draw_p,
        "away_probability": away_p,
        "btts_yes_probability": btts_yes,
        "btts_no_probability": btts_no,
        "over_2_5_probability": over25,
        "under_2_5_probability": under25,
        "home_fair_odds": 1.0 / max(1e-9, home_p),
        "draw_fair_odds": 1.0 / max(1e-9, draw_p),
        "away_fair_odds": 1.0 / max(1e-9, away_p),
        "home_sample": int(hp["sample"]),
        "away_sample": int(ap["sample"]),
        "home_primary_sport_key": hp["primary_sport_key"],
        "away_primary_sport_key": ap["primary_sport_key"],
        "source_competitions": comps,
    }, "OK"


def _candidate_rows(event: Mapping[str, Any], fit: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    h2h = [
        (str(event.get("home_team") or "Home"), float(fit["home_probability"])),
        ("Draw", float(fit["draw_probability"])),
        (str(event.get("away_team") or "Away"), float(fit["away_probability"])),
    ]
    selection, probability = max(h2h, key=lambda x: x[1])
    rows.append({
        "market_key": "h2h", "selection": selection, "point": None,
        "fair_probability": probability, "fair_odds": 1.0 / max(1e-9, probability),
    })

    btts = max(
        (("Yes", float(fit["btts_yes_probability"])), ("No", float(fit["btts_no_probability"]))),
        key=lambda x: x[1],
    )
    if btts[1] >= MIN_DERIVED_PROBABILITY:
        rows.append({
            "market_key": "btts", "selection": btts[0], "point": None,
            "fair_probability": btts[1], "fair_odds": 1.0 / btts[1],
        })

    total = max(
        (("Over", float(fit["over_2_5_probability"])), ("Under", float(fit["under_2_5_probability"]))),
        key=lambda x: x[1],
    )
    if total[1] >= MIN_DERIVED_PROBABILITY:
        rows.append({
            "market_key": "totals", "selection": total[0], "point": 2.5,
            "fair_probability": total[1], "fair_odds": 1.0 / total[1],
        })
    return rows


def freeze_predictions(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    _ensure_schema(db)
    now = _utc(now)
    end = now + timedelta(hours=HORIZON_HOURS)
    snapshot = _training_snapshot(db, now)
    events = [
        dict(r) for r in db.fetchall(
            """SELECT * FROM events
               WHERE status='UPCOMING' AND commence_time>? AND commence_time<=?
                 AND sport_key LIKE ?
               ORDER BY commence_time,event_id""",
            (now.isoformat(), end.isoformat(), "soccer_%"),
        )
    ]
    created = skipped = 0
    reasons: Dict[str, int] = defaultdict(int)
    for event in events:
        eid = str(event.get("event_id") or "")
        if not eid:
            continue
        if db.fetchone("SELECT id FROM football_cross_comp_predictions WHERE event_id=?", (eid,)):
            continue
        # Cups/continental events receive a separate comparison forecast even if
        # a main model eventually gains enough exact-competition history. For an
        # ordinary league, XCOMP1 acts only as a fallback where all main models
        # failed to fit.
        if not _research_competition(str(event.get("sport_key") or "")) and _main_prediction_exists(db, eid):
            continue
        fit, reason = _fit_event(event, snapshot)
        if not fit:
            skipped += 1
            reasons[reason] += 1
            continue
        created_at = now.isoformat()
        db.execute(
            """INSERT INTO football_cross_comp_predictions(
                event_id,created_at,sport_key,league,commence_time,home_team,away_team,
                model_version,expected_home_goals,expected_away_goals,
                home_probability,draw_probability,away_probability,
                btts_yes_probability,btts_no_probability,over_2_5_probability,under_2_5_probability,
                home_fair_odds,draw_fair_odds,away_fair_odds,
                home_sample,away_sample,home_primary_sport_key,away_primary_sport_key,
                source_competitions_json,evidence_mode,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                eid, created_at, str(event.get("sport_key") or ""), str(event.get("league") or ""),
                str(event.get("commence_time") or ""), str(event.get("home_team") or ""),
                str(event.get("away_team") or ""), MODEL_VERSION,
                fit["expected_home_goals"], fit["expected_away_goals"],
                fit["home_probability"], fit["draw_probability"], fit["away_probability"],
                fit["btts_yes_probability"], fit["btts_no_probability"],
                fit["over_2_5_probability"], fit["under_2_5_probability"],
                fit["home_fair_odds"], fit["draw_fair_odds"], fit["away_fair_odds"],
                fit["home_sample"], fit["away_sample"], fit["home_primary_sport_key"],
                fit["away_primary_sport_key"], json.dumps(fit["source_competitions"], separators=(",", ":")),
                "FORWARD_RESEARCH_CROSS_COMP", "OPEN",
            ),
        )
        pred = db.fetchone("SELECT id FROM football_cross_comp_predictions WHERE event_id=?", (eid,))
        if not pred:
            continue
        for item in _candidate_rows(event, fit):
            point_key = "" if item["point"] is None else f"{float(item['point']):.1f}"
            key = "|".join((eid, item["market_key"], item["selection"], point_key))
            db.execute(
                """INSERT INTO football_cross_comp_candidates(
                    candidate_key,prediction_id,event_id,created_at,commence_time,sport_key,league,
                    home_team,away_team,market_key,selection,point,fair_probability,fair_odds,
                    evidence_mode,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(candidate_key) DO NOTHING""",
                (
                    key, int(pred["id"]), eid, created_at, str(event.get("commence_time") or ""),
                    str(event.get("sport_key") or ""), str(event.get("league") or ""),
                    str(event.get("home_team") or ""), str(event.get("away_team") or ""),
                    item["market_key"], item["selection"], item["point"], item["fair_probability"],
                    item["fair_odds"], "FORWARD_RESEARCH_CROSS_COMP", "OPEN",
                ),
            )
        created += 1
    return {
        "created": created,
        "skipped": skipped,
        "reasons": dict(sorted(reasons.items())),
        "training_rows": len(snapshot.get("rows") or []),
        "baseline_competitions": len(snapshot.get("baselines") or {}),
        "provider_calls": 0,
        "betting_authority": False,
    }


def _candidate_result(row: Mapping[str, Any], home_score: int, away_score: int) -> str:
    market = str(row.get("market_key") or "")
    selection = str(row.get("selection") or "")
    if market == "h2h":
        actual = (
            str(row.get("home_team") or "") if home_score > away_score
            else str(row.get("away_team") or "") if away_score > home_score
            else "Draw"
        )
        return "WIN" if selection == actual else "LOSS"
    if market == "btts":
        actual = "Yes" if home_score > 0 and away_score > 0 else "No"
        return "WIN" if selection == actual else "LOSS"
    if market == "totals":
        point = float(row.get("point") or 2.5)
        total = home_score + away_score
        actual = "Over" if total > point else "Under"
        return "WIN" if selection == actual else "LOSS"
    return "VOID"


def settle_predictions(db, now: Optional[datetime] = None) -> int:
    _ensure_schema(db)
    now = _utc(now)
    rows = [
        dict(r) for r in db.fetchall(
            """SELECT p.*,r.home_score AS result_home_score,r.away_score AS result_away_score
               FROM football_cross_comp_predictions p
               JOIN event_results r ON r.event_id=p.event_id
               WHERE p.status='OPEN'
               ORDER BY p.id"""
        )
    ]
    settled = 0
    for pred in rows:
        try:
            hs = int(pred["result_home_score"])
            a_s = int(pred["result_away_score"])
        except Exception:
            continue
        actual = (1.0 if hs > a_s else 0.0, 1.0 if hs == a_s else 0.0, 1.0 if a_s > hs else 0.0)
        probs = (float(pred["home_probability"]), float(pred["draw_probability"]), float(pred["away_probability"]))
        brier = sum((p - y) ** 2 for p, y in zip(probs, actual)) / 3.0
        stamp = now.isoformat()
        db.execute(
            """UPDATE football_cross_comp_predictions
               SET status='SETTLED',home_score=?,away_score=?,brier_score=?,settled_at=?
               WHERE id=? AND status='OPEN'""",
            (hs, a_s, brier, stamp, int(pred["id"])),
        )
        candidates = [
            dict(r) for r in db.fetchall(
                "SELECT * FROM football_cross_comp_candidates WHERE prediction_id=? AND status='OPEN' ORDER BY id",
                (int(pred["id"]),),
            )
        ]
        for candidate in candidates:
            result = _candidate_result(candidate, hs, a_s)
            db.execute(
                "UPDATE football_cross_comp_candidates SET status='SETTLED',result=?,settled_at=? WHERE id=? AND status='OPEN'",
                (result, stamp, int(candidate["id"])),
            )
        settled += 1
    return settled


def run_cross_comp_maintenance(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = _utc(now)
    settled = settle_predictions(db, now)
    frozen = freeze_predictions(db, now)
    frozen["settled"] = settled
    return frozen


def _expand_fhg() -> int:
    import first_half_goal_lab as fhg

    before = len(fhg.LEAGUES)
    fhg.LEAGUES.update(FHG_EXTRA_LEAGUES)
    return len(fhg.LEAGUES) - before


def _expand_pxg() -> int:
    import proxy_xg

    before = len(proxy_xg.API_FOOTBALL_LEAGUE_MAP)
    proxy_xg.API_FOOTBALL_LEAGUE_MAP.update(PXG_EXTRA_LEAGUE_MAP)
    return len(proxy_xg.API_FOOTBALL_LEAGUE_MAP) - before


def _install_worker_bridge() -> bool:
    global _ORIGINAL_WORKER_ONE_CYCLE
    import worker

    if getattr(worker.Worker.one_cycle, "_football_coverage_expansion", False):
        return False
    _ORIGINAL_WORKER_ONE_CYCLE = worker.Worker.one_cycle

    def one_cycle_with_cross_comp(self):
        result = _ORIGINAL_WORKER_ONE_CYCLE(self)
        try:
            state = run_cross_comp_maintenance(self.db, datetime.now(timezone.utc))
            self.db.record_collector_run(
                "FOOTBALL_CROSS_COMP_MAINT", True,
                detail="; ".join(f"{k}={v}" for k, v in state.items()),
            )
        except Exception as exc:
            try:
                self.db.record_collector_run("FOOTBALL_CROSS_COMP_MAINT", False, detail=str(exc))
            except Exception:
                pass
            print(f"FOOTBALL_CROSS_COMP_MAINT_ERROR {type(exc).__name__}: {exc}", flush=True)
        return result

    one_cycle_with_cross_comp._football_coverage_expansion = True
    worker.Worker.one_cycle = one_cycle_with_cross_comp
    return True


def _install_export_tables() -> int:
    import exporter

    extra = ("football_cross_comp_predictions", "football_cross_comp_candidates")
    before = len(exporter.EXPORT_TABLES)
    exporter.EXPORT_TABLES = tuple(dict.fromkeys(tuple(exporter.EXPORT_TABLES) + extra))
    return len(exporter.EXPORT_TABLES) - before


def _install_challenge_bridge() -> bool:
    global _ORIGINAL_CHALLENGE_FUTURE, _ORIGINAL_CHALLENGE_SUPPORT, _ORIGINAL_CHALLENGE_HEALTH
    import challenge_export_patch as challenge

    if getattr(challenge._future_candidates, "_football_coverage_expansion", False):
        return False

    _ORIGINAL_CHALLENGE_FUTURE = challenge._future_candidates
    _ORIGINAL_CHALLENGE_SUPPORT = challenge._model_support
    _ORIGINAL_CHALLENGE_HEALTH = challenge._lane_health

    def future_candidates(db, now, end):
        out = list(_ORIGINAL_CHALLENGE_FUTURE(db, now, end))
        rows = challenge._safe_rows(
            db,
            "football_cross_comp_candidates",
            "SELECT * FROM football_cross_comp_candidates WHERE status='OPEN'",
        )
        for raw in rows:
            row = dict(raw)
            if not challenge._in_window(row.get("commence_time"), now, end):
                continue
            out.append({
                "source": "XCOMP1_RESEARCH",
                "source_table": "football_cross_comp_candidates",
                "source_id": row.get("id"),
                "event_id": row.get("event_id"),
                "commence_time": row.get("commence_time"),
                "sport_key": row.get("sport_key"),
                "league": row.get("league"),
                "home_team": row.get("home_team"),
                "away_team": row.get("away_team"),
                "market_key": row.get("market_key"),
                "selection": row.get("selection"),
                "point": row.get("point"),
                "bookmaker_key": None,
                "bookmaker_title": None,
                "offered_odds": None,
                "odds_band": "",
                "fair_probability": row.get("fair_probability"),
                "fair_odds": row.get("fair_odds"),
                "edge_pct": None,
                "min_odds": None,
                "status": row.get("status"),
                "created_at": row.get("created_at"),
                "notes": "RESEARCH_ONLY_CROSS_COMP; no executable price attached",
            })
        out.sort(key=lambda r: (str(r.get("commence_time") or ""), str(r.get("source") or ""), str(r.get("selection") or "")))
        return out

    def model_support(db, now, end):
        out = list(_ORIGINAL_CHALLENGE_SUPPORT(db, now, end))
        rows = challenge._safe_rows(db, "football_cross_comp_predictions")
        for raw in rows:
            row = dict(raw)
            if not challenge._in_window(row.get("commence_time"), now, end):
                continue
            item = {"model": "XCOMP1", "source_table": "football_cross_comp_predictions"}
            item.update(row)
            out.append(item)
        out.sort(key=lambda r: (str(r.get("commence_time") or ""), str(r.get("model") or ""), str(r.get("event_id") or "")))
        return out

    def lane_health(db, candidates):
        base_candidates = [
            r for r in candidates if str(r.get("source") or "") != "XCOMP1_RESEARCH"
        ]
        out = list(_ORIGINAL_CHALLENGE_HEALTH(db, base_candidates))
        settled = challenge._safe_rows(
            db,
            "football_cross_comp_candidates",
            "SELECT * FROM football_cross_comp_candidates WHERE status='SETTLED'",
        )
        if not settled:
            return out
        out.append(challenge._health_record(
            "XCOMP1_RESEARCH", "football_cross_comp_candidates", "SOURCE", "XCOMP1_RESEARCH",
            settled, evidence_kind="CALIBRATION_ONLY",
        ))
        markets: Dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in settled:
            market = str(row.get("market_key") or "")
            if market:
                markets[market].append(dict(row))
        for market, group in sorted(markets.items()):
            out.append(challenge._health_record(
                "XCOMP1_RESEARCH", "football_cross_comp_candidates", "MARKET", market,
                group, market_key=market, evidence_kind="CALIBRATION_ONLY",
            ))
        return out

    future_candidates._football_coverage_expansion = True
    model_support._football_coverage_expansion = True
    lane_health._football_coverage_expansion = True
    challenge._future_candidates = future_candidates
    challenge._model_support = model_support
    challenge._lane_health = lane_health
    return True


def install() -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    fhg_added = _expand_fhg()
    pxg_added = _expand_pxg()
    worker_bridge = _install_worker_bridge()
    export_tables_added = _install_export_tables()
    challenge_bridge = _install_challenge_bridge()
    _INSTALLED = True
    state = {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "fhg_leagues_added": fhg_added,
        "pxg_mappings_added": pxg_added,
        "worker_bridge": worker_bridge,
        "challenge_bridge": challenge_bridge,
        "export_tables_added": export_tables_added,
        "xcomp_model": MODEL_VERSION,
        "xcomp_horizon_hours": HORIZON_HOURS,
        "xcomp_evidence": "FORWARD_RESEARCH_ONLY",
        "provider_calls_added_by_xcomp": 0,
        "betting_authority": False,
    }
    print(f"FOOTBALL_COVERAGE_EXPANSION_INSTALL {state}", flush=True)
    return state
