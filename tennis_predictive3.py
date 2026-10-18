from __future__ import annotations

"""TPRED3: chronological rich-data tennis challenger.

Research-only model.  TPRED3 uses only information available before a match:
provider match history, per-match serve/return statistics, dated rankings and
surface context.  TPRED1/TPRED2 remain untouched controls.

The first TPRED3 specification is intentionally fixed and regularised.  It is
not fitted to the current outcome sample and has no betting authority.
"""

from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from tennis_rich_context import fixture_context, match_provider_player
from tennis_rich_source import _norm_name, status as rich_source_status

MODEL = "TPRED3"
MODEL_VERSION = "TPRED3_RICH_CHRONO_V1"
FORWARD_HOURS = 168.0
MIN_REBUILD_MATCHES = 100
REBUILD_STATS_DELTA = 250


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(value: Any) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return None


def _clip(value: float, lo: float = 0.02, hi: float = 0.98) -> float:
    return min(hi, max(lo, float(value)))


def _sigmoid(z: float) -> float:
    if z >= 0:
        e = math.exp(-z)
        return 1.0 / (1.0 + e)
    e = math.exp(z)
    return e / (1.0 + e)


def _logit(p: float) -> float:
    p = _clip(p, 1e-8, 1.0 - 1e-8)
    return math.log(p / (1.0 - p))


def _elo_probability(r1: float, r2: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((float(r2) - float(r1)) / 400.0))


def _brier(p: float, y: int) -> float:
    return (float(p) - int(y)) ** 2


def _log_loss(p: float, y: int) -> float:
    p = _clip(p, 1e-9, 1.0 - 1e-9)
    return -(int(y) * math.log(p) + (1 - int(y)) * math.log(1.0 - p))


def _fair_odds(p: float) -> float:
    return 1.0 / _clip(p, 1e-8, 1.0 - 1e-8)


def _surface(value: Any) -> str:
    raw = str(value or "UNKNOWN").strip().lower().replace("_", " ")
    if "clay" in raw:
        return "CLAY"
    if "grass" in raw:
        return "GRASS"
    if "carpet" in raw:
        return "CARPET"
    if "indoor" in raw or raw in {"i.hard", "ihard", "i hard"}:
        return "INDOOR_HARD"
    if "hard" in raw:
        return "HARD"
    return "UNKNOWN"


def install_schema(db) -> Dict[str, Any]:
    id_col = "BIGSERIAL PRIMARY KEY" if getattr(db, "is_postgres", False) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ddl = (
        f"""
        CREATE TABLE IF NOT EXISTS tennis_predictive3_walkforward (
            id {id_col},
            model_version TEXT NOT NULL,
            tour TEXT NOT NULL,
            provider_match_id INTEGER NOT NULL,
            match_date TEXT NOT NULL,
            surface TEXT,
            player_one_id INTEGER NOT NULL,
            player_one TEXT NOT NULL,
            player_two_id INTEGER NOT NULL,
            player_two TEXT NOT NULL,
            predicted_p1 REAL NOT NULL,
            outcome_p1 INTEGER NOT NULL,
            brier REAL NOT NULL,
            log_loss REAL NOT NULL,
            market_p1 REAL,
            market_brier REAL,
            model_edge_vs_market_pct REAL,
            evidence_ready INTEGER NOT NULL DEFAULT 0,
            confidence_tier TEXT NOT NULL,
            feature_json TEXT NOT NULL,
            canonical_event_id TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(tour,provider_match_id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS tennis_predictive3_predictions (
            id {id_col},
            model_version TEXT NOT NULL,
            event_id TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            commence_time TEXT NOT NULL,
            tour TEXT NOT NULL,
            surface TEXT,
            provider_match_id INTEGER,
            selection TEXT NOT NULL,
            opponent TEXT NOT NULL,
            probability REAL NOT NULL,
            fair_odds REAL NOT NULL,
            market_probability REAL,
            market_fair_odds REAL,
            model_edge_vs_market_pct REAL,
            confidence_tier TEXT NOT NULL,
            feature_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            outcome INTEGER,
            brier REAL,
            log_loss REAL,
            settled_at TEXT,
            UNIQUE(event_id,selection)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tennis_predictive3_state (
            singleton_id INTEGER PRIMARY KEY,
            model_version TEXT NOT NULL,
            last_match_count INTEGER NOT NULL DEFAULT 0,
            last_stats_count INTEGER NOT NULL DEFAULT 0,
            last_ranking_count INTEGER NOT NULL DEFAULT 0,
            last_walkforward_at TEXT,
            last_maintenance_at TEXT
        )
        """,
    )
    for sql in ddl:
        db.execute(sql)
    db.execute(
        """
        INSERT INTO tennis_predictive3_state(singleton_id,model_version)
        VALUES(1,?)
        ON CONFLICT(singleton_id) DO UPDATE SET model_version=excluded.model_version
        """,
        (MODEL_VERSION,),
    )
    return {"installed": True, "model": MODEL_VERSION, "betting_authority": False}


