from __future__ import annotations

"""TPRED3 chronological rich-feature tennis challenger.

TPRED3 is deliberately isolated from TPRED1/TPRED2. It consumes only cached
RapidAPI tennis data and existing stored market snapshots, predicts each
historical match before updating player/model state, and has no betting or
execution authority.

The model uses an Elo probability as a structural prior plus a small online
logistic correction. Correction weights start at zero and are updated only
after each historical outcome, in chronological order. This avoids fitting a
single static coefficient set to the full realised sample.
"""

from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
import bisect
import json
import math
import re
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from tennis_rich_context import fixture_context
from tennis_rich_source import _norm_name

MODEL = "TPRED3"
MODEL_VERSION = "TPRED3_RICH_PREQUENTIAL_V1"
FORWARD_HOURS = 168.0
MIN_HISTORY_MATCHES = 10
MIN_SURFACE_MATCHES = 3
MIN_RICH_MATCHES_TO_BUILD = 200
LEARNING_RATE = 0.035
L2 = 0.002

FEATURE_NAMES = (
    "rank_signal",
    "points_signal",
    "form10_diff",
    "surface_form_diff",
    "serve_diff",
    "return_diff",
    "workload_diff",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _clip(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, float(value)))


def _sigmoid(z: float) -> float:
    if z >= 0:
        e = math.exp(-z)
        return 1.0 / (1.0 + e)
    e = math.exp(z)
    return e / (1.0 + e)


