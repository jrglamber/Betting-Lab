from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import hashlib
import io
import json
import math
import re
import unicodedata
from statistics import mean
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import requests

from db import Database, sanitize_sensitive_text, utc_now_iso
from execution_shadow import clv_quality, commission_adjusted_pnl
from fair_value import clv_pct


APP_VERSION = "0.16.1"
EXPERIMENT_VERSION = "PRED1_SCORE_POISSON"
MARKET_EXPERIMENT_VERSION = "PRED1_DERIVED_MARKETS_V1"
MODEL_VERSION = "PRED1_BAYES_POISSON_V1"

# football-data.co.uk league codes for the major league set already collected
# by Betting Lab. Only score/result fields are imported; no bookmaker odds are
# read into the predictive model.


FOOTBALL_DATA_BASE_URL = "https://football-data.co.uk/mmz4281"
FOOTBALL_DATA_ALLOWED_HOSTS = {"football-data.co.uk", "www.football-data.co.uk"}


def _fetch_football_data_csv(session, url: str, *, timeout: int, headers: Mapping[str, str], max_redirects: int = 3):
    """Fetch a football-data CSV without following redirects to unexpected hosts.

    Railway logs in Sep-2026 showed the upstream www endpoint redirecting a CSV
    request to 127.0.0.1, which requests followed automatically. Start from the
    canonical non-www host and only follow HTTPS redirects that stay on the two
    football-data.co.uk hostnames.
    """
    from urllib.parse import urljoin, urlparse

    current = str(url)
    for _ in range(max(0, int(max_redirects)) + 1):
        response = session.get(
            current, timeout=timeout, headers=dict(headers), allow_redirects=False
        )
        status = int(getattr(response, "status_code", 200) or 200)
        if status in {301, 302, 303, 307, 308}:
            location = (getattr(response, "headers", {}) or {}).get("Location")
            if not location:
                raise RuntimeError(f"football-data redirect {status} missing Location")
            nxt = urljoin(current, str(location))
            parsed = urlparse(nxt)
            host = (parsed.hostname or "").lower()
            if parsed.scheme.lower() != "https" or host not in FOOTBALL_DATA_ALLOWED_HOSTS:
                raise RuntimeError(f"blocked football-data redirect to {parsed.scheme}://{host}")
            current = nxt
            continue
        response.raise_for_status()
        return response
    raise RuntimeError("football-data redirect limit exceeded")