def _ranking_index(db) -> Dict[Tuple[str, int], Tuple[List[str], List[Tuple[Optional[int], Optional[float]]]]]:
    rows = db.fetchall(
        """
        SELECT tour,provider_player_id,ranking_date,ranking,ranking_points
        FROM tennis_rich_rankings
        ORDER BY tour,provider_player_id,ranking_date
        """
    )
    tmp: Dict[Tuple[str, int], List[Tuple[str, Optional[int], Optional[float]]]] = defaultdict(list)
    for row in rows:
        pid = _as_int(row.get("provider_player_id"))
        if pid is None:
            continue
        tmp[(str(row.get("tour") or "").lower(), pid)].append(
            (str(row.get("ranking_date") or ""), _as_int(row.get("ranking")), _as_float(row.get("ranking_points")))
        )
    out = {}
    for key, values in tmp.items():
        out[key] = ([v[0] for v in values], [(v[1], v[2]) for v in values])
    return out


def _rank_asof(index, tour: str, player_id: int, at: datetime) -> Tuple[Optional[int], Optional[float]]:
    item = index.get((tour.lower(), int(player_id)))
    if not item:
        return None, None
    dates, values = item
    pos = bisect_right(dates, at.date().isoformat()) - 1
    if pos < 0:
        return None, None
    return values[pos]


def _ratio(num: Any, den: Any) -> Optional[float]:
    n, d = _as_float(num), _as_float(den)
    if n is None or d is None or d <= 0:
        return None
    return _clip(n / d, 0.0, 1.0)


def _pct(value: Any) -> Optional[float]:
    v = _as_float(value)
    if v is None:
        return None
    if v > 1.5:
        v /= 100.0
    return _clip(v, 0.0, 1.0)


def _player_match_metrics(stats: Mapping[str, Any]) -> Dict[str, Optional[float]]:
    first_won = _as_float(stats.get("winningOnFirstServe"))
    second_won = _as_float(stats.get("winningOnSecondServe"))
    service_points = _as_float(stats.get("firstServeOf"))
    service_won = None
    if first_won is not None and second_won is not None and service_points and service_points > 0:
        service_won = _clip((first_won + second_won) / service_points, 0.0, 1.0)
    if service_won is None:
        f1 = _pct(stats.get("winningOnFirstServePercentage"))
        f2 = _pct(stats.get("winningOnSecondServePercentage"))
        first_in = _pct(stats.get("firstServePercentage"))
        if f1 is not None and f2 is not None:
            w = first_in if first_in is not None else 0.60
            service_won = _clip(w * f1 + (1.0 - w) * f2, 0.0, 1.0)

    return_won = _ratio(stats.get("returnPtsWin"), stats.get("returnPtsWinOf"))
    if return_won is None:
        return_won = _pct(stats.get("returnPtsWinPercentage"))

    first_win = _ratio(stats.get("winningOnFirstServe"), stats.get("winningOnFirstServeOf"))
    if first_win is None:
        first_win = _pct(stats.get("winningOnFirstServePercentage"))
    second_win = _ratio(stats.get("winningOnSecondServe"), stats.get("winningOnSecondServeOf"))
    if second_win is None:
        second_win = _pct(stats.get("winningOnSecondServePercentage"))

    ace_rate = _ratio(stats.get("aces"), stats.get("firstServeOf"))
    df_rate = _ratio(stats.get("doubleFaults"), stats.get("firstServeOf"))
    bp_saved = _ratio(stats.get("breakPointSavedGm"), stats.get("breakPointFacedGm"))
    if bp_saved is None:
        bp_saved = _pct(stats.get("breakPointsSavedPercentage"))

    return {
        "service_points_won": service_won,
        "return_points_won": return_won,
        "first_serve_win": first_win,
        "second_serve_win": second_win,
        "ace_rate": ace_rate,
        "double_fault_rate": df_rate,
        "break_points_saved": bp_saved,
    }