def _logit(p: float) -> float:
    p = _clip(float(p), 1e-6, 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def _elo_p(r1: float, r2: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((float(r2) - float(r1)) / 400.0))


def _brier(p: float, y: int) -> float:
    return (float(p) - int(y)) ** 2


def _log_loss(p: float, y: int) -> float:
    p = _clip(float(p), 1e-9, 1.0 - 1e-9)
    return -(int(y) * math.log(p) + (1 - int(y)) * math.log(1.0 - p))


def _fair_odds(p: float) -> float:
    return 1.0 / _clip(float(p), 1e-6, 1.0 - 1e-6)


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    candidates = [text, text.replace("Z", "+00:00")]
    for candidate in candidates:
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def _pct(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = value.replace("%", "").strip()
        x = float(value)
    except Exception:
        return None
    if x > 1.5:
        x /= 100.0
    return _clip(x, 0.0, 1.0)


def install_schema(db) -> Dict[str, Any]:
    id_col = "BIGSERIAL PRIMARY KEY" if getattr(db, "is_postgres", False) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ddl = (
        """
        CREATE TABLE IF NOT EXISTS tennis_predictive3_state (
            singleton_id INTEGER PRIMARY KEY,
            model_version TEXT NOT NULL,
            last_rich_match_count INTEGER NOT NULL DEFAULT 0,
            last_ranking_count INTEGER NOT NULL DEFAULT 0,
            last_walkforward_at TEXT,
            last_maintenance_at TEXT,
            status TEXT NOT NULL DEFAULT 'BACKFILLING',
            model_weights_json TEXT
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS tennis_predictive3_walkforward (
            id {id_col},
            model_name TEXT NOT NULL,
            model_version TEXT NOT NULL,
            rich_match_id INTEGER NOT NULL,
            canonical_event_id TEXT,
            commence_time TEXT NOT NULL,
            tour TEXT NOT NULL,
            surface TEXT,
            tournament_title TEXT,
            player_one TEXT NOT NULL,
            player_two TEXT NOT NULL,
            predicted_p1 REAL NOT NULL,
            market_p1 REAL,
            outcome_p1 INTEGER NOT NULL,
            brier REAL NOT NULL,
            log_loss REAL NOT NULL,
            market_brier REAL,
            evidence_ready INTEGER NOT NULL DEFAULT 0,
            feature_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(model_version,tour,rich_match_id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS tennis_predictive3_predictions (
            id {id_col},
            model_name TEXT NOT NULL,
            model_version TEXT NOT NULL,
            event_id TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            commence_time TEXT NOT NULL,
            tour TEXT NOT NULL,
            surface TEXT,
            tournament_title TEXT,
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
            UNIQUE(model_version,event_id,selection)
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


def _new_player() -> Dict[str, Any]:
    return {
        "elo": 1500.0,
        "surface_elo": defaultdict(lambda: 1500.0),
        "matches": deque(maxlen=40),
        "surface_matches": defaultdict(lambda: deque(maxlen=30)),
        "serve": deque(maxlen=12),
        "return": deque(maxlen=12),
        "last": None,
        "count": 0,
    }


def _new_model_state() -> Dict[str, Any]:
    return {
        "players": defaultdict(_new_player),
        "weights": {name: 0.0 for name in FEATURE_NAMES},
        "updates": 0,
    }


def _mean(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _form(records: Sequence[Tuple[datetime, int]], n: int = 10) -> float:
    rows = list(records)[-n:]
    wins = sum(int(x[1]) for x in rows)
    return (wins + 2.0) / (len(rows) + 4.0)


def _workload(player: Mapping[str, Any], at: datetime) -> int:
    cutoff = at - timedelta(days=7)
    return sum(1 for dt, _ in player.get("matches") or [] if cutoff <= dt < at)


def _rest_days(player: Mapping[str, Any], at: datetime) -> Optional[float]:
    last = player.get("last")
    if not isinstance(last, datetime):
        return None
    return max(0.0, (at - last).total_seconds() / 86400.0)


def _metric_key(text: Any) -> Optional[str]:
    key = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()
    aliases = (
        ("first serve points won", "first_serve_won"),
        ("1st serve points won", "first_serve_won"),
        ("first serve won", "first_serve_won"),
        ("second serve points won", "second_serve_won"),
        ("2nd serve points won", "second_serve_won"),
        ("second serve won", "second_serve_won"),
        ("serve points won", "serve_points_won"),
        ("service points won", "serve_points_won"),
        ("return points won", "return_points_won"),
        ("receiving points won", "return_points_won"),
        ("break points saved", "break_points_saved"),
        ("double faults", "double_faults"),
        ("double fault", "double_faults"),
        ("aces", "aces"),
        ("ace", "aces"),
    )
    for needle, target in aliases:
        if needle in key:
            return target
    return None


def _extract_stats(payload_text: Any) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Best-effort parser for provider match stats, tolerant of schema variants."""
    if not payload_text:
        return {}, {}
    try:
        obj = json.loads(str(payload_text)) if isinstance(payload_text, str) else payload_text
    except Exception:
        return {}, {}
    out = [dict(), dict()]

    def assign(side: int, metric: Optional[str], value: Any) -> None:
        if metric is None:
            return
        pct = _pct(value)
        if pct is not None and metric in {
            "first_serve_won", "second_serve_won", "serve_points_won",
            "return_points_won", "break_points_saved",
        }:
            out[side][metric] = pct
            return
        try:
            out[side][metric] = float(str(value).replace("%", "").strip())
        except Exception:
            pass

    def walk(node: Any, side_hint: Optional[int] = None) -> None:
        if isinstance(node, Mapping):
            # Common shape: {name:'Aces', player1: 5, player2: 3}
            label = node.get("name") or node.get("label") or node.get("stat") or node.get("type") or node.get("key")
            metric = _metric_key(label)
            side_keys = (
                (0, ("player1", "player_1", "p1", "home", "value1", "value_1")),
                (1, ("player2", "player_2", "p2", "away", "value2", "value_2")),
            )
            if metric:
                for side, keys in side_keys:
                    for key in keys:
                        if key in node and not isinstance(node.get(key), (Mapping, list)):
                            assign(side, metric, node.get(key))
                            break
            for key, value in node.items():
                lk = str(key).lower().replace("_", "")
                child_side = side_hint
                if lk in {"player1", "p1", "home", "playerone"}:
                    child_side = 0
                elif lk in {"player2", "p2", "away", "playertwo"}:
                    child_side = 1
                direct_metric = _metric_key(key)
                if child_side is not None and direct_metric and not isinstance(value, (Mapping, list)):
                    assign(child_side, direct_metric, value)
                walk(value, child_side)
        elif isinstance(node, list):
            for item in node:
                walk(item, side_hint)

    walk(obj)
    return out[0], out[1]


def _serve_summary(stats: Mapping[str, float]) -> Optional[float]:
    vals = [stats.get("serve_points_won"), stats.get("first_serve_won"), stats.get("second_serve_won")]
    return _mean(vals)


def _return_summary(stats: Mapping[str, float]) -> Optional[float]:
    return _mean([stats.get("return_points_won")])


def _load_rankings(db) -> Dict[Tuple[str, int], List[Tuple[date, Optional[int], Optional[float]]]]:
    rows = db.fetchall(
        """
        SELECT ranking_date,tour,provider_player_id,ranking,ranking_points
        FROM tennis_rich_rankings ORDER BY tour,provider_player_id,ranking_date
        """
    )
    out: Dict[Tuple[str, int], List[Tuple[date, Optional[int], Optional[float]]]] = defaultdict(list)
    for row in rows:
        try:
            d = date.fromisoformat(str(row.get("ranking_date"))[:10])
            pid = int(row.get("provider_player_id"))
        except Exception:
            continue
        rank = int(row["ranking"]) if row.get("ranking") is not None else None
        points = float(row["ranking_points"]) if row.get("ranking_points") is not None else None
        out[(str(row.get("tour") or "").lower(), pid)].append((d, rank, points))
    return out


def _ranking_at(index: Mapping[Tuple[str, int], Sequence[Tuple[date, Optional[int], Optional[float]]]], tour: str, pid: int, at: datetime) -> Tuple[Optional[int], Optional[float]]:
    rows = list(index.get((tour.lower(), int(pid))) or [])
    if not rows:
        return None, None
    dates = [x[0] for x in rows]
    pos = bisect.bisect_right(dates, at.date()) - 1
    if pos < 0:
        return None, None
    _, rank, points = rows[pos]
    return rank, points


def _rank_signal(r1: Optional[int], r2: Optional[int]) -> float:
    if not r1 or not r2:
        return 0.0
    return _clip(math.tanh(math.log((float(r2) + 5.0) / (float(r1) + 5.0))), -1.0, 1.0)


def _points_signal(p1: Optional[float], p2: Optional[float]) -> float:
    if p1 is None or p2 is None:
        return 0.0
    return _clip(math.tanh(math.log((float(p1) + 250.0) / (float(p2) + 250.0))), -1.0, 1.0)


def _base_probability(a: Mapping[str, Any], b: Mapping[str, Any], surface: Optional[str]) -> Tuple[float, float, float]:
    overall = _elo_p(float(a["elo"]), float(b["elo"]))
    surf = overall
    use_surface = False
    if surface:
        ar = list((a.get("surface_matches") or {}).get(surface) or [])
        br = list((b.get("surface_matches") or {}).get(surface) or [])
        if min(len(ar), len(br)) >= MIN_SURFACE_MATCHES:
            surf = _elo_p(float(a["surface_elo"][surface]), float(b["surface_elo"][surface]))
            use_surface = True
    # Fixed structural blend; rich correction is learned prequentially.
    z = 0.60 * _logit(overall) + (0.40 * _logit(surf) if use_surface else 0.40 * _logit(overall))
    return _clip(_sigmoid(z), 0.03, 0.97), overall, surf


def _features(state: Mapping[str, Any], rankings, tour: str, p1id: int, p2id: int, at: datetime, surface: Optional[str]) -> Dict[str, Any]:
    a = state["players"][p1id]
    b = state["players"][p2id]
    r1, pts1 = _ranking_at(rankings, tour, p1id, at)
    r2, pts2 = _ranking_at(rankings, tour, p2id, at)
    sform1 = _form(list(a["surface_matches"].get(surface) or [])) if surface else 0.5
    sform2 = _form(list(b["surface_matches"].get(surface) or [])) if surface else 0.5
    serve1, serve2 = _mean(a["serve"]), _mean(b["serve"])
    ret1, ret2 = _mean(a["return"]), _mean(b["return"])
    base, overall_p, surface_p = _base_probability(a, b, surface)
    workload1, workload2 = _workload(a, at), _workload(b, at)
    vector = {
        "rank_signal": _rank_signal(r1, r2),
        "points_signal": _points_signal(pts1, pts2),
        "form10_diff": _clip(_form(a["matches"]) - _form(b["matches"]), -1.0, 1.0),
        "surface_form_diff": _clip(sform1 - sform2, -1.0, 1.0),
        "serve_diff": _clip((serve1 or 0.5) - (serve2 or 0.5), -0.5, 0.5) * 2.0 if serve1 is not None and serve2 is not None else 0.0,
        "return_diff": _clip((ret1 or 0.5) - (ret2 or 0.5), -0.5, 0.5) * 2.0 if ret1 is not None and ret2 is not None else 0.0,
        "workload_diff": _clip((workload2 - workload1) / 5.0, -1.0, 1.0),
    }
    return {
        **vector,
        "base_probability": base,
        "overall_elo_probability": overall_p,
        "surface_elo_probability": surface_p,
        "p1_elo": float(a["elo"]),
        "p2_elo": float(b["elo"]),
        "p1_surface_elo": float(a["surface_elo"][surface]) if surface else None,
        "p2_surface_elo": float(b["surface_elo"][surface]) if surface else None,
        "p1_prior_matches": int(a["count"]),
        "p2_prior_matches": int(b["count"]),
        "p1_surface_matches": len(list(a["surface_matches"].get(surface) or [])) if surface else 0,
        "p2_surface_matches": len(list(b["surface_matches"].get(surface) or [])) if surface else 0,
        "p1_rank": r1,
        "p2_rank": r2,
        "p1_ranking_points": pts1,
        "p2_ranking_points": pts2,
        "p1_serve": serve1,
        "p2_serve": serve2,
        "p1_return": ret1,
        "p2_return": ret2,
        "p1_workload_7d": workload1,
        "p2_workload_7d": workload2,
        "p1_rest_days": _rest_days(a, at),
        "p2_rest_days": _rest_days(b, at),
    }


def _predict(state: Mapping[str, Any], feat: Mapping[str, Any]) -> float:
    z = _logit(float(feat["base_probability"]))
    weights = state["weights"]
    for name in FEATURE_NAMES:
        z += float(weights.get(name) or 0.0) * float(feat.get(name) or 0.0)
    return _clip(_sigmoid(z), 0.03, 0.97)


def _learn(state: Dict[str, Any], feat: Mapping[str, Any], p: float, y: int) -> None:
    state["updates"] += 1
    eta = LEARNING_RATE / math.sqrt(1.0 + state["updates"] / 250.0)
    error = float(y) - float(p)
    for name in FEATURE_NAMES:
        old = float(state["weights"].get(name) or 0.0)
        x = float(feat.get(name) or 0.0)
        state["weights"][name] = old + eta * (error * x - L2 * old)


def _update_player_state(state: Dict[str, Any], p1id: int, p2id: int, at: datetime, surface: Optional[str], y: int, stats1: Mapping[str, float], stats2: Mapping[str, float]) -> None:
    a = state["players"][p1id]
    b = state["players"][p2id]
    expected = _elo_p(a["elo"], b["elo"])
    k = 32.0 if min(a["count"], b["count"]) < 20 else 24.0
    delta = k * (int(y) - expected)
    a["elo"] += delta
    b["elo"] -= delta
    if surface:
        sexpected = _elo_p(a["surface_elo"][surface], b["surface_elo"][surface])
        sk = 28.0 if min(len(a["surface_matches"][surface]), len(b["surface_matches"][surface])) < 12 else 20.0
        sdelta = sk * (int(y) - sexpected)
        a["surface_elo"][surface] += sdelta
        b["surface_elo"][surface] -= sdelta
        a["surface_matches"][surface].append((at, int(y)))
        b["surface_matches"][surface].append((at, 1 - int(y)))
    a["matches"].append((at, int(y)))
    b["matches"].append((at, 1 - int(y)))
    a["last"] = at
    b["last"] = at
    a["count"] += 1
    b["count"] += 1
    sa, sb = _serve_summary(stats1), _serve_summary(stats2)
    ra, rb = _return_summary(stats1), _return_summary(stats2)
    if sa is not None: a["serve"].append(sa)
    if sb is not None: b["serve"].append(sb)
    if ra is not None: a["return"].append(ra)
    if rb is not None: b["return"].append(rb)


def _canonical_index(db) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    rows = db.fetchall(
        """
        SELECT e.event_id,e.commence_time,e.player_one,e.player_two,e.tour
        FROM tennis_events e
        ORDER BY e.commence_time,e.event_id
        """
    )
    out = {}
    for row in rows:
        dt = _parse_dt(row.get("commence_time"))
        if not dt:
            continue
        n1, n2 = sorted((_norm_name(row.get("player_one")), _norm_name(row.get("player_two"))))
        out[(dt.date().isoformat(), n1, n2)] = dict(row)
    return out


def _market_probability(db, event_id: str, selection: str, at: datetime) -> Optional[float]:
    row = db.fetchone(
        """
        SELECT fair_probability FROM tennis_consensus_snapshots
        WHERE event_id=? AND selection=? AND captured_at<=?
        ORDER BY captured_at DESC,id DESC LIMIT 1
        """,
        (event_id, selection, _iso(at)),
    )
    try:
        return float(row["fair_probability"]) if row and row.get("fair_probability") is not None else None
    except Exception:
        return None


def _evidence_ready(feat: Mapping[str, Any], surface: Optional[str]) -> int:
    enough = min(int(feat.get("p1_prior_matches") or 0), int(feat.get("p2_prior_matches") or 0)) >= MIN_HISTORY_MATCHES
    if surface:
        enough = enough and min(int(feat.get("p1_surface_matches") or 0), int(feat.get("p2_surface_matches") or 0)) >= MIN_SURFACE_MATCHES
    return int(bool(enough))


def _confidence(feat: Mapping[str, Any], surface: Optional[str]) -> str:
    n = min(int(feat.get("p1_prior_matches") or 0), int(feat.get("p2_prior_matches") or 0))
    sn = min(int(feat.get("p1_surface_matches") or 0), int(feat.get("p2_surface_matches") or 0)) if surface else 0
    rich = sum(1 for key in ("p1_rank", "p2_rank", "p1_serve", "p2_serve") if feat.get(key) is not None)
    if n >= 25 and (not surface or sn >= 8) and rich >= 3:
        return "A"
    if n >= 15 and (not surface or sn >= 5) and rich >= 2:
        return "B"
    if n >= MIN_HISTORY_MATCHES and (not surface or sn >= MIN_SURFACE_MATCHES):
        return "C"
    return "WATCH"


def rebuild_walkforward(db) -> Dict[str, Any]:
    install_schema(db)
    matches = db.fetchall(
        """
        SELECT * FROM tennis_rich_matches
        WHERE player1_id IS NOT NULL AND player2_id IS NOT NULL AND match_date IS NOT NULL
        ORDER BY match_date,provider_match_id
        """
    )
    rankings = _load_rankings(db)
    canonical = _canonical_index(db)
    state = _new_model_state()
    rows_out = []
    for row in matches:
        at = _parse_dt(row.get("match_date"))
        if not at:
            continue
        try:
            p1id, p2id = int(row["player1_id"]), int(row["player2_id"])
        except Exception:
            continue
        p1, p2 = str(row.get("player1_name") or p1id), str(row.get("player2_name") or p2id)
        winner_id = row.get("winner_id")
        y = 1 if winner_id is None or int(winner_id) == p1id else 0
        surface = str(row.get("surface") or "").strip() or None
        tour = str(row.get("tour") or "").lower()
        feat = _features(state, rankings, tour, p1id, p2id, at, surface)
        p = _predict(state, feat)
        ready = _evidence_ready(feat, surface)
        stats1, stats2 = _extract_stats(row.get("stats_json"))

        pair = sorted((_norm_name(p1), _norm_name(p2)))
        canonical_row = canonical.get((at.date().isoformat(), pair[0], pair[1]))
        canonical_id = str(canonical_row.get("event_id")) if canonical_row else None
        market_p = None
        if canonical_row:
            raw = _market_probability(db, canonical_id, p1, at)
            if raw is None:
                # Provider/canonical name punctuation can differ. If provider p1
                # is canonical player two, invert the stored canonical p1 price.
                cp1 = str(canonical_row.get("player_one") or "")
                cprob = _market_probability(db, canonical_id, cp1, at)
                if cprob is not None:
                    market_p = cprob if _norm_name(cp1) == _norm_name(p1) else 1.0 - cprob
            else:
                market_p = raw
        rows_out.append((row, at, p1, p2, tour, surface, feat, p, y, ready, market_p, canonical_id))
        _learn(state, feat, p, y)
        _update_player_state(state, p1id, p2id, at, surface, y, stats1, stats2)

    db.execute("DELETE FROM tennis_predictive3_walkforward WHERE model_version=?", (MODEL_VERSION,))
    created_at = _iso(_now())
    for row, at, p1, p2, tour, surface, feat, p, y, ready, market_p, canonical_id in rows_out:
        db.execute(
            """
            INSERT INTO tennis_predictive3_walkforward(
                model_name,model_version,rich_match_id,canonical_event_id,commence_time,tour,surface,
                tournament_title,player_one,player_two,predicted_p1,market_p1,outcome_p1,brier,
                log_loss,market_brier,evidence_ready,feature_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                MODEL,MODEL_VERSION,int(row["provider_match_id"]),canonical_id,_iso(at),tour,surface,
                row.get("tournament_name"),p1,p2,p,market_p,y,_brier(p,y),_log_loss(p,y),
                _brier(market_p,y) if market_p is not None else None,ready,
                json.dumps(feat,separators=(",",":"),default=str),created_at,
            ),
        )
    return {
        "matches": len(matches),
        "walkforward_rows": len(rows_out),
        "evidence_ready": sum(x[9] for x in rows_out),
        "market_comparable": sum(1 for x in rows_out if x[10] is not None),
        "weights": dict(state["weights"]),
        "state": state,
    }


def _build_current_state(db, before: datetime) -> Tuple[Dict[str, Any], Dict[Any, Any]]:
    matches = db.fetchall(
        """
        SELECT * FROM tennis_rich_matches
        WHERE player1_id IS NOT NULL AND player2_id IS NOT NULL AND match_date IS NOT NULL
          AND match_date<? ORDER BY match_date,provider_match_id
        """,
        (_iso(before),),
    )
    rankings = _load_rankings(db)
    state = _new_model_state()
    for row in matches:
        at = _parse_dt(row.get("match_date"))
        if not at:
            continue
        p1id, p2id = int(row["player1_id"]), int(row["player2_id"])
        y = 1 if row.get("winner_id") is None or int(row["winner_id"]) == p1id else 0
        surface = str(row.get("surface") or "").strip() or None
        feat = _features(state, rankings, str(row.get("tour") or "").lower(), p1id, p2id, at, surface)
        p = _predict(state, feat)
        stats1, stats2 = _extract_stats(row.get("stats_json"))
        _learn(state, feat, p, y)
        _update_player_state(state, p1id, p2id, at, surface, y, stats1, stats2)
    return state, rankings


def create_forward_predictions(db, now: Optional[datetime] = None) -> Dict[str, int]:
    now = now or _now()
    state, rankings = _build_current_state(db, now)
    cutoff = now + timedelta(hours=FORWARD_HOURS)
    events = db.fetchall(
        """
        SELECT * FROM tennis_events
        WHERE status='UPCOMING' AND commence_time>? AND commence_time<=?
        ORDER BY commence_time,event_id
        """,
        (_iso(now), _iso(cutoff)),
    )
    created = 0
    skipped_no_fixture = 0
    for event in events:
        at = _parse_dt(event.get("commence_time"))
        if not at:
            continue
        tour = str(event.get("tour") or "").lower()
        if tour not in {"atp", "wta"}:
            sport_key = str(event.get("sport_key") or "").lower()
            tour = "wta" if "wta" in sport_key else "atp" if "atp" in sport_key else tour
        p1, p2 = str(event.get("player_one") or ""), str(event.get("player_two") or "")
        context = fixture_context(db, tour, p1, p2, event.get("commence_time")) if tour in {"atp","wta"} else None
        if not context:
            skipped_no_fixture += 1
            continue
        cp1, cp2 = _norm_name(p1), _norm_name(p2)
        fp1, fp2 = _norm_name(context.get("player1_name")), _norm_name(context.get("player2_name"))
        if {cp1,cp2} != {fp1,fp2}:
            skipped_no_fixture += 1
            continue
        if cp1 == fp1:
            p1id, p2id = int(context["player1_id"]), int(context["player2_id"])
        else:
            p1id, p2id = int(context["player2_id"]), int(context["player1_id"])
        surface = str(context.get("surface") or "").strip() or None
        feat = _features(state, rankings, tour, p1id, p2id, at, surface)
        probability = _predict(state, feat)
        market_p = _market_probability(db, str(event["event_id"]), p1, now)
        tier = _confidence(feat, surface)

        def insert(selection: str, opponent: str, prob: float, mp: Optional[float]) -> None:
            nonlocal created
            changed = db.execute(
                """
                INSERT INTO tennis_predictive3_predictions(
                    model_name,model_version,event_id,generated_at,commence_time,tour,surface,
                    tournament_title,selection,opponent,probability,fair_odds,market_probability,
                    market_fair_odds,model_edge_vs_market_pct,confidence_tier,feature_json,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN')
                ON CONFLICT(model_version,event_id,selection) DO NOTHING
                """,
                (
                    MODEL,MODEL_VERSION,event["event_id"],_iso(now),event["commence_time"],tour,surface,
                    event.get("tournament_title"),selection,opponent,prob,_fair_odds(prob),mp,
                    _fair_odds(mp) if mp is not None else None,
                    (prob-mp)*100.0 if mp is not None else None,tier,
                    json.dumps(feat,separators=(",",":"),default=str),
                ),
            )
            created += int(bool(changed))

        insert(p1,p2,probability,market_p)
        insert(p2,p1,1.0-probability,1.0-market_p if market_p is not None else None)
    return {"created": created, "skipped_no_rich_fixture": skipped_no_fixture}


def settle_forward_predictions(db) -> int:
    rows = db.fetchall(
        """
        SELECT p.id,p.selection,p.probability,r.winner,r.completed_at,r.fetched_at
        FROM tennis_predictive3_predictions p
        JOIN tennis_results r ON r.event_id=p.event_id
        WHERE p.status='OPEN' ORDER BY p.id
        """
    )
    updated = 0
    for row in rows:
        y = 1 if _norm_name(row.get("selection")) == _norm_name(row.get("winner")) else 0
        db.execute(
            """
            UPDATE tennis_predictive3_predictions
            SET status='SETTLED',outcome=?,brier=?,log_loss=?,settled_at=? WHERE id=?
            """,
            (
                y,_brier(float(row["probability"]),y),_log_loss(float(row["probability"]),y),
                row.get("completed_at") or row.get("fetched_at") or _iso(_now()),row["id"],
            ),
        )
        updated += 1
    return updated


def _metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"samples": 0, "accuracy_pct": None, "brier": None, "log_loss": None, "market_samples": 0, "market_brier": None, "brier_improvement_vs_market_pct": None}
    acc = sum(1 for r in rows if (float(r["predicted_p1"]) >= 0.5) == bool(int(r["outcome_p1"])))
    brier = sum(float(r["brier"]) for r in rows) / len(rows)
    ll = sum(float(r["log_loss"]) for r in rows) / len(rows)
    mkt = [r for r in rows if r.get("market_brier") is not None]
    market_brier = sum(float(r["market_brier"]) for r in mkt) / len(mkt) if mkt else None
    comparable_model_brier = sum(float(r["brier"]) for r in mkt) / len(mkt) if mkt else None
    return {
        "samples": len(rows),
        "accuracy_pct": acc / len(rows) * 100.0,
        "brier": brier,
        "log_loss": ll,
        "market_samples": len(mkt),
        "market_brier": market_brier,
        "brier_improvement_vs_market_pct": (
            (market_brier - comparable_model_brier) / market_brier * 100.0
            if market_brier and comparable_model_brier is not None and market_brier > 0 else None
        ),
    }


def run_maintenance(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    install_schema(db)
    now = now or _now()
    rich_count = int((db.fetchone("SELECT COUNT(*) AS n FROM tennis_rich_matches") or {}).get("n") or 0)
    rank_count = int((db.fetchone("SELECT COUNT(*) AS n FROM tennis_rich_rankings") or {}).get("n") or 0)
    prior = db.fetchone("SELECT * FROM tennis_predictive3_state WHERE singleton_id=1") or {}
    if rich_count < MIN_RICH_MATCHES_TO_BUILD:
        db.execute(
            """
            UPDATE tennis_predictive3_state SET last_rich_match_count=?,last_ranking_count=?,
                last_maintenance_at=?,status='BACKFILLING' WHERE singleton_id=1
            """,
            (rich_count,rank_count,_iso(now)),
        )
        return {"ok": True, "status": "BACKFILLING", "rich_matches": rich_count, "rankings": rank_count, "minimum_matches": MIN_RICH_MATCHES_TO_BUILD, "betting_authority": False}

    rebuilt = rich_count != int(prior.get("last_rich_match_count") or 0) or rank_count != int(prior.get("last_ranking_count") or 0)
    walk = None
    if rebuilt:
        walk = rebuild_walkforward(db)
    forward = create_forward_predictions(db, now)
    settled = settle_forward_predictions(db)
    weights = walk.get("weights") if walk else json.loads(str(prior.get("model_weights_json") or "{}"))
    db.execute(
        """
        UPDATE tennis_predictive3_state SET last_rich_match_count=?,last_ranking_count=?,
            last_walkforward_at=CASE WHEN ? THEN ? ELSE last_walkforward_at END,
            last_maintenance_at=?,status='ACTIVE_RESEARCH',model_weights_json=? WHERE singleton_id=1
        """,
        (rich_count,rank_count,int(rebuilt),_iso(now),_iso(now),json.dumps(weights,separators=(",",":"))),
    )
    return {
        "ok": True,
        "status": "ACTIVE_RESEARCH",
        "model": MODEL_VERSION,
        "walkforward_rebuilt": rebuilt,
        "walkforward": {k:v for k,v in (walk or {}).items() if k != "state"},
        "forward": forward,
        "settled": settled,
        "betting_authority": False,
    }


def scoreboard(db) -> Dict[str, Any]:
    install_schema(db)
    state = db.fetchone("SELECT * FROM tennis_predictive3_state WHERE singleton_id=1") or {}
    rows = db.fetchall("SELECT * FROM tennis_predictive3_walkforward WHERE model_version=? ORDER BY commence_time,id", (MODEL_VERSION,))
    ready = [r for r in rows if int(r.get("evidence_ready") or 0) == 1]
    predictions = db.fetchall("SELECT * FROM tennis_predictive3_predictions WHERE model_version=? ORDER BY generated_at,id", (MODEL_VERSION,))
    by_tour = {}
    for tour in ("atp","wta"):
        subset = [r for r in ready if str(r.get("tour") or "").lower() == tour]
        if subset:
            by_tour[tour.upper()] = _metrics(subset)
    by_surface = {}
    for surface in sorted({str(r.get("surface") or "") for r in ready if r.get("surface")}):
        subset = [r for r in ready if str(r.get("surface") or "") == surface]
        by_surface[surface] = _metrics(subset)
    return {
        "model": MODEL,
        "model_version": MODEL_VERSION,
        "status": state.get("status") or "BACKFILLING",
        "all_walkforward": _metrics(rows),
        "evidence_ready_walkforward": _metrics(ready),
        "by_tour": by_tour,
        "by_surface": by_surface,
        "forward_open": sum(1 for r in predictions if r.get("status") == "OPEN"),
        "forward_settled": sum(1 for r in predictions if r.get("status") == "SETTLED"),
        "last_walkforward_at": state.get("last_walkforward_at"),
        "rich_matches": int(state.get("last_rich_match_count") or 0),
        "ranking_rows": int(state.get("last_ranking_count") or 0),
        "weights": json.loads(str(state.get("model_weights_json") or "{}")),
        "betting_authority": False,
    }