FOOTBALL_DATA_LEAGUE_CODES: Dict[str, str] = {
    "soccer_epl": "E0",
    "soccer_efl_champ": "E1",
    "soccer_england_league1": "E2",
    "soccer_england_league2": "E3",
    "soccer_spl": "SC0",
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

ALIAS_NORMALIZATION = {
    "man united": "manchester united",
    "man utd": "manchester united",
    "man city": "manchester city",
    "nottm forest": "nottingham forest",
    "nott m forest": "nottingham forest",
    "wolves": "wolverhampton wanderers",
    "spurs": "tottenham hotspur",
    "sheffield utd": "sheffield united",
    "sheffield weds": "sheffield wednesday",
    "ath madrid": "atletico madrid",
    "atl madrid": "atletico madrid",
    "athletic club": "athletic bilbao",
    "inter": "inter milan",
    "internazionale": "inter milan",
    "ac milan": "milan",
    "paris sg": "paris saint germain",
    "psg": "paris saint germain",
    "marseille": "olympique marseille",
    "lyon": "olympique lyonnais",
    "monchengladbach": "borussia monchengladbach",
    "m gladbach": "borussia monchengladbach",
    "bayern munich": "bayern munchen",
    "koln": "fc koln",
    "cologne": "fc koln",
    "st pauli": "fc st pauli",
    "sporting lisbon": "sporting cp",
    "sp lisbon": "sporting cp",
    "benfica": "sl benfica",
}


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"\b(fc|cf|afc|sc|ac|fk|bk|if|sv|cd|ud|calcio|club)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return ALIAS_NORMALIZATION.get(text, text)


def _match_key(sport_key: str, played_at: str, home_team: str, away_team: str) -> str:
    date = parse_iso(played_at).date().isoformat()
    raw = "|".join(
        [sport_key, date, _normalize_text(home_team), _normalize_text(away_team)]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _config_hash(settings, execution_books: Sequence[str]) -> str:
    payload = {
        "experiment": EXPERIMENT_VERSION,
        "forecast_hours": float(settings.predictive_football_forecast_hours_before),
        "min_league_matches": int(settings.predictive_football_min_league_matches),
        "min_team_matches": float(settings.predictive_football_min_team_matches),
        "prior_matches": float(settings.predictive_football_prior_matches),
        "half_life_days": float(settings.predictive_football_half_life_days),
        "lookback_days": int(settings.predictive_football_lookback_days),
        "min_edge_pct": float(settings.predictive_football_min_edge_pct),
        "strong_edge_pct": float(settings.predictive_football_strong_edge_pct),
        "markets": list(getattr(settings, "predictive_football_markets", ("h2h",))),
        "total_points": list(getattr(settings, "predictive_football_total_points", (2.5,))),
        "execution_books": list(execution_books),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def _season_code(now: datetime, offset: int = 0) -> str:
    # Football-data uses codes such as 2627 for the 2026/27 season.
    year = now.year if now.month >= 7 else now.year - 1
    year += int(offset)
    return f"{year % 100:02d}{(year + 1) % 100:02d}"


def _parse_football_data_date(raw: str) -> Optional[datetime]:
    value = str(raw or "").strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _poisson_pmf(k: int, lam: float) -> float:
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def score_probabilities(home_lambda: float, away_lambda: float, max_goals: int = 10) -> Tuple[float, float, float]:
    home = draw = away = 0.0
    for i in range(max_goals + 1):
        ph = _poisson_pmf(i, home_lambda)
        for j in range(max_goals + 1):
            p = ph * _poisson_pmf(j, away_lambda)
            if i > j:
                home += p
            elif i == j:
                draw += p
            else:
                away += p
    total = home + draw + away
    if total <= 0:
        return (1/3, 1/3, 1/3)
    return home / total, draw / total, away / total



def btts_probabilities(home_lambda: float, away_lambda: float) -> Tuple[float, float]:
    # Under independent Poisson scoring, P(BTTS) is the probability each side
    # scores at least once. This is derived from the already-frozen lambdas.
    yes = (1.0 - math.exp(-home_lambda)) * (1.0 - math.exp(-away_lambda))
    yes = max(0.0, min(1.0, yes))
    return yes, 1.0 - yes


def total_probabilities(
    home_lambda: float,
    away_lambda: float,
    point: float,
) -> Tuple[float, float]:
    # PRED1 only enables half-goal lines by default, so there is no push state.
    lam = max(1e-9, float(home_lambda) + float(away_lambda))
    cutoff = math.floor(float(point))
    under = sum(_poisson_pmf(k, lam) for k in range(cutoff + 1))
    under = max(0.0, min(1.0, under))
    return 1.0 - under, under  # Over, Under


def derived_market_probabilities(
    home_lambda: float,
    away_lambda: float,
    markets: Sequence[str],
    total_points: Sequence[float],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    enabled = {str(x).lower() for x in markets}
    if "btts" in enabled:
        yes, no = btts_probabilities(home_lambda, away_lambda)
        out.extend([
            {"market_key":"btts","selection":"Yes","point":None,"line_key":"","probability":yes},
            {"market_key":"btts","selection":"No","point":None,"line_key":"","probability":no},
        ])
    if "totals" in enabled:
        for raw_point in total_points:
            point = float(raw_point)
            # Avoid accidental push-bearing integer lines in this first version.
            if abs((point * 2.0) - round(point * 2.0)) > 1e-9:
                continue
            if abs(point - round(point)) < 1e-9:
                continue
            over, under = total_probabilities(home_lambda, away_lambda, point)
            line = f"{point:.1f}"
            out.extend([
                {"market_key":"totals","selection":"Over","point":point,"line_key":line,"probability":over},
                {"market_key":"totals","selection":"Under","point":point,"line_key":line,"probability":under},
            ])
    for row in out:
        row["fair_odds"] = _fair_odds(float(row["probability"]))
    return out


def _point_equal(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    try:
        return abs(float(a) - float(b)) < 1e-9
    except Exception:
        return False


def _valid_two_way_wave(
    rows: Sequence[Mapping[str, Any]],
    market_key: str,
    point: Optional[float],
    approved_books: Sequence[str],
) -> Dict[str, Dict[str, Mapping[str, Any]]]:
    approved = set(str(x) for x in approved_books)
    required = {"Yes","No"} if market_key == "btts" else {"Over","Under"}
    grouped: Dict[str, Dict[str, Mapping[str, Any]]] = {}
    outcomes: Dict[str, set[str]] = {}
    for row in rows:
        book = str(row.get("bookmaker_key") or "")
        if book not in approved:
            continue
        if str(row.get("market_key") or "") != str(market_key):
            continue
        if market_key == "totals" and not _point_equal(row.get("point"), point):
            continue
        if market_key == "btts" and row.get("point") is not None:
            continue
        selection = str(row.get("outcome_name") or "")
        try:
            price = float(row.get("price"))
        except Exception:
            continue
        if selection not in required or price <= 1.0:
            continue
        grouped.setdefault(book,{})[selection]=row
        outcomes.setdefault(book,set()).add(selection)
    return {
        book: selections for book,selections in grouped.items()
        if outcomes.get(book)==required and required.issubset(selections)
    }


def _minimum_odds(probability: float, edge_pct_value: float) -> float:
    if probability <= 0:
        return float("inf")
    return (1.0 + edge_pct_value / 100.0) / probability


def _edge_pct(probability: float, odds: float) -> float:
    return (probability * odds - 1.0) * 100.0


def _fair_odds(probability: float) -> float:
    return 1.0 / max(1e-9, probability)


def _valid_three_way_wave(
    rows: Sequence[Mapping[str, Any]],
    home_team: str,
    away_team: str,
    approved_books: Sequence[str],
) -> Dict[str, Dict[str, Mapping[str, Any]]]:
    approved = set(str(x) for x in approved_books)
    required = {str(home_team), str(away_team), "Draw"}
    grouped: Dict[str, Dict[str, Mapping[str, Any]]] = {}
    outcomes: Dict[str, set[str]] = {}
    for row in rows:
        book = str(row.get("bookmaker_key") or "")
        if book not in approved:
            continue
        if str(row.get("market_key") or "") != "h2h":
            continue
        sel = str(row.get("outcome_name") or "")
        try:
            price = float(row.get("price"))
        except Exception:
            continue
        if not sel or price <= 1.0:
            continue
        grouped.setdefault(book, {})[sel] = row
        outcomes.setdefault(book, set()).add(sel)
    return {
        book: selections
        for book, selections in grouped.items()
        if outcomes.get(book) == required and required.issubset(selections)
    }


def _selection_probabilities(prediction: Mapping[str, Any]) -> Dict[str, Tuple[float, float]]:
    return {
        str(prediction["home_team"]): (
            float(prediction["home_probability"]),
            float(prediction["home_fair_odds"]),
        ),
        "Draw": (
            float(prediction["draw_probability"]),
            float(prediction["draw_fair_odds"]),
        ),
        str(prediction["away_team"]): (
            float(prediction["away_probability"]),
            float(prediction["away_fair_odds"]),
        ),
    }


def _team_mapping(target: str, candidates: Sequence[str]) -> Optional[Tuple[str, float]]:
    if not candidates:
        return None
    tn = _normalize_text(target)
    exact = [c for c in candidates if _normalize_text(c) == tn]
    if exact:
        return exact[0], 1.0
    scored = sorted(
        (
            (SequenceMatcher(None, tn, _normalize_text(c)).ratio(), c)
            for c in candidates
        ),
        reverse=True,
    )
    best_score, best_name = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < 0.72 or best_score - second < 0.04:
        return None
    return best_name, float(best_score)


def _weight(days_ago: float, half_life_days: float) -> float:
    return 0.5 ** (max(0.0, days_ago) / max(1.0, half_life_days))


class PredictiveFootballEngine:
    def __init__(
        self,
        db: Database,
        settings,
        *,
        execution_bookmaker_keys: Sequence[str],
        session=None,
    ):
        self.db = db
        self.settings = settings
        self.execution_bookmaker_keys = tuple(str(x) for x in execution_bookmaker_keys)
        self.session = session or requests.Session()
        self.config_hash = _config_hash(settings, self.execution_bookmaker_keys)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.predictive_football_enabled)

    def sync_internal_results(self) -> int:
        rows = self.db.fetchall(
            """
            SELECT r.event_id,r.completed_at,r.fetched_at,r.home_score,r.away_score,
                   e.sport_key,e.home_team,e.away_team,e.commence_time
            FROM event_results r
            JOIN events e ON e.event_id=r.event_id
            WHERE e.sport_key LIKE ?
            ORDER BY COALESCE(r.completed_at,e.commence_time),r.event_id
            """,
            ("soccer_%",),
        )
        inserted = 0
        for r in rows:
            played = str(r.get("completed_at") or r.get("commence_time") or r.get("fetched_at"))
            try:
                parse_iso(played)
            except Exception:
                continue
            key = _match_key(r["sport_key"], played, r["home_team"], r["away_team"])
            exists = self.db.fetchone(
                "SELECT id FROM football_predictive_training_matches WHERE match_key=?",
                (key,),
            )
            if exists:
                continue
            self.db.execute(
                """
                INSERT INTO football_predictive_training_matches(
                    match_key,sport_key,played_at,home_team,away_team,
                    home_goals,away_goals,source,source_ref,imported_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    key,r["sport_key"],played,r["home_team"],r["away_team"],
                    int(r["home_score"]),int(r["away_score"]),
                    "the_odds_api",r["event_id"],utc_now_iso(),
                ),
            )
            inserted += 1
        return inserted

    def _earliest_internal_result_date(self, sport_key: str) -> Optional[datetime.date]:
        row = self.db.fetchone(
            """
            SELECT MIN(COALESCE(r.completed_at,e.commence_time)) AS first_at
            FROM event_results r
            JOIN events e ON e.event_id=r.event_id
            WHERE e.sport_key=?
            """,
            (sport_key,),
        )
        value = (row or {}).get("first_at")
        if not value:
            return None
        try:
            return parse_iso(value).date()
        except Exception:
            return None

    def _bootstrap_source_due(self, source_key: str, now: datetime) -> bool:
        row = self.db.fetchone(
            "SELECT * FROM football_predictive_source_state WHERE source_key=?",
            (source_key,),
        )
        if not row or not row.get("last_attempt_at"):
            return True
        try:
            elapsed = (now - parse_iso(row["last_attempt_at"])).total_seconds()
        except Exception:
            return True
        if not row.get("last_success_at"):
            return elapsed >= 3600
        return elapsed >= max(
            3600, int(self.settings.predictive_football_bootstrap_refresh_seconds)
        )

    def _set_source_state(
        self,
        source_key: str,
        *,
        attempted_at: str,
        success: bool,
        rows_imported: int,
        error: str = "",
    ) -> None:
        existing = self.db.fetchone(
            "SELECT source_key FROM football_predictive_source_state WHERE source_key=?",
            (source_key,),
        )
        if existing:
            if success:
                self.db.execute(
                    """
                    UPDATE football_predictive_source_state
                    SET last_attempt_at=?,last_success_at=?,rows_imported=?,
                        last_error=NULL
                    WHERE source_key=?
                    """,
                    (attempted_at,attempted_at,int(rows_imported),source_key),
                )
            else:
                self.db.execute(
                    """
                    UPDATE football_predictive_source_state
                    SET last_attempt_at=?,last_error=? WHERE source_key=?
                    """,
                    (attempted_at,sanitize_sensitive_text(error)[:1000],source_key),
                )
        else:
            self.db.execute(
                """
                INSERT INTO football_predictive_source_state(
                    source_key,last_attempt_at,last_success_at,rows_imported,last_error
                ) VALUES(?,?,?,?,?)
                """,
                (
                    source_key,attempted_at,attempted_at if success else None,
                    int(rows_imported),None if success else sanitize_sensitive_text(error)[:1000],
                ),
            )

    def _import_bootstrap_csv(
        self,
        sport_key: str,
        season_code: str,
        csv_text: str,
        now: datetime,
    ) -> int:
        reader = csv.DictReader(io.StringIO(csv_text))
        cutoff = self._earliest_internal_result_date(sport_key)
        inserted = 0
        for row in reader:
            played = _parse_football_data_date(row.get("Date", ""))
            if not played or played > now:
                continue
            # Internal provider results are authoritative once Betting Lab began
            # recording the league, avoiding double-counting current matches.
            if cutoff is not None and played.date() >= cutoff:
                continue
            home = str(row.get("HomeTeam") or "").strip()
            away = str(row.get("AwayTeam") or "").strip()
            if not home or not away:
                continue
            try:
                hg = int(float(row.get("FTHG")))
                ag = int(float(row.get("FTAG")))
            except Exception:
                continue
            key = _match_key(sport_key, played.isoformat(), home, away)
            exists = self.db.fetchone(
                "SELECT id FROM football_predictive_training_matches WHERE match_key=?",
                (key,),
            )
            if exists:
                continue
            self.db.execute(
                """
                INSERT INTO football_predictive_training_matches(
                    match_key,sport_key,played_at,home_team,away_team,
                    home_goals,away_goals,source,source_ref,imported_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    key,sport_key,played.isoformat(),home,away,hg,ag,
                    "football-data.co.uk",season_code,utc_now_iso(),
                ),
            )
            inserted += 1
        return inserted

    def bootstrap_target_sports(self) -> List[str]:
        # Prioritise football competitions that actually exist in the Betting Lab
        # database, then configured football keys, then every score source we
        # know how to import. This deliberately does NOT require SPORT_KEYS to
        # be populated correctly; the score bootstrap is free and should never
        # remain at 0/0 just because a Railway sport-key variable is blank or
        # uses a different collection configuration.
        observed = self.db.fetchall(
            """
            SELECT sport_key,COUNT(*) AS n
            FROM events
            WHERE sport_key LIKE ?
            GROUP BY sport_key
            ORDER BY n DESC,sport_key
            """,
            ("soccer_%",),
        )
        ordered: List[str] = []
        for key in [str(r["sport_key"]) for r in observed]:
            if key in FOOTBALL_DATA_LEAGUE_CODES and key not in ordered:
                ordered.append(key)
        for key in tuple(getattr(self.settings, "sport_keys", ()) or ()):
            key = str(key)
            if key in FOOTBALL_DATA_LEAGUE_CODES and key not in ordered:
                ordered.append(key)
        for key in FOOTBALL_DATA_LEAGUE_CODES:
            if key not in ordered:
                ordered.append(key)
        return ordered

    def bootstrap_historical_data(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        if not self.enabled or not self.settings.predictive_football_bootstrap_enabled:
            return {"enabled": False, "attempted": 0, "imported": 0}

        attempted = imported = successes = 0
        source_limit = max(
            1, int(self.settings.predictive_football_bootstrap_sources_per_cycle)
        )
        stop = False
        for sport_key in self.bootstrap_target_sports():
            if stop:
                break
            league_code = FOOTBALL_DATA_LEAGUE_CODES[sport_key]
            for offset in (-1, 0):
                if attempted >= source_limit:
                    stop = True
                    break
                season = _season_code(now, offset)
                source_key = f"football_data:{sport_key}:{season}"
                if not self._bootstrap_source_due(source_key, now):
                    continue
                attempted += 1
                stamp = now.isoformat()
                url = (
                    f"{FOOTBALL_DATA_BASE_URL}/{season}/{league_code}.csv"
                )
                try:
                    response = _fetch_football_data_csv(
                        self.session, url, timeout=20,
                        headers={"User-Agent": "Project-Exit-Plan-Betting-Lab/0.16.1"},
                    )
                    text = response.content.decode("utf-8-sig", errors="replace")
                    n = self._import_bootstrap_csv(sport_key, season, text, now)
                    imported += n
                    successes += 1
                    self._set_source_state(
                        source_key,attempted_at=stamp,success=True,
                        rows_imported=n,
                    )
                except Exception as exc:
                    self._set_source_state(
                        source_key,attempted_at=stamp,success=False,
                        rows_imported=0,error=str(exc),
                    )
                    self.db.record_collector_run(
                        "PREDICTIVE_BOOTSTRAP",False,sport_key=sport_key,
                        detail=f"source={source_key}; error={sanitize_sensitive_text(exc)}",
                    )
        if attempted:
            self.db.record_collector_run(
                "PREDICTIVE_BOOTSTRAP",True,
                detail=(
                    f"attempted={attempted}; successes={successes}; "
                    f"rows_imported={imported}; provider_credits=0"
                ),
            )
        return {
            "enabled": True,"attempted":attempted,"successes":successes,
            "imported":imported,
            "target_sports":self.bootstrap_target_sports(),
        }

    def _training_rows(
        self,
        sport_key: str,
        kickoff: datetime,
        *,
        training_cutoff: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        # training_cutoff is used only by the v0.13 historical validator. It
        # lets a past fixture be reconstructed with information that existed
        # before the forecast freeze, while preserving the real kickoff time
        # for recency weights. Forward PRED1/PRED2 calls leave it as None.
        cutoff = training_cutoff or kickoff
        start = cutoff - timedelta(days=int(self.settings.predictive_football_lookback_days))
        return self.db.fetchall(
            """
            SELECT * FROM football_predictive_training_matches
            WHERE sport_key=? AND played_at<? AND played_at>=?
            ORDER BY played_at,id
            """,
            (sport_key,cutoff.isoformat(),start.isoformat()),
        )

    def fit_event(
        self,
        event: Mapping[str, Any],
        *,
        now: Optional[datetime] = None,
        training_cutoff: Optional[datetime] = None,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        now = now or datetime.now(timezone.utc)
        try:
            kickoff = parse_iso(event["commence_time"])
        except Exception:
            return None, "INVALID_KICKOFF"
        rows = self._training_rows(event["sport_key"], kickoff, training_cutoff=training_cutoff)
        if len(rows) < int(self.settings.predictive_football_min_league_matches):
            return None, "INSUFFICIENT_LEAGUE_HISTORY"

        names = sorted(
            {
                str(r["home_team"]) for r in rows
            } | {
                str(r["away_team"]) for r in rows
            }
        )
        hm = _team_mapping(str(event["home_team"]), names)
        am = _team_mapping(str(event["away_team"]), names)
        if not hm:
            return None, "HOME_TEAM_UNMAPPED"
        if not am:
            return None, "AWAY_TEAM_UNMAPPED"
        mapped_home, home_map_score = hm
        mapped_away, away_map_score = am

        half_life = float(self.settings.predictive_football_half_life_days)
        weighted = []
        for r in rows:
            played = parse_iso(r["played_at"])
            days = max(0.0, (kickoff - played).total_seconds() / 86400.0)
            w = _weight(days,half_life)
            weighted.append((r,w))

        total_w = sum(w for _,w in weighted)
        if total_w <= 0:
            return None, "NO_EFFECTIVE_HISTORY"
        league_home_mean = sum(float(r["home_goals"])*w for r,w in weighted)/total_w
        league_away_mean = sum(float(r["away_goals"])*w for r,w in weighted)/total_w
        base_team_goal = max(0.3,(league_home_mean+league_away_mean)/2.0)

        def team_strength(team_name: str) -> Tuple[float,float,float]:
            scored = conceded = baseline_scored = baseline_conceded = weight_sum = 0.0
            appearances = 0.0
            canonical = _normalize_text(team_name)
            for r,w in weighted:
                if _normalize_text(str(r["home_team"])) == canonical:
                    scored += float(r["home_goals"])*w
                    conceded += float(r["away_goals"])*w
                    baseline_scored += league_home_mean*w
                    baseline_conceded += league_away_mean*w
                    weight_sum += w
                    appearances += w
                elif _normalize_text(str(r["away_team"])) == canonical:
                    scored += float(r["away_goals"])*w
                    conceded += float(r["home_goals"])*w
                    baseline_scored += league_away_mean*w
                    baseline_conceded += league_home_mean*w
                    weight_sum += w
                    appearances += w
            prior = max(0.0,float(self.settings.predictive_football_prior_matches))
            attack = (
                scored + prior*base_team_goal
            ) / max(1e-9, baseline_scored + prior*base_team_goal)
            defense = (
                conceded + prior*base_team_goal
            ) / max(1e-9, baseline_conceded + prior*base_team_goal)
            return attack,defense,appearances

        h_att,h_def,h_eff = team_strength(mapped_home)
        a_att,a_def,a_eff = team_strength(mapped_away)

        minimum_team = float(self.settings.predictive_football_min_team_matches)
        if h_eff < minimum_team:
            return None, "INSUFFICIENT_HOME_TEAM_HISTORY"
        if a_eff < minimum_team:
            return None, "INSUFFICIENT_AWAY_TEAM_HISTORY"

        home_lambda = max(0.20,min(4.50,league_home_mean*h_att*a_def))
        away_lambda = max(0.20,min(4.50,league_away_mean*a_att*h_def))
        hp,dp,ap = score_probabilities(home_lambda,away_lambda)

        return {
            "expected_home_goals":home_lambda,
            "expected_away_goals":away_lambda,
            "home_probability":hp,"draw_probability":dp,"away_probability":ap,
            "home_fair_odds":_fair_odds(hp),
            "draw_fair_odds":_fair_odds(dp),
            "away_fair_odds":_fair_odds(ap),
            "league_training_matches":len(rows),
            "home_effective_matches":h_eff,
            "away_effective_matches":a_eff,
            "mapped_home_team":mapped_home,
            "mapped_away_team":mapped_away,
            "home_mapping_score":home_map_score,
            "away_mapping_score":away_map_score,
        }, "OK"

    def freeze_due_predictions(self, now: Optional[datetime] = None) -> Dict[str, int]:
        now = now or datetime.now(timezone.utc)
        forecast_h = float(self.settings.predictive_football_forecast_hours_before)
        horizon = now + timedelta(hours=forecast_h)
        events = self.db.fetchall(
            """
            SELECT * FROM events
            WHERE status='UPCOMING' AND commence_time>? AND commence_time<=?
              AND sport_key LIKE ?
            ORDER BY commence_time,event_id
            """,
            (now.isoformat(),horizon.isoformat(),"soccer_%"),
        )
        created = skipped = 0
        reasons: Dict[str,int] = {}
        for event in events:
            existing = self.db.fetchone(
                "SELECT id FROM football_predictive_predictions WHERE event_id=?",
                (event["event_id"],),
            )
            if existing:
                continue
            fit,reason = self.fit_event(event,now=now)
            if not fit:
                skipped += 1
                reasons[reason]=reasons.get(reason,0)+1
                continue
            self.db.execute(
                """
                INSERT INTO football_predictive_predictions(
                    event_id,created_at,sport_key,league,commence_time,
                    home_team,away_team,mapped_home_team,mapped_away_team,
                    home_mapping_score,away_mapping_score,
                    expected_home_goals,expected_away_goals,
                    home_probability,draw_probability,away_probability,
                    home_fair_odds,draw_fair_odds,away_fair_odds,
                    league_training_matches,home_effective_matches,
                    away_effective_matches,model_version,config_hash
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event["event_id"],now.isoformat(),event["sport_key"],event["league"],
                    event["commence_time"],event["home_team"],event["away_team"],
                    fit["mapped_home_team"],fit["mapped_away_team"],
                    fit["home_mapping_score"],fit["away_mapping_score"],
                    fit["expected_home_goals"],fit["expected_away_goals"],
                    fit["home_probability"],fit["draw_probability"],fit["away_probability"],
                    fit["home_fair_odds"],fit["draw_fair_odds"],fit["away_fair_odds"],
                    fit["league_training_matches"],fit["home_effective_matches"],
                    fit["away_effective_matches"],MODEL_VERSION,self.config_hash,
                ),
            )
            created += 1
        if created or skipped:
            self.db.record_collector_run(
                "PREDICTIVE_FORECAST",True,
                detail=f"created={created}; skipped={skipped}; reasons={json.dumps(reasons,sort_keys=True)}",
            )
        return {"created":created,"skipped":skipped}


    def ensure_market_predictions(self) -> int:
        markets = tuple(
            str(x).lower()
            for x in getattr(
                self.settings,"predictive_football_markets",("h2h","btts","totals")
            )
        )
        if not ({"btts","totals"} & set(markets)):
            return 0
        total_points = tuple(
            float(x)
            for x in getattr(
                self.settings,"predictive_football_total_points",(1.5,2.5,3.5)
            )
        )
        predictions = self.db.fetchall(
            "SELECT * FROM football_predictive_predictions ORDER BY id"
        )
        created = 0
        for pred in predictions:
            rows = derived_market_probabilities(
                float(pred["expected_home_goals"]),
                float(pred["expected_away_goals"]),
                markets,
                total_points,
            )
            for row in rows:
                exists = self.db.fetchone(
                    """
                    SELECT id FROM football_predictive_market_predictions
                    WHERE prediction_id=? AND market_key=? AND selection=?
                      AND line_key=?
                    """,
                    (
                        pred["id"],row["market_key"],row["selection"],
                        row["line_key"],
                    ),
                )
                if exists:
                    continue
                self.db.execute(
                    """
                    INSERT INTO football_predictive_market_predictions(
                        prediction_id,event_id,created_at,market_key,selection,
                        point,line_key,probability,fair_odds
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        pred["id"],pred["event_id"],pred["created_at"],
                        row["market_key"],row["selection"],row["point"],
                        row["line_key"],row["probability"],row["fair_odds"],
                    ),
                )
                created += 1
        return created

    def _market_waves(
        self,
        prediction: Mapping[str, Any],
        market_key: str,
    ) -> List[str]:
        rows = self.db.fetchall(
            """
            SELECT DISTINCT captured_at
            FROM odds_snapshots
            WHERE event_id=? AND captured_at>=? AND captured_at<?
              AND market_key=?
            ORDER BY captured_at
            """,
            (
                prediction["event_id"],prediction["created_at"],
                prediction["commence_time"],market_key,
            ),
        )
        return [str(r["captured_at"]) for r in rows]

    def evaluate_market_predictions(
        self,
        now: Optional[datetime]=None,
    ) -> int:
        now = now or datetime.now(timezone.utc)
        rows = self.db.fetchall(
            """
            SELECT mp.*,p.commence_time,p.home_team,p.away_team
            FROM football_predictive_market_predictions mp
            JOIN football_predictive_predictions p ON p.id=mp.prediction_id
            WHERE p.commence_time>?
            ORDER BY mp.prediction_id,mp.market_key,mp.point,mp.selection
            """,
            (now.isoformat(),),
        )
        created = 0
        for mp in rows:
            for wave in self._market_waves(mp,str(mp["market_key"])):
                exists_eval = self.db.fetchone(
                    """
                    SELECT id FROM football_predictive_market_evaluations
                    WHERE market_prediction_id=? AND evaluated_at=?
                    """,
                    (mp["id"],wave),
                )
                if exists_eval:
                    continue
                quote_rows = self.db.fetchall(
                    """
                    SELECT * FROM odds_snapshots
                    WHERE event_id=? AND captured_at=? AND market_key=?
                    ORDER BY id
                    """,
                    (mp["event_id"],wave,mp["market_key"]),
                )
                valid = _valid_two_way_wave(
                    quote_rows,str(mp["market_key"]),mp.get("point"),
                    self.execution_bookmaker_keys,
                )
                quotes=[]
                for book,selections in valid.items():
                    q=selections.get(str(mp["selection"]))
                    if q:
                        quotes.append(q)

                prob=float(mp["probability"])
                fair=float(mp["fair_odds"])
                min_odds=_minimum_odds(
                    prob,float(self.settings.predictive_football_min_edge_pct)
                )
                if not quotes:
                    self.db.execute(
                        """
                        INSERT INTO football_predictive_market_evaluations(
                            market_prediction_id,prediction_id,event_id,
                            evaluated_at,market_key,selection,point,line_key,
                            model_probability,model_fair_odds,min_required_odds,
                            decision,reason,strong_candidate
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            mp["id"],mp["prediction_id"],mp["event_id"],wave,
                            mp["market_key"],mp["selection"],mp.get("point"),
                            mp["line_key"],prob,fair,min_odds,
                            "REJECT","NO_APPROVED_EXACT_MARKET_QUOTE",0,
                        ),
                    )
                    continue

                best=max(
                    quotes,key=lambda r:(float(r["price"]),str(r["bookmaker_key"]))
                )
                odds=float(best["price"])
                edge=_edge_pct(prob,odds)
                predictive_key="|".join([
                    str(mp["event_id"]),str(mp["market_key"]),
                    str(mp["selection"]),str(mp["line_key"]),
                ])
                frozen=self.db.fetchone(
                    """
                    SELECT id FROM football_predictive_market_bets
                    WHERE predictive_key=?
                    """,
                    (predictive_key,),
                )
                if frozen:
                    decision="TRACK";reason="PREDICTIVE_MARKET_BET_ALREADY_FROZEN"
                elif odds + 1e-12 < min_odds:
                    decision="REJECT";reason="EXECUTABLE_PRICE_BELOW_MODEL_MIN"
                else:
                    decision="ACCEPT";reason="FIRST_ACCEPTABLE_MODEL_PRICE"
                    strong=int(
                        edge >= float(
                            self.settings.predictive_football_strong_edge_pct
                        )
                    )
                    self.db.execute(
                        """
                        INSERT INTO football_predictive_market_bets(
                            predictive_key,market_prediction_id,prediction_id,
                            event_id,created_at,market_key,selection,point,line_key,
                            bookmaker_key,bookmaker_title,offered_odds,
                            model_probability,model_fair_odds,edge_pct,min_odds,
                            strong_candidate,status,app_version,
                            experiment_version,config_hash
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            predictive_key,mp["id"],mp["prediction_id"],
                            mp["event_id"],wave,mp["market_key"],mp["selection"],
                            mp.get("point"),mp["line_key"],best["bookmaker_key"],
                            best["bookmaker_title"],odds,prob,fair,edge,min_odds,
                            strong,"OPEN",APP_VERSION,MARKET_EXPERIMENT_VERSION,
                            self.config_hash,
                        ),
                    )
                    created += 1
                strong=int(
                    edge >= float(self.settings.predictive_football_strong_edge_pct)
                )
                self.db.execute(
                    """
                    INSERT INTO football_predictive_market_evaluations(
                        market_prediction_id,prediction_id,event_id,evaluated_at,
                        market_key,selection,point,line_key,bookmaker_key,
                        bookmaker_title,best_executable_odds,model_probability,
                        model_fair_odds,min_required_odds,edge_pct,decision,
                        reason,strong_candidate
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        mp["id"],mp["prediction_id"],mp["event_id"],wave,
                        mp["market_key"],mp["selection"],mp.get("point"),
                        mp["line_key"],best["bookmaker_key"],
                        best["bookmaker_title"],odds,prob,fair,min_odds,edge,
                        decision,reason,strong,
                    ),
                )
        return created

    def track_market_prices(self, now: Optional[datetime]=None) -> int:
        now=now or datetime.now(timezone.utc)
        bets=self.db.fetchall(
            """
            SELECT b.*,p.commence_time
            FROM football_predictive_market_bets b
            JOIN football_predictive_predictions p ON p.id=b.prediction_id
            ORDER BY b.id
            """
        )
        inserted=0
        for bet in bets:
            start=parse_iso(bet["commence_time"])
            until=min(now,start)
            waves=self.db.fetchall(
                """
                SELECT DISTINCT captured_at FROM odds_snapshots
                WHERE event_id=? AND captured_at>? AND captured_at<=?
                  AND market_key=? ORDER BY captured_at
                """,
                (
                    bet["event_id"],bet["created_at"],until.isoformat(),
                    bet["market_key"],
                ),
            )
            for w in waves:
                wave=str(w["captured_at"])
                exists=self.db.fetchone(
                    """
                    SELECT id FROM football_predictive_market_price_observations
                    WHERE predictive_bet_id=? AND source_snapshot_at=?
                    """,
                    (bet["id"],wave),
                )
                if exists:
                    continue
                quote_rows=self.db.fetchall(
                    """
                    SELECT * FROM odds_snapshots
                    WHERE event_id=? AND captured_at=? AND bookmaker_key=?
                      AND market_key=? ORDER BY id
                    """,
                    (
                        bet["event_id"],wave,bet["bookmaker_key"],
                        bet["market_key"],
                    ),
                )
                valid=_valid_two_way_wave(
                    quote_rows,str(bet["market_key"]),bet.get("point"),
                    (bet["bookmaker_key"],),
                )
                q=(valid.get(str(bet["bookmaker_key"])) or {}).get(
                    str(bet["selection"])
                )
                if not q:
                    continue
                price=float(q["price"])
                move=(float(bet["offered_odds"])/price-1.0)*100.0
                self.db.execute(
                    """
                    INSERT INTO football_predictive_market_price_observations(
                        predictive_bet_id,observed_at,source_snapshot_at,
                        bookmaker_key,price,move_vs_entry_pct
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (bet["id"],utc_now_iso(),wave,bet["bookmaker_key"],price,move),
                )
                inserted+=1
        return inserted

    def finalize_market_clv(self, now: Optional[datetime]=None) -> int:
        now=now or datetime.now(timezone.utc)
        bets=self.db.fetchall(
            """
            SELECT b.*,p.commence_time
            FROM football_predictive_market_bets b
            JOIN football_predictive_predictions p ON p.id=b.prediction_id
            WHERE b.clv_pct IS NULL
            ORDER BY b.id
            """
        )
        updated=0
        for bet in bets:
            start=parse_iso(bet["commence_time"])
            if start>now:
                continue
            waves=self.db.fetchall(
                """
                SELECT DISTINCT captured_at FROM odds_snapshots
                WHERE event_id=? AND captured_at<=? AND captured_at>=?
                  AND market_key=? ORDER BY captured_at DESC
                """,
                (
                    bet["event_id"],start.isoformat(),bet["created_at"],
                    bet["market_key"],
                ),
            )
            close=None
            for w in waves:
                wave=str(w["captured_at"])
                quote_rows=self.db.fetchall(
                    """
                    SELECT * FROM odds_snapshots
                    WHERE event_id=? AND captured_at=? AND bookmaker_key=?
                      AND market_key=? ORDER BY id
                    """,
                    (
                        bet["event_id"],wave,bet["bookmaker_key"],
                        bet["market_key"],
                    ),
                )
                valid=_valid_two_way_wave(
                    quote_rows,str(bet["market_key"]),bet.get("point"),
                    (bet["bookmaker_key"],),
                )
                q=(valid.get(str(bet["bookmaker_key"])) or {}).get(
                    str(bet["selection"])
                )
                if q:
                    close=q
                    break
            if not close:
                continue
            close_at=parse_iso(close["captured_at"])
            mins=max(0.0,(start-close_at).total_seconds()/60.0)
            closing=float(close["price"])
            self.db.execute(
                """
                UPDATE football_predictive_market_bets
                SET closing_odds=?,clv_pct=?,closing_observed_at=?,
                    closing_minutes_before_kickoff=?,clv_quality=?
                WHERE id=?
                """,
                (
                    closing,clv_pct(float(bet["offered_odds"]),closing),
                    close["captured_at"],mins,clv_quality(mins),bet["id"],
                ),
            )
            updated+=1
        return updated

    @staticmethod
    def _market_actual(
        market_key: str,
        selection: str,
        point: Optional[float],
        home_score: int,
        away_score: int,
    ) -> bool:
        if market_key=="btts":
            actual="Yes" if home_score>0 and away_score>0 else "No"
            return selection==actual
        if market_key=="totals":
            if point is None:
                return False
            total=home_score+away_score
            actual="Over" if total>float(point) else "Under"
            return selection==actual
        return False

    def settle_market_predictions(self) -> Dict[str,int]:
        market_predictions=self.db.fetchall(
            """
            SELECT mp.*,r.home_score,r.away_score
            FROM football_predictive_market_predictions mp
            JOIN event_results r ON r.event_id=mp.event_id
            WHERE mp.actual_hit IS NULL
            ORDER BY mp.id
            """
        )
        pred_settled=0
        for mp in market_predictions:
            hit=self._market_actual(
                str(mp["market_key"]),str(mp["selection"]),mp.get("point"),
                int(mp["home_score"]),int(mp["away_score"]),
            )
            y=1.0 if hit else 0.0
            p=float(mp["probability"])
            self.db.execute(
                """
                UPDATE football_predictive_market_predictions
                SET actual_hit=?,brier_score=?,settled_at=? WHERE id=?
                """,
                (int(hit),(p-y)**2,utc_now_iso(),mp["id"]),
            )
            pred_settled+=1

        bets=self.db.fetchall(
            """
            SELECT b.*,r.home_score,r.away_score
            FROM football_predictive_market_bets b
            JOIN event_results r ON r.event_id=b.event_id
            WHERE b.status='OPEN'
            ORDER BY b.id
            """
        )
        bet_settled=0
        for bet in bets:
            hit=self._market_actual(
                str(bet["market_key"]),str(bet["selection"]),bet.get("point"),
                int(bet["home_score"]),int(bet["away_score"]),
            )
            result="WIN" if hit else "LOSS"
            gross=float(bet["offered_odds"])-1.0 if hit else -1.0
            rate,commission,net=commission_adjusted_pnl(
                gross,str(bet["bookmaker_key"])
            )
            self.db.execute(
                """
                UPDATE football_predictive_market_bets
                SET status='SETTLED',result=?,pnl_units=?,
                    commission_rate_pct=?,commission_units=?,net_pnl_units=?,
                    settled_at=? WHERE id=?
                """,
                (
                    result,gross,rate,commission,net,utc_now_iso(),bet["id"],
                ),
            )
            bet_settled+=1
        return {"predictions":pred_settled,"bets":bet_settled}

    def _unprocessed_waves(
        self,
        prediction: Mapping[str, Any],
    ) -> List[str]:
        rows = self.db.fetchall(
            """
            SELECT DISTINCT captured_at
            FROM odds_snapshots
            WHERE event_id=? AND captured_at>=? AND captured_at<?
              AND market_key='h2h'
            ORDER BY captured_at
            """,
            (
                prediction["event_id"],prediction["created_at"],
                prediction["commence_time"],
            ),
        )
        return [str(r["captured_at"]) for r in rows]

    def evaluate_predictions(self) -> int:
        predictions = self.db.fetchall(
            """
            SELECT * FROM football_predictive_predictions
            WHERE commence_time>?
            ORDER BY created_at,id
            """,
            (utc_now_iso(),),
        )
        created = 0
        for pred in predictions:
            probs = _selection_probabilities(pred)
            for wave in self._unprocessed_waves(pred):
                rows = self.db.fetchall(
                    """
                    SELECT * FROM odds_snapshots
                    WHERE event_id=? AND captured_at=? AND market_key='h2h'
                    ORDER BY id
                    """,
                    (pred["event_id"],wave),
                )
                valid = _valid_three_way_wave(
                    rows,pred["home_team"],pred["away_team"],
                    self.execution_bookmaker_keys,
                )
                for selection,(prob,fair) in probs.items():
                    existing_eval = self.db.fetchone(
                        """
                        SELECT id FROM football_predictive_evaluations
                        WHERE prediction_id=? AND evaluated_at=? AND selection=?
                        """,
                        (pred["id"],wave,selection),
                    )
                    if existing_eval:
                        continue

                    quotes = []
                    for book,sels in valid.items():
                        row = sels.get(selection)
                        if row:
                            quotes.append(row)
                    min_odds = _minimum_odds(
                        prob,float(self.settings.predictive_football_min_edge_pct)
                    )
                    if not quotes:
                        self.db.execute(
                            """
                            INSERT INTO football_predictive_evaluations(
                                prediction_id,event_id,evaluated_at,selection,
                                model_probability,model_fair_odds,min_required_odds,
                                decision,reason,strong_candidate
                            ) VALUES(?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                pred["id"],pred["event_id"],wave,selection,prob,fair,
                                min_odds,"REJECT","NO_APPROVED_THREE_WAY_QUOTE",0,
                            ),
                        )
                        continue

                    best = max(
                        quotes,
                        key=lambda r:(float(r["price"]),str(r["bookmaker_key"])),
                    )
                    odds = float(best["price"])
                    edge = _edge_pct(prob,odds)
                    key = f"{pred['event_id']}|{selection}"
                    frozen = self.db.fetchone(
                        "SELECT id FROM football_predictive_bets WHERE predictive_key=?",
                        (key,),
                    )
                    if frozen:
                        decision="TRACK";reason="PREDICTIVE_BET_ALREADY_FROZEN"
                    elif odds + 1e-12 < min_odds:
                        decision="REJECT";reason="EXECUTABLE_PRICE_BELOW_MODEL_MIN"
                    else:
                        decision="ACCEPT";reason="FIRST_ACCEPTABLE_MODEL_PRICE"
                        strong = int(
                            edge >= float(self.settings.predictive_football_strong_edge_pct)
                        )
                        self.db.execute(
                            """
                            INSERT INTO football_predictive_bets(
                                predictive_key,prediction_id,event_id,created_at,
                                selection,bookmaker_key,bookmaker_title,
                                offered_odds,model_probability,model_fair_odds,
                                edge_pct,min_odds,strong_candidate,status,
                                app_version,experiment_version,config_hash
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                key,pred["id"],pred["event_id"],wave,selection,
                                best["bookmaker_key"],best["bookmaker_title"],odds,
                                prob,fair,edge,min_odds,strong,"OPEN",
                                APP_VERSION,EXPERIMENT_VERSION,self.config_hash,
                            ),
                        )
                        created += 1
                    strong = int(
                        edge >= float(self.settings.predictive_football_strong_edge_pct)
                    )
                    self.db.execute(
                        """
                        INSERT INTO football_predictive_evaluations(
                            prediction_id,event_id,evaluated_at,selection,
                            bookmaker_key,bookmaker_title,best_executable_odds,
                            model_probability,model_fair_odds,min_required_odds,
                            edge_pct,decision,reason,strong_candidate
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            pred["id"],pred["event_id"],wave,selection,
                            best["bookmaker_key"],best["bookmaker_title"],odds,
                            prob,fair,min_odds,edge,decision,reason,strong,
                        ),
                    )
        return created

    def track_prices(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        bets = self.db.fetchall(
            """
            SELECT b.*,p.home_team,p.away_team,p.commence_time
            FROM football_predictive_bets b
            JOIN football_predictive_predictions p ON p.id=b.prediction_id
            ORDER BY b.id
            """
        )
        inserted=0
        for bet in bets:
            start=parse_iso(bet["commence_time"])
            until=min(now,start)
            waves=self.db.fetchall(
                """
                SELECT DISTINCT captured_at FROM odds_snapshots
                WHERE event_id=? AND captured_at>? AND captured_at<=?
                  AND market_key='h2h' ORDER BY captured_at
                """,
                (bet["event_id"],bet["created_at"],until.isoformat()),
            )
            for w in waves:
                wave=str(w["captured_at"])
                exists=self.db.fetchone(
                    """
                    SELECT id FROM football_predictive_price_observations
                    WHERE predictive_bet_id=? AND source_snapshot_at=?
                    """,
                    (bet["id"],wave),
                )
                if exists:
                    continue
                rows=self.db.fetchall(
                    """
                    SELECT * FROM odds_snapshots
                    WHERE event_id=? AND captured_at=? AND bookmaker_key=?
                      AND market_key='h2h' ORDER BY id
                    """,
                    (bet["event_id"],wave,bet["bookmaker_key"]),
                )
                valid=_valid_three_way_wave(
                    rows,bet["home_team"],bet["away_team"],
                    (bet["bookmaker_key"],),
                )
                row=(valid.get(bet["bookmaker_key"]) or {}).get(bet["selection"])
                if not row:
                    continue
                price=float(row["price"])
                move=(float(bet["offered_odds"])/price-1.0)*100.0
                self.db.execute(
                    """
                    INSERT INTO football_predictive_price_observations(
                        predictive_bet_id,observed_at,source_snapshot_at,
                        bookmaker_key,price,move_vs_entry_pct
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (bet["id"],utc_now_iso(),wave,bet["bookmaker_key"],price,move),
                )
                inserted+=1
        return inserted

    def finalize_clv(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        bets=self.db.fetchall(
            """
            SELECT b.*,p.home_team,p.away_team,p.commence_time
            FROM football_predictive_bets b
            JOIN football_predictive_predictions p ON p.id=b.prediction_id
            WHERE b.clv_pct IS NULL
            ORDER BY b.id
            """
        )
        updated=0
        for bet in bets:
            start=parse_iso(bet["commence_time"])
            if start>now:
                continue
            waves=self.db.fetchall(
                """
                SELECT DISTINCT captured_at FROM odds_snapshots
                WHERE event_id=? AND captured_at<=? AND captured_at>=?
                  AND market_key='h2h' ORDER BY captured_at DESC
                """,
                (bet["event_id"],start.isoformat(),bet["created_at"]),
            )
            close=None
            for w in waves:
                wave=w["captured_at"]
                rows=self.db.fetchall(
                    """
                    SELECT * FROM odds_snapshots
                    WHERE event_id=? AND captured_at=? AND bookmaker_key=?
                      AND market_key='h2h'
                    """,
                    (bet["event_id"],wave,bet["bookmaker_key"]),
                )
                valid=_valid_three_way_wave(
                    rows,bet["home_team"],bet["away_team"],
                    (bet["bookmaker_key"],),
                )
                row=(valid.get(bet["bookmaker_key"]) or {}).get(bet["selection"])
                if row:
                    close=row
                    break
            if not close:
                continue
            close_at=parse_iso(close["captured_at"])
            mins=max(0.0,(start-close_at).total_seconds()/60.0)
            closing=float(close["price"])
            self.db.execute(
                """
                UPDATE football_predictive_bets
                SET closing_odds=?,clv_pct=?,closing_observed_at=?,
                    closing_minutes_before_kickoff=?,clv_quality=?
                WHERE id=?
                """,
                (
                    closing,clv_pct(float(bet["offered_odds"]),closing),
                    close["captured_at"],mins,clv_quality(mins),bet["id"],
                ),
            )
            updated+=1
        return updated

    def finalize_prediction_market_close(self, now: Optional[datetime] = None) -> int:
        now=now or datetime.now(timezone.utc)
        preds=self.db.fetchall(
            """
            SELECT * FROM football_predictive_predictions
            WHERE commence_time<=? AND closing_market_observed_at IS NULL
            ORDER BY id
            """,
            (now.isoformat(),),
        )
        updated=0
        for pred in preds:
            rows=self.db.fetchall(
                """
                SELECT * FROM consensus_snapshots
                WHERE event_id=? AND market_key='h2h' AND captured_at<=?
                ORDER BY captured_at DESC,id DESC
                """,
                (pred["event_id"],pred["commence_time"]),
            )
            required={pred["home_team"],"Draw",pred["away_team"]}
            grouped={}
            for row in rows:
                selection=str(row["selection"])
                if selection not in required:
                    continue
                grouped.setdefault(str(row["captured_at"]),{})[selection]=float(
                    row["fair_probability"]
                )
            chosen_at=None
            by_selection={}
            for captured_at in sorted(grouped.keys(),reverse=True):
                candidate=grouped[captured_at]
                if set(candidate.keys())==required:
                    chosen_at=captured_at
                    by_selection=candidate
                    break
            if len(by_selection)!=3 or not chosen_at:
                continue
            mins=max(
                0.0,
                (parse_iso(pred["commence_time"])-parse_iso(chosen_at)).total_seconds()/60.0
            )
            self.db.execute(
                """
                UPDATE football_predictive_predictions
                SET closing_home_probability=?,closing_draw_probability=?,
                    closing_away_probability=?,closing_market_observed_at=?,
                    closing_market_quality=?
                WHERE id=?
                """,
                (
                    by_selection[pred["home_team"]],by_selection["Draw"],
                    by_selection[pred["away_team"]],chosen_at,clv_quality(mins),
                    pred["id"],
                ),
            )
            updated+=1
        return updated

    def settle(self) -> Dict[str,int]:
        predictions=self.db.fetchall(
            """
            SELECT p.*,r.home_score,r.away_score
            FROM football_predictive_predictions p
            JOIN event_results r ON r.event_id=p.event_id
            WHERE p.actual_outcome IS NULL
            ORDER BY p.id
            """
        )
        pred_settled=0
        for pred in predictions:
            hs=int(pred["home_score"]);as_=int(pred["away_score"])
            outcome=pred["home_team"] if hs>as_ else (
                pred["away_team"] if as_>hs else "Draw"
            )
            probs={
                pred["home_team"]:float(pred["home_probability"]),
                "Draw":float(pred["draw_probability"]),
                pred["away_team"]:float(pred["away_probability"]),
            }
            actual=probs[outcome]
            brier=sum(
                (p-(1.0 if sel==outcome else 0.0))**2
                for sel,p in probs.items()
            )
            logloss=-math.log(max(1e-12,actual))

            closing_brier=None
            advantage=None
            if all(
                pred.get(k) is not None
                for k in (
                    "closing_home_probability","closing_draw_probability",
                    "closing_away_probability",
                )
            ):
                cp={
                    pred["home_team"]:float(pred["closing_home_probability"]),
                    "Draw":float(pred["closing_draw_probability"]),
                    pred["away_team"]:float(pred["closing_away_probability"]),
                }
                closing_brier=sum(
                    (p-(1.0 if sel==outcome else 0.0))**2
                    for sel,p in cp.items()
                )
                advantage=closing_brier-brier

            self.db.execute(
                """
                UPDATE football_predictive_predictions
                SET actual_outcome=?,brier_score=?,log_loss=?,
                    closing_market_brier=?,model_brier_advantage=?,settled_at=?
                WHERE id=?
                """,
                (
                    outcome,brier,logloss,closing_brier,advantage,
                    utc_now_iso(),pred["id"],
                ),
            )
            pred_settled+=1

        bets=self.db.fetchall(
            """
            SELECT b.*,p.home_team,p.away_team,r.home_score,r.away_score
            FROM football_predictive_bets b
            JOIN football_predictive_predictions p ON p.id=b.prediction_id
            JOIN event_results r ON r.event_id=b.event_id
            WHERE b.status='OPEN'
            ORDER BY b.id
            """
        )
        bet_settled=0
        for bet in bets:
            hs=int(bet["home_score"]);as_=int(bet["away_score"])
            outcome=bet["home_team"] if hs>as_ else (
                bet["away_team"] if as_>hs else "Draw"
            )
            result="WIN" if str(bet["selection"])==str(outcome) else "LOSS"
            gross=float(bet["offered_odds"])-1.0 if result=="WIN" else -1.0
            rate,commission,net=commission_adjusted_pnl(
                gross,str(bet["bookmaker_key"])
            )
            self.db.execute(
                """
                UPDATE football_predictive_bets
                SET status='SETTLED',result=?,pnl_units=?,
                    commission_rate_pct=?,commission_units=?,net_pnl_units=?,
                    settled_at=? WHERE id=?
                """,
                (
                    result,gross,rate,commission,net,utc_now_iso(),bet["id"],
                ),
            )
            bet_settled+=1
        return {"predictions":pred_settled,"bets":bet_settled}

    def maintenance(self, now: Optional[datetime]=None) -> Dict[str,Any]:
        now=now or datetime.now(timezone.utc)
        internal=self.sync_internal_results()
        bootstrap=self.bootstrap_historical_data(now)
        forecasts=self.freeze_due_predictions(now)
        market_predictions=self.ensure_market_predictions()
        shadows=self.evaluate_predictions()
        market_shadows=self.evaluate_market_predictions(now)
        tracked=self.track_prices(now)
        market_tracked=self.track_market_prices(now)
        clv=self.finalize_clv(now)
        market_clv=self.finalize_market_clv(now)
        market_close=self.finalize_prediction_market_close(now)
        settled=self.settle()
        market_settled=self.settle_market_predictions()
        return {
            "internal_training_rows":internal,
            "bootstrap":bootstrap,
            "forecasts":forecasts,
            "derived_market_predictions_created":market_predictions,
            "shadows_created":shadows,
            "derived_market_shadows_created":market_shadows,
            "price_observations":tracked,
            "derived_market_price_observations":market_tracked,
            "clv_finalized":clv,
            "derived_market_clv_finalized":market_clv,
            "market_close_finalized":market_close,
            "settled":settled,
            "derived_market_settled":market_settled,
        }

    def one_cycle(self, now: Optional[datetime]=None) -> Dict[str,Any]:
        if not self.enabled:
            return {"enabled":False}
        return {"enabled":True,**self.maintenance(now)}



def predictive_bootstrap_status(db: Database) -> Dict[str,Any]:
    rows = db.fetchall(
        """
        SELECT * FROM football_predictive_source_state
        ORDER BY source_key
        """
    )
    attempted = len(rows)
    successful = sum(1 for r in rows if r.get("last_success_at"))
    failed = sum(
        1 for r in rows
        if r.get("last_attempt_at") and not r.get("last_success_at")
    )
    imported = sum(int(r.get("rows_imported") or 0) for r in rows)
    latest_attempt = max(
        (str(r["last_attempt_at"]) for r in rows if r.get("last_attempt_at")),
        default=None,
    )
    latest_success = max(
        (str(r["last_success_at"]) for r in rows if r.get("last_success_at")),
        default=None,
    )
    failures = [
        {
            "source_key":r["source_key"],
            "last_attempt_at":r.get("last_attempt_at"),
            "last_error":r.get("last_error"),
        }
        for r in rows if r.get("last_error")
    ]
    return {
        "sources_attempted":attempted,
        "sources_successful":successful,
        "sources_failed":failed,
        "rows_imported":imported,
        "latest_attempt_at":latest_attempt,
        "latest_success_at":latest_success,
        "failures":failures[-10:],
    }

def predictive_scoreboard(db: Database) -> Dict[str,Any]:
    preds=db.fetchall(
        "SELECT * FROM football_predictive_predictions ORDER BY id"
    )
    settled_preds=[p for p in preds if p.get("brier_score") is not None]

    h2h_bets=db.fetchall(
        "SELECT * FROM football_predictive_bets ORDER BY id"
    )
    market_bets=db.fetchall(
        "SELECT * FROM football_predictive_market_bets ORDER BY id"
    )
    all_bets=h2h_bets+market_bets
    settled_bets=[b for b in all_bets if b.get("pnl_units") is not None]
    headline=[
        b for b in all_bets
        if b.get("clv_pct") is not None and b.get("clv_quality") in {"A","B"}
    ]
    clvs=[float(b["clv_pct"]) for b in headline]
    net=sum(float(b.get("net_pnl_units") or 0.0) for b in settled_bets)
    gross=sum(float(b.get("pnl_units") or 0.0) for b in settled_bets)
    strong=sum(
        1 for b in all_bets if int(b.get("strong_candidate") or 0)==1
    )
    source_rows=int((db.fetchone(
        "SELECT COUNT(*) AS n FROM football_predictive_training_matches"
    ) or {}).get("n") or 0)
    briers=[float(p["brier_score"]) for p in settled_preds]
    logs=[float(p["log_loss"]) for p in settled_preds]
    advantages=[
        float(p["model_brier_advantage"])
        for p in settled_preds
        if p.get("model_brier_advantage") is not None
        and p.get("closing_market_quality") in {"A","B"}
    ]
    accuracy=sum(
        1 for p in settled_preds
        if max(
            (
                (float(p["home_probability"]),p["home_team"]),
                (float(p["draw_probability"]),"Draw"),
                (float(p["away_probability"]),p["away_team"]),
            )
        )[1] == p["actual_outcome"]
    )
    btts_bets=[b for b in market_bets if b.get("market_key")=="btts"]
    totals_bets=[b for b in market_bets if b.get("market_key")=="totals"]
    market_predictions=db.fetchall(
        "SELECT * FROM football_predictive_market_predictions ORDER BY id"
    )
    derived_cases={
        (
            int(x["prediction_id"]),str(x["market_key"]),str(x["line_key"])
        )
        for x in market_predictions
    }
    settled_derived=[
        x for x in market_predictions if x.get("brier_score") is not None
    ]
    derived_briers=[float(x["brier_score"]) for x in settled_derived]
    return {
        "training_matches":source_rows,
        "predictions":len(preds),
        "settled_predictions":len(settled_preds),
        "prediction_accuracy_pct":(
            accuracy/len(settled_preds)*100.0 if settled_preds else None
        ),
        "avg_brier_score":mean(briers) if briers else None,
        "avg_log_loss":mean(logs) if logs else None,
        "closing_market_comparison_samples":len(advantages),
        "avg_model_brier_advantage":mean(advantages) if advantages else None,
        "derived_market_cases":len(derived_cases),
        "settled_derived_market_selection_predictions":len(settled_derived),
        "avg_derived_market_brier":(
            mean(derived_briers) if derived_briers else None
        ),
        "bets":len(all_bets),
        "h2h_bets":len(h2h_bets),
        "btts_bets":len(btts_bets),
        "totals_bets":len(totals_bets),
        "strong_candidates":strong,
        "settled_bets":len(settled_bets),
        "gross_pnl_units":gross,
        "net_pnl_units":net,
        "net_roi_pct":net/len(settled_bets)*100.0 if settled_bets else None,
        "clv_samples":len(clvs),
        "avg_clv_pct":mean(clvs) if clvs else None,
        "beat_close_pct":(
            sum(1 for x in clvs if x>0)/len(clvs)*100.0 if clvs else None
        ),
    }


def predictive_market_summary(db: Database) -> List[Dict[str,Any]]:
    predictions=db.fetchall(
        "SELECT * FROM football_predictive_market_predictions ORDER BY id"
    )
    bets=db.fetchall(
        "SELECT * FROM football_predictive_market_bets ORDER BY id"
    )
    keys=sorted({
        (str(x["market_key"]),str(x["line_key"])) for x in predictions
    })
    out=[]
    for market,line in keys:
        ps=[
            x for x in predictions
            if str(x["market_key"])==market and str(x["line_key"])==line
        ]
        bs=[
            x for x in bets
            if str(x["market_key"])==market and str(x["line_key"])==line
        ]
        settled_p=[x for x in ps if x.get("brier_score") is not None]
        headline=[
            x for x in bs
            if x.get("clv_pct") is not None and x.get("clv_quality") in {"A","B"}
        ]
        settled_b=[x for x in bs if x.get("net_pnl_units") is not None]
        out.append({
            "market_key":market,
            "line_key":line,
            "cases":len({
                int(x["prediction_id"]) for x in ps
            }),
            "settled_selection_predictions":len(settled_p),
            "avg_brier":(
                mean(float(x["brier_score"]) for x in settled_p)
                if settled_p else None
            ),
            "bets":len(bs),
            "settled_bets":len(settled_b),
            "ab_clv_samples":len(headline),
            "avg_ab_clv_pct":(
                mean(float(x["clv_pct"]) for x in headline)
                if headline else None
            ),
            "net_pnl_units":sum(
                float(x.get("net_pnl_units") or 0.0) for x in settled_b
            ),
            "net_roi_pct":(
                sum(float(x.get("net_pnl_units") or 0.0) for x in settled_b)
                / len(settled_b)*100.0 if settled_b else None
            ),
        })
    return out


def predictive_market_funnel(db: Database) -> List[Dict[str,Any]]:
    return db.fetchall(
        """
        SELECT market_key,line_key,decision,reason,COUNT(*) AS count
        FROM football_predictive_market_evaluations
        GROUP BY market_key,line_key,decision,reason
        ORDER BY market_key,line_key,count DESC,decision,reason
        """
    )


def latest_predictive_market_bets(
    db: Database, limit: int=100
) -> List[Dict[str,Any]]:
    return db.fetchall(
        """
        SELECT b.*,p.league,p.home_team,p.away_team,p.commence_time,
               p.expected_home_goals,p.expected_away_goals,
               (SELECT o.move_vs_entry_pct
                FROM football_predictive_market_price_observations o
                WHERE o.predictive_bet_id=b.id
                ORDER BY o.source_snapshot_at DESC LIMIT 1) AS latest_move_pct
        FROM football_predictive_market_bets b
        JOIN football_predictive_predictions p ON p.id=b.prediction_id
        ORDER BY b.id DESC LIMIT ?
        """,
        (limit,),
    )


def predictive_funnel(db: Database) -> List[Dict[str,Any]]:
    return db.fetchall(
        """
        SELECT decision,reason,COUNT(*) AS count
        FROM football_predictive_evaluations
        GROUP BY decision,reason
        ORDER BY count DESC,decision,reason
        """
    )


def predictive_league_summary(db: Database) -> List[Dict[str,Any]]:
    return db.fetchall(
        """
        SELECT sport_key,league,
               COUNT(*) AS predictions,
               SUM(CASE WHEN brier_score IS NOT NULL THEN 1 ELSE 0 END) AS settled,
               AVG(brier_score) AS avg_brier,
               AVG(model_brier_advantage) AS avg_brier_advantage
        FROM football_predictive_predictions
        GROUP BY sport_key,league
        ORDER BY predictions DESC,league
        """
    )


def latest_predictive_bets(db: Database, limit: int=100) -> List[Dict[str,Any]]:
    return db.fetchall(
        """
        SELECT b.*,p.league,p.home_team,p.away_team,p.commence_time,
               p.expected_home_goals,p.expected_away_goals,
               (SELECT o.move_vs_entry_pct
                FROM football_predictive_price_observations o
                WHERE o.predictive_bet_id=b.id
                ORDER BY o.source_snapshot_at DESC LIMIT 1) AS latest_move_pct
        FROM football_predictive_bets b
        JOIN football_predictive_predictions p ON p.id=b.prediction_id
        ORDER BY b.id DESC LIMIT ?
        """,
        (limit,),
    )