def _stats_by_player(row: Mapping[str, Any]) -> Dict[int, Dict[str, Optional[float]]]:
    raw = row.get("stats_json")
    if not raw:
        return {}
    try:
        payload = json.loads(str(raw)) if isinstance(raw, str) else raw
    except Exception:
        return {}
    if not isinstance(payload, Mapping):
        return {}
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    if not isinstance(data, Mapping):
        return {}
    p1s = data.get("player1Stats") if isinstance(data.get("player1Stats"), Mapping) else None
    p2s = data.get("player2Stats") if isinstance(data.get("player2Stats"), Mapping) else None
    if p1s is None and isinstance(data.get("player1"), Mapping):
        candidate = data.get("player1")
        p1s = candidate.get("stats") if isinstance(candidate.get("stats"), Mapping) else candidate
    if p2s is None and isinstance(data.get("player2"), Mapping):
        candidate = data.get("player2")
        p2s = candidate.get("stats") if isinstance(candidate.get("stats"), Mapping) else candidate
    out = {}
    p1id, p2id = _as_int(row.get("player1_id")), _as_int(row.get("player2_id"))
    if p1id is not None and isinstance(p1s, Mapping):
        out[p1id] = _player_match_metrics(p1s)
    if p2id is not None and isinstance(p2s, Mapping):
        out[p2id] = _player_match_metrics(p2s)
    return out


def _new_state() -> Dict[str, Any]:
    return {"players": {}}


def _player(state: Dict[str, Any], player_id: int, name: str) -> Dict[str, Any]:
    players = state["players"]
    if player_id not in players:
        players[player_id] = {
            "id": player_id,
            "name": name,
            "elo": 1500.0,
            "surface_elo": {},
            "matches": [],
            "surface_matches": defaultdict(list),
            "stat_history": [],
            "last": None,
        }
    elif name:
        players[player_id]["name"] = name
    return players[player_id]


def _recent_form(player: Mapping[str, Any], n: int = 10) -> float:
    rows = list(player.get("matches") or [])[-n:]
    wins = sum(int(x[1]) for x in rows)
    return (wins + 2.0) / (len(rows) + 4.0)


def _surface_form(player: Mapping[str, Any], surface: str, n: int = 10) -> Tuple[float, int]:
    rows = list((player.get("surface_matches") or {}).get(surface, []))[-n:]
    wins = sum(int(x[1]) for x in rows)
    return (wins + 2.0) / (len(rows) + 4.0), len(rows)


def _matches_7d(player: Mapping[str, Any], at: datetime) -> int:
    cutoff = at - timedelta(days=7)
    return sum(1 for dt, _ in player.get("matches") or [] if cutoff <= dt < at)


def _rest_days(player: Mapping[str, Any], at: datetime) -> Optional[float]:
    last = player.get("last")
    if last is None:
        return None
    return max(0.0, (at - last).total_seconds() / 86400.0)


def _recent_stat_averages(player: Mapping[str, Any], surface: str, n: int = 10) -> Tuple[Dict[str, Optional[float]], int, int]:
    history = list(player.get("stat_history") or [])
    same_surface = [x for x in history if x[1] == surface]
    use = same_surface[-n:] if len(same_surface) >= 3 else history[-n:]
    keys = (
        "service_points_won", "return_points_won", "first_serve_win", "second_serve_win",
        "ace_rate", "double_fault_rate", "break_points_saved",
    )
    out: Dict[str, Optional[float]] = {}
    for key in keys:
        vals = [float(x[2][key]) for x in use if x[2].get(key) is not None]
        out[key] = (sum(vals) / len(vals)) if vals else None
    return out, len(use), len(same_surface)


def _features(
    state: Dict[str, Any], ranking_index, tour: str, at: datetime, surface: str,
    p1id: int, p1name: str, p2id: int, p2name: str,
) -> Dict[str, Any]:
    a, b = _player(state, p1id, p1name), _player(state, p2id, p2name)
    s1 = float(a.get("surface_elo", {}).get(surface, a["elo"]))
    s2 = float(b.get("surface_elo", {}).get(surface, b["elo"]))
    sf1, sn1 = _surface_form(a, surface)
    sf2, sn2 = _surface_form(b, surface)
    st1, stat_n1, stat_surface_n1 = _recent_stat_averages(a, surface)
    st2, stat_n2, stat_surface_n2 = _recent_stat_averages(b, surface)
    rank1, points1 = _rank_asof(ranking_index, tour, p1id, at)
    rank2, points2 = _rank_asof(ranking_index, tour, p2id, at)
    return {
        "surface": surface,
        "p1_elo": float(a["elo"]), "p2_elo": float(b["elo"]),
        "p1_surface_elo": s1, "p2_surface_elo": s2,
        "p1_surface_matches": sn1, "p2_surface_matches": sn2,
        "p1_form10": _recent_form(a), "p2_form10": _recent_form(b),
        "p1_surface_form10": sf1, "p2_surface_form10": sf2,
        "p1_matches_7d": _matches_7d(a, at), "p2_matches_7d": _matches_7d(b, at),
        "p1_rest_days": _rest_days(a, at), "p2_rest_days": _rest_days(b, at),
        "p1_prior_matches": len(a.get("matches") or []), "p2_prior_matches": len(b.get("matches") or []),
        "p1_ranking": rank1, "p2_ranking": rank2,
        "p1_ranking_points": points1, "p2_ranking_points": points2,
        "p1_stat_matches": stat_n1, "p2_stat_matches": stat_n2,
        "p1_surface_stat_matches": stat_surface_n1, "p2_surface_stat_matches": stat_surface_n2,
        "p1_stats": st1, "p2_stats": st2,
    }


