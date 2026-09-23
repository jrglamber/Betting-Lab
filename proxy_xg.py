from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import math
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import requests

from db import Database, sanitize_sensitive_text


APP_VERSION = "0.19.0"
MODEL_VERSION = "PXG1_STATSBOMB_RIDGE_V1"
FEATURE_VERSION = "PXG_FEATURES_V1"
FEATURES: Tuple[str, ...] = (
    "total_shots",
    "shots_on_target",
    "shots_off_target",
    "blocked_shots",
    "shots_inside_box",
    "shots_outside_box",
    "corners",
    "red_cards",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


DISCOVERY_RETRY_MAX_ATTEMPTS = 6
DISCOVERY_RETRY_COOLDOWN_HOURS = 6.0


def _parse_utc(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _discovery_error_retryable(row: Mapping[str, Any], now: Optional[datetime] = None) -> bool:
    """Allow transient/plan-gate discovery failures to recover after a cooldown.

    v0.17 stopped retrying a backfill day forever after three failures. That is
    too strict when an account is upgraded from API-Football Free to Pro or a
    provider timeout happens three times in a row. Keep the normal quick retry
    behaviour for attempts <3, then permit a small number of delayed retries
    only for errors that are plausibly transient.
    """
    if str(row.get("status") or "").upper() != "ERROR":
        return False
    attempts = int(row.get("attempts") or 0)
    if attempts < 3:
        return True
    if attempts >= DISCOVERY_RETRY_MAX_ATTEMPTS:
        return False
    message = str(row.get("last_error") or "").lower()
    retry_markers = (
        "free plans do not have access",
        "timed out",
        "timeout",
        "connection",
        "temporarily",
        "rate limit",
        "service unavailable",
    )
    if not any(marker in message for marker in retry_markers):
        return False
    updated = _parse_utc(row.get("updated_at"))
    if updated is None:
        return True
    current = now or _now()
    return current - updated >= timedelta(hours=DISCOVERY_RETRY_COOLDOWN_HOURS)


def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# Explicit league mapping keeps the free current-data collector inside the
# football universe already being researched by Betting Lab. Country is used
# where league names are ambiguous (Bundesliga, League One, etc.).
API_FOOTBALL_LEAGUE_MAP: Dict[Tuple[str, str], str] = {
    ("england", "premier league"): "soccer_epl",
    ("england", "championship"): "soccer_efl_champ",
    ("england", "league one"): "soccer_england_league1",
    ("england", "league two"): "soccer_england_league2",
    ("scotland", "premiership"): "soccer_spl",
    ("scotland", "scottish premiership"): "soccer_spl",
    ("spain", "la liga"): "soccer_spain_la_liga",
    ("spain", "segunda division"): "soccer_spain_segunda_division",
    ("germany", "bundesliga"): "soccer_germany_bundesliga",
    ("germany", "2 bundesliga"): "soccer_germany_bundesliga2",
    ("germany", "3 liga"): "soccer_germany_liga3",
    ("italy", "serie a"): "soccer_italy_serie_a",
    ("italy", "serie b"): "soccer_italy_serie_b",
    ("france", "ligue 1"): "soccer_france_ligue_one",
    ("france", "ligue 2"): "soccer_france_ligue_two",
    ("netherlands", "eredivisie"): "soccer_netherlands_eredivisie",
    ("portugal", "primeira liga"): "soccer_portugal_primeira_liga",
    ("belgium", "jupiler pro league"): "soccer_belgium_first_div",
    ("belgium", "pro league"): "soccer_belgium_first_div",
    ("austria", "bundesliga"): "soccer_austria_bundesliga",
    ("denmark", "superliga"): "soccer_denmark_superliga",
    ("switzerland", "super league"): "soccer_switzerland_superleague",
    ("norway", "eliteserien"): "soccer_norway_eliteserien",
    ("sweden", "allsvenskan"): "soccer_sweden_allsvenskan",
    ("sweden", "superettan"): "soccer_sweden_superettan",
    ("poland", "ekstraklasa"): "soccer_poland_ekstraklasa",
    ("greece", "super league 1"): "soccer_greece_super_league",
    ("greece", "super league"): "soccer_greece_super_league",
    ("turkey", "super lig"): "soccer_turkey_super_league",
    ("finland", "veikkausliiga"): "soccer_finland_veikkausliiga",
    ("ireland", "premier division"): "soccer_league_of_ireland",
    ("world", "uefa champions league"): "soccer_uefa_champs_league",
    ("world", "uefa europa league"): "soccer_uefa_europa_league",
    ("world", "uefa europa conference league"): "soccer_uefa_europa_conference_league",
    ("world", "uefa nations league"): "soccer_uefa_nations_league",
}


def map_api_football_league(country: Any, league_name: Any) -> Optional[str]:
    c, l = _norm(country), _norm(league_name)
    direct = API_FOOTBALL_LEAGUE_MAP.get((c, l))
    if direct:
        return direct
    # UEFA competitions are occasionally returned with a non-World country.
    for (_, mapped_league), sport_key in API_FOOTBALL_LEAGUE_MAP.items():
        if mapped_league == l and mapped_league.startswith("uefa "):
            return sport_key
    return None


def _to_num(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "")
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        return None


def _blank_features() -> Dict[str, float]:
    return {name: 0.0 for name in FEATURES}


def extract_statsbomb_features(events: Sequence[Mapping[str, Any]], teams: Sequence[str]) -> Dict[str, Dict[str, float]]:
    out = {str(team): _blank_features() for team in teams}
    canonical = {_norm(team): str(team) for team in teams}
    for event in events:
        team_obj = event.get("team") or {}
        team_name = str(team_obj.get("name") or "")
        key = canonical.get(_norm(team_name))
        if not key:
            continue
        kind = str((event.get("type") or {}).get("name") or "")
        if kind == "Shot":
            f = out[key]
            f["total_shots"] += 1.0
            shot = event.get("shot") or {}
            outcome = _norm((shot.get("outcome") or {}).get("name"))
            if outcome in {"goal", "saved", "saved to post"}:
                f["shots_on_target"] += 1.0
            elif outcome in {"off t", "wayward", "post", "saved off target"}:
                f["shots_off_target"] += 1.0
            elif outcome == "blocked":
                f["blocked_shots"] += 1.0
            location = event.get("location") or []
            inside = False
            if isinstance(location, (list, tuple)) and len(location) >= 2:
                try:
                    x, y = float(location[0]), float(location[1])
                    inside = x >= 102.0 and 18.0 <= y <= 62.0
                except Exception:
                    inside = False
            if inside:
                f["shots_inside_box"] += 1.0
            else:
                f["shots_outside_box"] += 1.0
        elif kind == "Pass":
            ptype = _norm(((event.get("pass") or {}).get("type") or {}).get("name"))
            if ptype == "corner":
                out[key]["corners"] += 1.0
        elif kind in {"Bad Behaviour", "Foul Committed"}:
            payload = event.get("bad_behaviour") or event.get("foul_committed") or {}
            card = _norm((payload.get("card") or {}).get("name"))
            if card in {"red card", "second yellow"}:
                out[key]["red_cards"] += 1.0
    return out


def extract_api_football_features(fixture_item: Mapping[str, Any]) -> Dict[str, Dict[str, float]]:
    teams = fixture_item.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    ids = {
        str(home.get("id")): str(home.get("name") or ""),
        str(away.get("id")): str(away.get("name") or ""),
    }
    result: Dict[str, Dict[str, float]] = {}
    name_map = {
        "shots on goal": "shots_on_target",
        "shots off goal": "shots_off_target",
        "total shots": "total_shots",
        "blocked shots": "blocked_shots",
        "shots insidebox": "shots_inside_box",
        "shots inside box": "shots_inside_box",
        "shots outsidebox": "shots_outside_box",
        "shots outside box": "shots_outside_box",
        "corner kicks": "corners",
        "red cards": "red_cards",
    }
    for block in fixture_item.get("statistics") or []:
        team = block.get("team") or {}
        team_name = str(team.get("name") or ids.get(str(team.get("id")), ""))
        if not team_name:
            continue
        values: Dict[str, Optional[float]] = {name: None for name in FEATURES}
        for stat in block.get("statistics") or []:
            mapped = name_map.get(_norm(stat.get("type")))
            if not mapped:
                continue
            values[mapped] = _to_num(stat.get("value"))
        # A null red-card value normally means zero; the core shot/corner fields
        # must be present so unavailable provider coverage cannot silently become 0.
        if values["red_cards"] is None:
            values["red_cards"] = 0.0
        core = [x for x in FEATURES if x != "red_cards"]
        if any(values[x] is None for x in core):
            continue
        result[team_name] = {x: float(values[x] or 0.0) for x in FEATURES}
    return result


def _gaussian_solve(a: List[List[float]], b: List[float]) -> List[float]:
    n = len(b)
    m = [list(a[i]) + [float(b[i])] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            raise ValueError("singular ridge system")
        if pivot != col:
            m[col], m[pivot] = m[pivot], m[col]
        div = m[col][col]
        m[col] = [x / div for x in m[col]]
        for row in range(n):
            if row == col:
                continue
            factor = m[row][col]
            if abs(factor) < 1e-15:
                continue
            m[row] = [m[row][j] - factor * m[col][j] for j in range(n + 1)]
    return [m[i][-1] for i in range(n)]


def fit_ridge(rows: Sequence[Mapping[str, Any]], alpha: float = 3.0) -> Dict[str, Any]:
    if len(rows) < 20:
        raise ValueError("not enough proxy-xG training samples")
    ordered = sorted(rows, key=lambda r: (str(r.get("played_at") or ""), int(r.get("id") or 0)))
    holdout_n = max(20, int(round(len(ordered) * 0.20)))
    holdout_n = min(holdout_n, max(1, len(ordered) - 20))
    train, holdout = ordered[:-holdout_n], ordered[-holdout_n:]
    means: Dict[str, float] = {}
    stds: Dict[str, float] = {}
    for feature in FEATURES:
        vals = [float(r.get(feature) or 0.0) for r in train]
        mean = sum(vals) / len(vals)
        var = sum((x - mean) ** 2 for x in vals) / max(1, len(vals))
        means[feature] = mean
        stds[feature] = max(math.sqrt(var), 1e-6)
    y = [float(r.get("target_xg") or 0.0) for r in train]
    y_mean = sum(y) / len(y)
    p = len(FEATURES)
    xtx = [[0.0 for _ in range(p)] for _ in range(p)]
    xty = [0.0 for _ in range(p)]
    for row, target in zip(train, y):
        x = [(float(row.get(f) or 0.0) - means[f]) / stds[f] for f in FEATURES]
        yc = target - y_mean
        for i in range(p):
            xty[i] += x[i] * yc
            for j in range(p):
                xtx[i][j] += x[i] * x[j]
    for i in range(p):
        xtx[i][i] += float(alpha)
    beta = _gaussian_solve(xtx, xty)
    model = {
        "features": list(FEATURES),
        "means": means,
        "stds": stds,
        "beta": beta,
        "y_mean": y_mean,
        "alpha": float(alpha),
    }

    def predict(row: Mapping[str, Any]) -> float:
        value = y_mean
        for i, feature in enumerate(FEATURES):
            value += beta[i] * ((float(row.get(feature) or 0.0) - means[feature]) / stds[feature])
        return max(0.05, min(5.0, value))

    preds = [predict(r) for r in holdout]
    actual = [float(r.get("target_xg") or 0.0) for r in holdout]
    baseline = [y_mean for _ in holdout]
    mae = sum(abs(a - p_) for a, p_ in zip(actual, preds)) / len(actual)
    rmse = math.sqrt(sum((a - p_) ** 2 for a, p_ in zip(actual, preds)) / len(actual))
    bmae = sum(abs(a - p_) for a, p_ in zip(actual, baseline)) / len(actual)
    brmse = math.sqrt(sum((a - p_) ** 2 for a, p_ in zip(actual, baseline)) / len(actual))
    return {
        "model": model,
        "sample_count": len(ordered),
        "training_count": len(train),
        "holdout_count": len(holdout),
        "holdout_mae": mae,
        "holdout_rmse": rmse,
        "baseline_mae": bmae,
        "baseline_rmse": brmse,
        "training_cutoff": str(train[-1].get("played_at") or ""),
        "status": "READY" if mae < bmae else "RESEARCH_ONLY",
    }


def predict_proxy_xg(features: Mapping[str, Any], model_payload: Mapping[str, Any]) -> float:
    means = model_payload.get("means") or {}
    stds = model_payload.get("stds") or {}
    beta = model_payload.get("beta") or []
    y_mean = float(model_payload.get("y_mean") or 0.0)
    value = y_mean
    for i, feature in enumerate(FEATURES):
        if i >= len(beta):
            break
        std = max(float(stds.get(feature) or 1.0), 1e-6)
        value += float(beta[i]) * ((float(features.get(feature) or 0.0) - float(means.get(feature) or 0.0)) / std)
    return max(0.05, min(5.0, value))


class ProxyXgEngine:
    def __init__(self, db: Database, settings, session=None):
        self.db = db
        self.settings = settings
        self.session = session or requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "proxy_xg_enabled", True))

    @property
    def api_key(self) -> str:
        return str(getattr(self.settings, "proxy_xg_api_football_key", "") or "").strip()

    def _statsbomb_get_json(self, match_id: str) -> Any:
        base = str(getattr(
            self.settings,
            "predictive_football_pred3_statsbomb_base_url",
            "https://raw.githubusercontent.com/hudl/open-data/master/data",
        )).rstrip("/")
        response = self.session.get(
            f"{base}/events/{match_id}.json",
            timeout=30,
            headers={"User-Agent": f"Project-Exit-Plan-Betting-Lab/{APP_VERSION}"},
        )
        response.raise_for_status()
        return response.json()

    def sync_statsbomb_manifest(self) -> int:
        rows = self.db.fetchall(
            """
            SELECT m.match_id
            FROM football_predictive3_statsbomb_manifest m
            JOIN football_predictive3_training_matches t ON t.statsbomb_match_id=m.match_id
            LEFT JOIN football_pxg_statsbomb_manifest x ON x.match_id=m.match_id
            WHERE m.status='IMPORTED' AND x.match_id IS NULL
            ORDER BY t.played_at DESC,m.match_id
            """
        )
        stamp = _now().isoformat()
        for row in rows:
            self.db.execute(
                "INSERT INTO football_pxg_statsbomb_manifest(match_id,status,attempts,created_at,updated_at) VALUES(?,?,?,?,?)",
                (str(row["match_id"]), "PENDING", 0, stamp, stamp),
            )
        return len(rows)

    def import_statsbomb_samples(self) -> Dict[str, int]:
        self.sync_statsbomb_manifest()
        limit = max(1, int(getattr(self.settings, "proxy_xg_statsbomb_matches_per_cycle", 12)))
        pending = self.db.fetchall(
            """
            SELECT x.match_id,t.*
            FROM football_pxg_statsbomb_manifest x
            JOIN football_predictive3_training_matches t ON t.statsbomb_match_id=x.match_id
            WHERE x.status='PENDING' OR (x.status='ERROR' AND x.attempts<3)
            ORDER BY CASE WHEN x.status='PENDING' THEN 0 ELSE 1 END,t.played_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        imported = errors = 0
        for row in pending:
            match_id = str(row["match_id"])
            stamp = _now().isoformat()
            self.db.execute(
                "UPDATE football_pxg_statsbomb_manifest SET attempts=attempts+1,updated_at=? WHERE match_id=?",
                (stamp, match_id),
            )
            try:
                events = self._statsbomb_get_json(match_id)
                if not isinstance(events, list):
                    raise ValueError("StatsBomb events payload was not a list")
                home = str(row["home_team"])
                away = str(row["away_team"])
                features = extract_statsbomb_features(events, (home, away))
                for team, opponent, is_home, target_xg, target_npxg in (
                    (home, away, 1, row["home_xg"], row["home_npxg"]),
                    (away, home, 0, row["away_xg"], row["away_npxg"]),
                ):
                    f = features.get(team)
                    if not f:
                        raise ValueError(f"StatsBomb features missing team {team}")
                    exists = self.db.fetchone(
                        "SELECT id FROM football_pxg_statsbomb_samples WHERE statsbomb_match_id=? AND team=?",
                        (match_id, team),
                    )
                    if not exists:
                        self.db.execute(
                            """
                            INSERT INTO football_pxg_statsbomb_samples(
                                statsbomb_match_id,sport_key,competition_name,season_name,played_at,
                                team,opponent,is_home,target_xg,target_npxg,total_shots,shots_on_target,
                                shots_off_target,blocked_shots,shots_inside_box,shots_outside_box,
                                corners,red_cards,feature_version,imported_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                match_id,row["sport_key"],row["competition_name"],row.get("season_name"),row["played_at"],
                                team,opponent,is_home,float(target_xg),float(target_npxg),
                                f["total_shots"],f["shots_on_target"],f["shots_off_target"],f["blocked_shots"],
                                f["shots_inside_box"],f["shots_outside_box"],f["corners"],f["red_cards"],
                                FEATURE_VERSION,stamp,
                            ),
                        )
                self.db.execute(
                    "UPDATE football_pxg_statsbomb_manifest SET status='IMPORTED',last_error=NULL,updated_at=? WHERE match_id=?",
                    (stamp, match_id),
                )
                imported += 1
            except Exception as exc:
                self.db.execute(
                    "UPDATE football_pxg_statsbomb_manifest SET status='ERROR',last_error=?,updated_at=? WHERE match_id=?",
                    (sanitize_sensitive_text(exc)[:1000], stamp, match_id),
                )
                errors += 1
        return {"statsbomb_matches_imported": imported, "statsbomb_errors": errors}

    def fit_model_if_due(self) -> Dict[str, Any]:
        rows = self.db.fetchall(
            "SELECT * FROM football_pxg_statsbomb_samples ORDER BY played_at,id"
        )
        count = len(rows)
        min_samples = max(50, int(getattr(self.settings, "proxy_xg_min_training_samples", 200)))
        if count < min_samples:
            return {"model_trained": 0, "training_samples": count, "reason": "INSUFFICIENT_SAMPLES"}
        latest = self.db.fetchone("SELECT * FROM football_pxg_models ORDER BY id DESC LIMIT 1")
        refit_every = max(10, int(getattr(self.settings, "proxy_xg_refit_every_samples", 40)))
        if latest and count < int(latest.get("sample_count") or 0) + refit_every:
            return {"model_trained": 0, "training_samples": count, "reason": "NO_REFIT_DUE"}
        fit = fit_ridge(rows, alpha=float(getattr(self.settings, "proxy_xg_ridge_alpha", 3.0)))
        stamp = _now().isoformat()
        self.db.execute(
            """
            INSERT INTO football_pxg_models(
                trained_at,model_version,feature_version,status,sample_count,training_count,
                holdout_count,training_cutoff,coefficients_json,holdout_mae,holdout_rmse,
                baseline_mae,baseline_rmse
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                stamp,MODEL_VERSION,FEATURE_VERSION,fit["status"],fit["sample_count"],fit["training_count"],
                fit["holdout_count"],fit["training_cutoff"],json.dumps(fit["model"],sort_keys=True),
                fit["holdout_mae"],fit["holdout_rmse"],fit["baseline_mae"],fit["baseline_rmse"],
            ),
        )
        return {
            "model_trained": 1,
            "training_samples": count,
            "status": fit["status"],
            "holdout_mae": fit["holdout_mae"],
            "baseline_mae": fit["baseline_mae"],
        }

    def _api_usage(self) -> Dict[str, Any]:
        day = _now().date().isoformat()
        return self.db.fetchone("SELECT * FROM football_pxg_api_usage WHERE usage_date=?", (day,)) or {
            "usage_date": day, "calls": 0, "provider_remaining": None
        }

    def _api_call_allowed(self) -> bool:
        if not self.api_key:
            return False
        usage = self._api_usage()
        budget = max(1, int(getattr(self.settings, "proxy_xg_api_daily_call_budget", 90)))
        reserve = max(0, int(getattr(self.settings, "proxy_xg_api_provider_reserve", 5)))
        if int(usage.get("calls") or 0) >= budget:
            return False
        remaining = usage.get("provider_remaining")
        if remaining is not None and int(remaining) <= reserve:
            return False
        return True

    def _record_api_call(self, response: Optional[requests.Response]) -> None:
        day = _now().date().isoformat()
        remaining = None
        if response is not None:
            raw = response.headers.get("x-ratelimit-requests-remaining")
            try:
                remaining = int(raw) if raw is not None else None
            except Exception:
                remaining = None
        existing = self.db.fetchone("SELECT * FROM football_pxg_api_usage WHERE usage_date=?", (day,))
        stamp = _now().isoformat()
        if existing:
            self.db.execute(
                "UPDATE football_pxg_api_usage SET calls=calls+1,provider_remaining=COALESCE(?,provider_remaining),updated_at=? WHERE usage_date=?",
                (remaining, stamp, day),
            )
        else:
            self.db.execute(
                "INSERT INTO football_pxg_api_usage(usage_date,calls,provider_remaining,updated_at) VALUES(?,?,?,?)",
                (day, 1, remaining, stamp),
            )

    def _api_get(self, path: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self._api_call_allowed():
            raise RuntimeError("API_FOOTBALL_QUOTA_GUARD")
        base = str(getattr(self.settings, "proxy_xg_api_football_base_url", "https://v3.football.api-sports.io")).rstrip("/")
        response: Optional[requests.Response] = None
        try:
            response = self.session.get(
                f"{base}/{path.lstrip('/')}",
                params=dict(params),
                timeout=30,
                headers={"x-apisports-key": self.api_key, "User-Agent": f"Project-Exit-Plan-Betting-Lab/{APP_VERSION}"},
            )
            self._record_api_call(response)
            response.raise_for_status()
            payload = response.json()
            errors = payload.get("errors") if isinstance(payload, dict) else None
            if errors:
                raise RuntimeError(f"API-Football errors: {errors}")
            return payload
        except Exception:
            if response is None:
                self._record_api_call(None)
            raise

    def _next_discovery_day(self) -> Optional[str]:
        days = max(1, int(getattr(self.settings, "proxy_xg_api_backfill_days", 45)))
        today = _now().date()
        for offset in range(1, days + 1):
            day = (today - timedelta(days=offset)).isoformat()
            row = self.db.fetchone("SELECT * FROM football_pxg_api_discovery_days WHERE day=?", (day,))
            if not row:
                return day
            if _discovery_error_retryable(row):
                return day
        return None

    def discover_api_football_day(self) -> Dict[str, Any]:
        if not self.api_key:
            return {"api_discovery": 0, "reason": "NO_API_KEY"}
        day = self._next_discovery_day()
        if not day:
            return {"api_discovery": 0, "reason": "BACKFILL_COMPLETE"}
        stamp = _now().isoformat()
        row = self.db.fetchone("SELECT * FROM football_pxg_api_discovery_days WHERE day=?", (day,))
        if row:
            self.db.execute("UPDATE football_pxg_api_discovery_days SET attempts=attempts+1,updated_at=? WHERE day=?", (stamp, day))
        else:
            self.db.execute(
                "INSERT INTO football_pxg_api_discovery_days(day,status,attempts,fixtures_seen,relevant_found,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (day, "PENDING", 1, 0, 0, stamp, stamp),
            )
        try:
            payload = self._api_get("fixtures", {"date": day, "timezone": "UTC"})
            fixtures = payload.get("response") or []
            relevant = 0
            for item in fixtures:
                fixture = item.get("fixture") or {}
                league = item.get("league") or {}
                teams = item.get("teams") or {}
                status = fixture.get("status") or {}
                short = str(status.get("short") or "")
                if short not in {"FT", "AET", "PEN"}:
                    continue
                sport_key = map_api_football_league(league.get("country"), league.get("name"))
                if not sport_key:
                    continue
                if sport_key not in set(getattr(self.settings, "sport_keys", ())):
                    continue
                fixture_id = str(fixture.get("id") or "")
                home = str((teams.get("home") or {}).get("name") or "")
                away = str((teams.get("away") or {}).get("name") or "")
                played_at = str(fixture.get("date") or "")
                if not fixture_id or not home or not away or not played_at:
                    continue
                existing = self.db.fetchone("SELECT fixture_id FROM football_pxg_api_manifest WHERE fixture_id=?", (fixture_id,))
                if existing:
                    continue
                self.db.execute(
                    """
                    INSERT INTO football_pxg_api_manifest(
                        fixture_id,sport_key,league_name,country,played_at,home_team,away_team,
                        status,attempts,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (fixture_id,sport_key,str(league.get("name") or ""),str(league.get("country") or ""),played_at,
                     home,away,"PENDING",0,stamp,stamp),
                )
                relevant += 1
            self.db.execute(
                "UPDATE football_pxg_api_discovery_days SET status='IMPORTED',fixtures_seen=?,relevant_found=?,last_error=NULL,updated_at=? WHERE day=?",
                (len(fixtures), relevant, stamp, day),
            )
            return {"api_discovery": 1, "day": day, "fixtures_seen": len(fixtures), "relevant_found": relevant}
        except Exception as exc:
            self.db.execute(
                "UPDATE football_pxg_api_discovery_days SET status='ERROR',last_error=?,updated_at=? WHERE day=?",
                (sanitize_sensitive_text(exc, (self.api_key,))[:1000], stamp, day),
            )
            return {"api_discovery": 0, "day": day, "reason": sanitize_sensitive_text(exc, (self.api_key,))[:200]}

    def import_api_football_matches(self) -> Dict[str, int]:
        if not self.api_key:
            return {"current_matches_imported": 0, "current_match_errors": 0}
        limit = max(1, int(getattr(self.settings, "proxy_xg_api_matches_per_cycle", 4)))
        pending = self.db.fetchall(
            """
            SELECT * FROM football_pxg_api_manifest
            WHERE status='PENDING' OR (status='ERROR' AND attempts<3)
            ORDER BY played_at DESC LIMIT ?
            """,
            (limit,),
        )
        imported = errors = 0
        for row in pending:
            if not self._api_call_allowed():
                break
            fixture_id = str(row["fixture_id"])
            stamp = _now().isoformat()
            self.db.execute("UPDATE football_pxg_api_manifest SET attempts=attempts+1,updated_at=? WHERE fixture_id=?", (stamp, fixture_id))
            try:
                payload = self._api_get("fixtures", {"id": fixture_id, "timezone": "UTC"})
                response = payload.get("response") or []
                if not response:
                    raise ValueError("API-Football fixture detail returned no rows")
                item = response[0]
                features = extract_api_football_features(item)
                home = str(row["home_team"])
                away = str(row["away_team"])
                # Provider spelling should usually match the discovery row exactly;
                # normalized matching tolerates accents/punctuation differences.
                by_norm = {_norm(name): feat for name, feat in features.items()}
                hf = by_norm.get(_norm(home)); af = by_norm.get(_norm(away))
                if not hf or not af:
                    raise ValueError("API-Football embedded fixture statistics incomplete")
                goals = item.get("goals") or {}
                hg = goals.get("home"); ag = goals.get("away")
                existing = self.db.fetchone("SELECT id FROM football_pxg_current_matches WHERE fixture_id=?", (fixture_id,))
                if not existing:
                    self.db.execute(
                        """
                        INSERT INTO football_pxg_current_matches(
                            fixture_id,sport_key,league_name,country,played_at,home_team,away_team,
                            home_goals,away_goals,home_features_json,away_features_json,
                            source,imported_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (fixture_id,row["sport_key"],row["league_name"],row["country"],row["played_at"],home,away,
                         hg,ag,json.dumps(hf,sort_keys=True),json.dumps(af,sort_keys=True),"API-FOOTBALL",stamp),
                    )
                self.db.execute("UPDATE football_pxg_api_manifest SET status='IMPORTED',last_error=NULL,updated_at=? WHERE fixture_id=?", (stamp, fixture_id))
                imported += 1
            except Exception as exc:
                self.db.execute(
                    "UPDATE football_pxg_api_manifest SET status='ERROR',last_error=?,updated_at=? WHERE fixture_id=?",
                    (sanitize_sensitive_text(exc, (self.api_key,))[:1000], stamp, fixture_id),
                )
                errors += 1
        return {"current_matches_imported": imported, "current_match_errors": errors}

    def rescore_current_matches(self) -> int:
        model_row = self.db.fetchone("SELECT * FROM football_pxg_models ORDER BY id DESC LIMIT 1")
        if not model_row:
            return 0
        try:
            payload = json.loads(str(model_row["coefficients_json"]))
        except Exception:
            return 0
        rows = self.db.fetchall(
            "SELECT * FROM football_pxg_current_matches WHERE pxg_model_id IS NULL OR pxg_model_id<>?",
            (model_row["id"],),
        )
        updated = 0
        for row in rows:
            try:
                hf = json.loads(str(row["home_features_json"])); af = json.loads(str(row["away_features_json"]))
                hp = predict_proxy_xg(hf, payload); ap = predict_proxy_xg(af, payload)
            except Exception:
                continue
            self.db.execute(
                "UPDATE football_pxg_current_matches SET home_proxy_xg=?,away_proxy_xg=?,pxg_model_id=?,scored_at=? WHERE id=?",
                (hp, ap, model_row["id"], _now().isoformat(), row["id"]),
            )
            updated += 1
        return updated

    def one_cycle(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        result: Dict[str, Any] = {"enabled": True}
        result.update(self.import_statsbomb_samples())
        result.update(self.fit_model_if_due())
        if self.api_key:
            result.update(self.discover_api_football_day())
            result.update(self.import_api_football_matches())
        else:
            result["api_football"] = "WAITING_FOR_API_KEY"
        result["current_matches_rescored"] = self.rescore_current_matches()
        return result


def proxy_xg_status(db: Database, settings) -> Dict[str, Any]:
    samples = db.fetchone("SELECT COUNT(*) AS n,COUNT(DISTINCT statsbomb_match_id) AS matches FROM football_pxg_statsbomb_samples") or {}
    manifest = db.fetchone(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status='IMPORTED' THEN 1 ELSE 0 END) AS imported,
               SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN status='ERROR' THEN 1 ELSE 0 END) AS errors
        FROM football_pxg_statsbomb_manifest
        """
    ) or {}
    api_manifest = db.fetchone(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status='IMPORTED' THEN 1 ELSE 0 END) AS imported,
               SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN status='ERROR' THEN 1 ELSE 0 END) AS errors
        FROM football_pxg_api_manifest
        """
    ) or {}
    current = db.fetchone(
        "SELECT COUNT(*) AS matches,MAX(played_at) AS latest,SUM(CASE WHEN home_proxy_xg IS NOT NULL AND away_proxy_xg IS NOT NULL THEN 1 ELSE 0 END) AS scored FROM football_pxg_current_matches"
    ) or {}
    model = db.fetchone("SELECT * FROM football_pxg_models ORDER BY id DESC LIMIT 1")
    usage = db.fetchone("SELECT * FROM football_pxg_api_usage WHERE usage_date=?", (_now().date().isoformat(),)) or {}
    return {
        "enabled": bool(getattr(settings, "proxy_xg_enabled", True)),
        "api_football_configured": bool(str(getattr(settings, "proxy_xg_api_football_key", "") or "").strip()),
        "api_daily_budget": int(getattr(settings, "proxy_xg_api_daily_call_budget", 90)),
        "today_api_usage": usage,
        "statsbomb_manifest": manifest,
        "training_samples": int(samples.get("n") or 0),
        "training_matches": int(samples.get("matches") or 0),
        "latest_model": model,
        "api_manifest": api_manifest,
        "current_matches": current,
        "model_version": MODEL_VERSION,
        "feature_version": FEATURE_VERSION,
        "note": "PXG1 is research-only. Existing PRED1/PRED2/PRED3 and betting lanes are unchanged.",
    }
