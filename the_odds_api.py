from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, Optional, Tuple
import requests


BASE_URL = "https://api.the-odds-api.com/v4"


def _historical_iso(value: str) -> str:
    """Normalize historical snapshot timestamps to the provider's canonical UTC Z form."""
    raw = str(value or "").strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        return dt.isoformat(timespec="seconds").replace("+00:00", "Z")
    except Exception:
        return raw


@dataclass
class ApiResult:
    data: object
    remaining: Optional[int]
    used: Optional[int]
    last_cost: Optional[int]


class TheOddsApi:
    def __init__(self, api_key: str, session=None, timeout: int = 20):
        self.api_key = api_key
        self.session = session or requests.Session()
        self.timeout = timeout

    def _headers(self, response) -> Tuple[Optional[int], Optional[int], Optional[int]]:
        def to_int(name: str):
            raw = response.headers.get(name)
            try:
                return int(raw) if raw is not None else None
            except (TypeError, ValueError):
                return None
        return (
            to_int("x-requests-remaining"),
            to_int("x-requests-used"),
            to_int("x-requests-last"),
        )

    def _get(self, path: str, params: Dict[str, str]) -> ApiResult:
        if not self.api_key:
            raise RuntimeError("ODDS_API_KEY is not configured")
        params = dict(params)
        params["apiKey"] = self.api_key
        response = self.session.get(
            f"{BASE_URL}{path}", params=params, timeout=self.timeout
        )
        response.raise_for_status()
        remaining, used, last_cost = self._headers(response)
        return ApiResult(response.json(), remaining, used, last_cost)

    def sports(self) -> ApiResult:
        # The sports endpoint is quota-free and is used by Tennis Shadow to
        # discover currently active tournament keys.
        return self._get("/sports/", {})

    def quota_probe(self) -> ApiResult:
        return self.sports()

    def events(self, sport_key: str) -> ApiResult:
        return self._get(f"/sports/{sport_key}/events", {"dateFormat": "iso"})

    def sport_odds(
        self,
        sport_key: str,
        region: str,
        markets: Iterable[str],
        bookmaker_keys: Iterable[str] = (),
    ) -> ApiResult:
        """All upcoming/live events for one competition in one market call."""
        params = {
            "markets": ",".join(markets),
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        books = [str(x) for x in bookmaker_keys if str(x)]
        if books:
            params["bookmakers"] = ",".join(books)
        else:
            params["regions"] = region
        return self._get(f"/sports/{sport_key}/odds", params)

    def event_odds(
        self,
        sport_key: str,
        event_id: str,
        region: str,
        markets: Iterable[str],
        bookmaker_keys: Iterable[str] = (),
    ) -> ApiResult:
        params = {
            "markets": ",".join(markets),
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        books = [str(x) for x in bookmaker_keys if str(x)]
        if books:
            params["bookmakers"] = ",".join(books)
        else:
            params["regions"] = region
        return self._get(
            f"/sports/{sport_key}/events/{event_id}/odds",
            params,
        )

    def historical_events(
        self,
        sport_key: str,
        date: str,
        *,
        event_ids: Iterable[str] = (),
        commence_time_from: Optional[str] = None,
        commence_time_to: Optional[str] = None,
    ) -> ApiResult:
        # The provider's historical-events endpoint currently accepts only the
        # historical snapshot `date` (plus apiKey). Parameters that are valid on
        # current event/odds endpoints such as dateFormat, eventIds and
        # commenceTimeFrom/To trigger HTTP 422 here. Keep the keyword arguments
        # in the method signature for backward compatibility, but deliberately do
        # not send them. The caller filters/matches the returned event list locally.
        params = {"date": _historical_iso(date)}
        return self._get(f"/historical/sports/{sport_key}/events", params)

    def historical_event_odds(
        self,
        sport_key: str,
        event_id: str,
        region: str,
        markets: Iterable[str],
        date: str,
        bookmaker_keys: Iterable[str] = (),
    ) -> ApiResult:
        params = {
            "markets": ",".join(markets),
            "oddsFormat": "decimal",
            "dateFormat": "iso",
            "date": _historical_iso(date),
        }
        books = [str(x) for x in bookmaker_keys if str(x)]
        if books:
            params["bookmakers"] = ",".join(books)
        else:
            params["regions"] = region
        return self._get(
            f"/historical/sports/{sport_key}/events/{event_id}/odds", params
        )

    def scores(
        self,
        sport_key: str,
        *,
        event_ids: Iterable[str] = (),
        days_from: int = 1,
    ) -> ApiResult:
        params = {
            "dateFormat": "iso",
            "daysFrom": str(int(days_from)),
        }
        ids = [str(x) for x in event_ids if str(x)]
        if ids:
            params["eventIds"] = ",".join(ids)
        return self._get(f"/sports/{sport_key}/scores/", params)