def _predict_p1(f: Mapping[str, Any]) -> float:
    n_surface = min(int(f.get("p1_surface_matches") or 0), int(f.get("p2_surface_matches") or 0))
    surface_weight = min(0.65, n_surface / (n_surface + 8.0)) if n_surface > 0 else 0.0
    r1 = float(f["p1_elo"]) + surface_weight * (float(f["p1_surface_elo"]) - float(f["p1_elo"]))
    r2 = float(f["p2_elo"]) + surface_weight * (float(f["p2_surface_elo"]) - float(f["p2_elo"]))
    z = _logit(_elo_probability(r1, r2))

    # Small fixed corrections.  These are deliberately not fitted to the
    # observed TPRED3 outcomes; walk-forward evidence decides whether they help.
    p1pts, p2pts = _as_float(f.get("p1_ranking_points")), _as_float(f.get("p2_ranking_points"))
    if p1pts is not None and p2pts is not None and p1pts >= 0 and p2pts >= 0:
        z += 0.10 * math.log((p1pts + 100.0) / (p2pts + 100.0))
    else:
        rnk1, rnk2 = _as_int(f.get("p1_ranking")), _as_int(f.get("p2_ranking"))
        if rnk1 and rnk2:
            z += 0.08 * math.log((rnk2 + 20.0) / (rnk1 + 20.0))

    z += 0.35 * (float(f["p1_form10"]) - float(f["p2_form10"]))
    z += 0.45 * (float(f["p1_surface_form10"]) - float(f["p2_surface_form10"]))

    s1, s2 = f.get("p1_stats") or {}, f.get("p2_stats") or {}
    serve1, serve2 = _as_float(s1.get("service_points_won")), _as_float(s2.get("service_points_won"))
    ret1, ret2 = _as_float(s1.get("return_points_won")), _as_float(s2.get("return_points_won"))
    if None not in (serve1, serve2, ret1, ret2):
        quality1 = (serve1 + ret1) / 2.0
        quality2 = (serve2 + ret2) / 2.0
        z += 2.50 * (quality1 - quality2)

    z -= 0.05 * (int(f.get("p1_matches_7d") or 0) - int(f.get("p2_matches_7d") or 0))
    rest1, rest2 = _as_float(f.get("p1_rest_days")), _as_float(f.get("p2_rest_days"))
    if rest1 is not None and rest1 < 1.0:
        z -= 0.06
    if rest2 is not None and rest2 < 1.0:
        z += 0.06
    return _clip(_sigmoid(z), 0.05, 0.95)


def _confidence(f: Mapping[str, Any]) -> str:
    prior = min(int(f.get("p1_prior_matches") or 0), int(f.get("p2_prior_matches") or 0))
    stats = min(int(f.get("p1_stat_matches") or 0), int(f.get("p2_stat_matches") or 0))
    ranks = f.get("p1_ranking") is not None and f.get("p2_ranking") is not None
    surface = str(f.get("surface") or "UNKNOWN") != "UNKNOWN"
    if prior >= 20 and stats >= 8 and ranks and surface:
        return "A"
    if prior >= 10 and stats >= 5 and ranks and surface:
        return "B"
    if prior >= 5 and stats >= 3 and surface:
        return "C"
    return "WATCH"


def _evidence_ready(f: Mapping[str, Any]) -> int:
    return int(_confidence(f) in {"A", "B", "C"})


def _update_state(state: Dict[str, Any], row: Mapping[str, Any], p1id: int, p1name: str, p2id: int, p2name: str, y: int) -> None:
    at = _parse(row["match_date"])
    surface = _surface(row.get("surface"))
    a, b = _player(state, p1id, p1name), _player(state, p2id, p2name)
    expected = _elo_probability(float(a["elo"]), float(b["elo"]))
    k = 32.0 if min(len(a["matches"]), len(b["matches"])) < 20 else 24.0
    delta = k * (int(y) - expected)
    a["elo"] = float(a["elo"]) + delta
    b["elo"] = float(b["elo"]) - delta

    sa = float(a["surface_elo"].get(surface, a["elo"] - delta))
    sb = float(b["surface_elo"].get(surface, b["elo"] + delta))
    sexp = _elo_probability(sa, sb)
    sk = 28.0 if min(len(a["surface_matches"][surface]), len(b["surface_matches"][surface])) < 12 else 20.0
    sdelta = sk * (int(y) - sexp)
    a["surface_elo"][surface] = sa + sdelta
    b["surface_elo"][surface] = sb - sdelta

    a["matches"].append((at, int(y)))
    b["matches"].append((at, 1 - int(y)))
    a["surface_matches"][surface].append((at, int(y)))
    b["surface_matches"][surface].append((at, 1 - int(y)))
    a["last"], b["last"] = at, at

    metrics = _stats_by_player(row)
    if p1id in metrics and any(v is not None for v in metrics[p1id].values()):
        a["stat_history"].append((at, surface, metrics[p1id]))
    if p2id in metrics and any(v is not None for v in metrics[p2id].values()):
        b["stat_history"].append((at, surface, metrics[p2id]))


def _provider_rows(db, before: Optional[datetime] = None) -> List[Dict[str, Any]]:
    params: Tuple[Any, ...] = ()
    where = ""
    if before is not None:
        where = " AND match_date<?"
        params = (_iso(before),)
    rows = db.fetchall(
        f"""
        SELECT * FROM tennis_rich_matches
        WHERE match_date IS NOT NULL AND player1_id IS NOT NULL AND player2_id IS NOT NULL
          AND winner_id IS NOT NULL
          AND COALESCE(result_type,'completed') NOT IN ('walkover','w/o','retired')
          {where}
        ORDER BY match_date,provider_match_id
        """,
        params,
    )
    return [dict(r) for r in rows]


def _canonical_index(db) -> Dict[Tuple[str, str, str], List[Dict[str, Any]]]:
    try:
        rows = db.fetchall(
            """
            SELECT event_id,tour,commence_time,player_one,player_two
            FROM tennis_events ORDER BY commence_time,event_id
            """
        )
    except Exception:
        return {}
    out: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        try:
            day = _parse(row["commence_time"]).date().isoformat()
        except Exception:
            continue
        a, b = sorted((_norm_name(row.get("player_one")), _norm_name(row.get("player_two"))))
        tour = str(row.get("tour") or "").lower()
        if tour not in {"atp", "wta"}:
            sport = str(row.get("tour") or "").lower()
            tour = "wta" if "wta" in sport else "atp" if "atp" in sport else tour
        out[(tour, day, f"{a}|{b}")].append(dict(row))
    return out


def _canonical_match(index, tour: str, at: datetime, a_name: str, b_name: str) -> Optional[Dict[str, Any]]:
    a, b = sorted((_norm_name(a_name), _norm_name(b_name)))
    pair = f"{a}|{b}"
    for offset in (0, -1, 1):
        day = (at.date() + timedelta(days=offset)).isoformat()
        rows = index.get((tour.lower(), day, pair)) or []
        if rows:
            return rows[0]
    return None


def _market_probability(db, event: Mapping[str, Any], provider_selection: str, at: datetime) -> Optional[float]:
    pnorm = _norm_name(provider_selection)
    selection = None
    if _norm_name(event.get("player_one")) == pnorm:
        selection = event.get("player_one")
    elif _norm_name(event.get("player_two")) == pnorm:
        selection = event.get("player_two")
    if not selection:
        return None
    try:
        row = db.fetchone(
            """
            SELECT fair_probability FROM tennis_consensus_snapshots
            WHERE event_id=? AND selection=? AND captured_at<=?
            ORDER BY captured_at DESC,id DESC LIMIT 1
            """,
            (event["event_id"], selection, _iso(at)),
        )
    except Exception:
        row = None
    return _as_float(row.get("fair_probability")) if row else None


def _oriented(row: Mapping[str, Any]) -> Optional[Tuple[int, str, int, str, int]]:
    a_id, b_id = _as_int(row.get("player1_id")), _as_int(row.get("player2_id"))
    winner = _as_int(row.get("winner_id"))
    if a_id is None or b_id is None or winner not in {a_id, b_id}:
        return None
    a_name, b_name = str(row.get("player1_name") or ""), str(row.get("player2_name") or "")
    # Provider results list the winner first.  Sort by stable provider ID so the
    # prediction orientation cannot leak the outcome label.
    if a_id <= b_id:
        return a_id, a_name, b_id, b_name, int(winner == a_id)
    return b_id, b_name, a_id, a_name, int(winner == b_id)


def rebuild_walkforward(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    install_schema(db)
    now = now or _now()
    db.execute("DELETE FROM tennis_predictive3_walkforward")
    ranking_index = _ranking_index(db)
    canonical = _canonical_index(db)
    state = _new_state()
    created = 0
    evidence = 0
    market_comparable = 0
    skipped = 0
    for row in _provider_rows(db):
        oriented = _oriented(row)
        if not oriented:
            skipped += 1
            continue
        p1id, p1name, p2id, p2name, y = oriented
        at = _parse(row["match_date"])
        surface = _surface(row.get("surface"))
        feat = _features(state, ranking_index, str(row.get("tour") or ""), at, surface, p1id, p1name, p2id, p2name)
        p = _predict_p1(feat)
        tier = _confidence(feat)
        ready = _evidence_ready(feat)
        event = _canonical_match(canonical, str(row.get("tour") or ""), at, p1name, p2name)
        market_p = _market_probability(db, event, p1name, at) if event else None
        db.execute(
            """
            INSERT INTO tennis_predictive3_walkforward(
                model_version,tour,provider_match_id,match_date,surface,
                player_one_id,player_one,player_two_id,player_two,predicted_p1,outcome_p1,
                brier,log_loss,market_p1,market_brier,model_edge_vs_market_pct,
                evidence_ready,confidence_tier,feature_json,canonical_event_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                MODEL_VERSION,str(row.get("tour") or "").upper(),int(row["provider_match_id"]),row["match_date"],surface,
                p1id,p1name,p2id,p2name,p,y,_brier(p,y),_log_loss(p,y),market_p,
                _brier(market_p,y) if market_p is not None else None,
                ((p-market_p)*100.0) if market_p is not None else None,ready,tier,
                json.dumps(feat,separators=(",",":")),event.get("event_id") if event else None,_iso(now),
            ),
        )
        created += 1
        evidence += ready
        market_comparable += int(market_p is not None)
        _update_state(state, row, p1id, p1name, p2id, p2name, y)

    counts = _source_counts(db)
    db.execute(
        """
        UPDATE tennis_predictive3_state SET
            model_version=?,last_match_count=?,last_stats_count=?,last_ranking_count=?,last_walkforward_at=?
        WHERE singleton_id=1
        """,
        (MODEL_VERSION, counts["matches"], counts["stats"], counts["rankings"], _iso(now)),
    )
    return {
        "created": created,
        "evidence_ready": evidence,
        "market_comparable": market_comparable,
        "skipped": skipped,
        "model_version": MODEL_VERSION,
    }


def _state_from_history(db, before: datetime) -> Tuple[Dict[str, Any], Any]:
    state = _new_state()
    ranking_index = _ranking_index(db)
    for row in _provider_rows(db, before=before):
        oriented = _oriented(row)
        if not oriented:
            continue
        p1id, p1name, p2id, p2name, y = oriented
        _update_state(state, row, p1id, p1name, p2id, p2name, y)
    return state, ranking_index


def _event_tour(event: Mapping[str, Any]) -> Optional[str]:
    tour = str(event.get("tour") or "").lower()
    sport = str(event.get("sport_key") or "").lower()
    if tour in {"atp", "wta"}:
        return tour
    if "wta" in tour or "wta" in sport:
        return "wta"
    if "atp" in tour or "atp" in sport:
        return "atp"
    return None


def create_forward_predictions(db, now: Optional[datetime] = None) -> Dict[str, int]:
    install_schema(db)
    now = now or _now()
    cutoff = now + timedelta(hours=FORWARD_HOURS)
    try:
        events = db.fetchall(
            """
            SELECT * FROM tennis_events
            WHERE status='UPCOMING' AND commence_time>? AND commence_time<=?
            ORDER BY commence_time,event_id
            """,
            (_iso(now), _iso(cutoff)),
        )
    except Exception:
        return {"created": 0, "eligible": 0, "unmatched": 0}

    state, ranking_index = _state_from_history(db, now)
    created = 0
    eligible = 0
    unmatched = 0
    for event in events:
        tour = _event_tour(event)
        if tour is None:
            continue
        p1name, p2name = str(event.get("player_one") or ""), str(event.get("player_two") or "")
        p1map, p2map = match_provider_player(db, tour, p1name), match_provider_player(db, tour, p2name)
        if not p1map or not p2map:
            unmatched += 1
            continue
        p1id, p2id = _as_int(p1map.get("provider_player_id")), _as_int(p2map.get("provider_player_id"))
        if p1id is None or p2id is None:
            unmatched += 1
            continue
        at = _parse(event["commence_time"])
        fixture = fixture_context(db, tour, p1name, p2name, event.get("commence_time")) or {}
        surface = _surface(fixture.get("surface"))
        feat = _features(state, ranking_index, tour, at, surface, p1id, p1name, p2id, p2name)
        p1prob = _predict_p1(feat)
        tier = _confidence(feat)
        eligible += int(tier != "WATCH")
        provider_mid = _as_int(fixture.get("provider_match_id"))

        def insert(selection: str, opponent: str, probability: float) -> None:
            nonlocal created
            market = None
            try:
                m = db.fetchone(
                    """
                    SELECT fair_probability FROM tennis_consensus_snapshots
                    WHERE event_id=? AND selection=? AND captured_at<=?
                    ORDER BY captured_at DESC,id DESC LIMIT 1
                    """,
                    (event["event_id"], selection, _iso(now)),
                )
                market = _as_float(m.get("fair_probability")) if m else None
            except Exception:
                market = None
            changed = db.execute(
                """
                INSERT INTO tennis_predictive3_predictions(
                    model_version,event_id,generated_at,commence_time,tour,surface,provider_match_id,
                    selection,opponent,probability,fair_odds,market_probability,market_fair_odds,
                    model_edge_vs_market_pct,confidence_tier,feature_json,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'OPEN')
                ON CONFLICT(event_id,selection) DO NOTHING
                """,
                (
                    MODEL_VERSION,event["event_id"],_iso(now),event["commence_time"],tour.upper(),surface,provider_mid,
                    selection,opponent,probability,_fair_odds(probability),market,
                    _fair_odds(market) if market is not None else None,
                    ((probability-market)*100.0) if market is not None else None,tier,
                    json.dumps(feat,separators=(",",":")),
                ),
            )
            created += int(bool(changed))

        insert(p1name, p2name, p1prob)
        insert(p2name, p1name, 1.0 - p1prob)
    return {"created": created, "eligible": eligible, "unmatched": unmatched}


def settle_forward_predictions(db) -> int:
    try:
        rows = db.fetchall(
            """
            SELECT p.id,p.selection,p.probability,r.winner,r.completed_at,r.fetched_at
            FROM tennis_predictive3_predictions p
            JOIN tennis_results r ON r.event_id=p.event_id
            WHERE p.status='OPEN' ORDER BY p.id
            """
        )
    except Exception:
        return 0
    changed = 0
    for row in rows:
        y = int(str(row.get("selection")) == str(row.get("winner")))
        db.execute(
            """
            UPDATE tennis_predictive3_predictions SET
                status='SETTLED',outcome=?,brier=?,log_loss=?,settled_at=?
            WHERE id=?
            """,
            (y,_brier(float(row["probability"]),y),_log_loss(float(row["probability"]),y),
             row.get("completed_at") or row.get("fetched_at") or _iso(_now()),row["id"]),
        )
        changed += 1
    return changed


def _source_counts(db) -> Dict[str, int]:
    def count(sql: str) -> int:
        try:
            return int((db.fetchone(sql) or {}).get("n") or 0)
        except Exception:
            return 0
    return {
        "matches": count("SELECT COUNT(*) AS n FROM tennis_rich_matches"),
        "stats": count("SELECT COUNT(*) AS n FROM tennis_rich_matches WHERE stats_json IS NOT NULL"),
        "rankings": count("SELECT COUNT(*) AS n FROM tennis_rich_rankings"),
        "players": count("SELECT COUNT(*) AS n FROM tennis_rich_players"),
        "fixtures": count("SELECT COUNT(*) AS n FROM tennis_rich_fixtures"),
    }


def run_maintenance(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    install_schema(db)
    now = now or _now()
    counts = _source_counts(db)
    state = db.fetchone("SELECT * FROM tennis_predictive3_state WHERE singleton_id=1") or {}
    ready = counts["matches"] >= MIN_REBUILD_MATCHES
    rebuild = False
    if ready:
        rebuild = (
            not state.get("last_walkforward_at")
            or counts["matches"] != int(state.get("last_match_count") or 0)
            or counts["stats"] - int(state.get("last_stats_count") or 0) >= REBUILD_STATS_DELTA
            or counts["rankings"] - int(state.get("last_ranking_count") or 0) >= 1000
        )
    walkforward = {"rebuilt": False}
    if rebuild:
        walkforward = rebuild_walkforward(db, now)
        walkforward["rebuilt"] = True
    forward = create_forward_predictions(db, now) if ready else {"created": 0, "eligible": 0, "unmatched": 0}
    settled = settle_forward_predictions(db)
    db.execute("UPDATE tennis_predictive3_state SET last_maintenance_at=? WHERE singleton_id=1", (_iso(now),))
    return {
        "ok": True,
        "model_version": MODEL_VERSION,
        "source_ready": ready,
        "source_counts": counts,
        "walkforward": walkforward,
        "forward": forward,
        "forward_settled": settled,
        "provider_requests": 0,
        "betting_authority": False,
    }


def _metrics(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    rows = list(rows)
    if not rows:
        return {"samples": 0, "accuracy_pct": None, "brier": None, "log_loss": None, "market_samples": 0, "market_brier": None, "brier_improvement_vs_market_pct": None}
    acc = sum(1 for r in rows if (float(r["predicted_p1"]) >= 0.5) == bool(int(r["outcome_p1"])))
    brier = sum(float(r["brier"]) for r in rows) / len(rows)
    ll = sum(float(r["log_loss"]) for r in rows) / len(rows)
    comparable = [r for r in rows if r.get("market_brier") is not None]
    market_brier = sum(float(r["market_brier"]) for r in comparable) / len(comparable) if comparable else None
    model_on_comparable = None
    improvement = None
    if comparable:
        model_on_comparable = sum(float(r["brier"]) for r in comparable) / len(comparable)
        if market_brier and market_brier > 0:
            improvement = (market_brier - model_on_comparable) / market_brier * 100.0
    return {
        "samples": len(rows),
        "accuracy_pct": acc / len(rows) * 100.0,
        "brier": brier,
        "log_loss": ll,
        "market_samples": len(comparable),
        "market_brier": market_brier,
        "model_brier_on_market_samples": model_on_comparable,
        "brier_improvement_vs_market_pct": improvement,
    }


def scoreboard(db) -> Dict[str, Any]:
    install_schema(db)
    counts = _source_counts(db)
    try:
        rows = db.fetchall("SELECT * FROM tennis_predictive3_walkforward ORDER BY match_date,id")
        predictions = db.fetchall("SELECT * FROM tennis_predictive3_predictions ORDER BY generated_at,id")
    except Exception:
        rows, predictions = [], []
    ready_rows = [r for r in rows if int(r.get("evidence_ready") or 0) == 1]
    by_tour = {}
    for tour in ("ATP", "WTA"):
        subset = [r for r in ready_rows if str(r.get("tour") or "").upper() == tour]
        if subset:
            by_tour[tour] = _metrics(subset)
    by_surface = {}
    for surface in ("HARD","INDOOR_HARD","CLAY","GRASS","CARPET"):
        subset = [r for r in ready_rows if str(r.get("surface") or "") == surface]
        if subset:
            by_surface[surface] = _metrics(subset)
    state = db.fetchone("SELECT * FROM tennis_predictive3_state WHERE singleton_id=1") or {}
    rich = rich_source_status(db)
    stats_coverage = counts["stats"] / counts["matches"] * 100.0 if counts["matches"] else 0.0
    return {
        "model": MODEL,
        "model_version": MODEL_VERSION,
        "source_ready": counts["matches"] >= MIN_REBUILD_MATCHES,
        "source_counts": counts,
        "stats_coverage_pct": stats_coverage,
        "all_walkforward": _metrics(rows),
        "evidence_ready_walkforward": _metrics(ready_rows),
        "by_tour": by_tour,
        "by_surface": by_surface,
        "forward_open": sum(1 for r in predictions if r.get("status") == "OPEN"),
        "forward_settled": sum(1 for r in predictions if r.get("status") == "SETTLED"),
        "forward_evidence_ready_open": sum(1 for r in predictions if r.get("status") == "OPEN" and r.get("confidence_tier") in {"A","B","C"}),
        "last_walkforward_at": state.get("last_walkforward_at"),
        "last_maintenance_at": state.get("last_maintenance_at"),
        "rich_source": rich,
        "betting_authority": False,
    }
