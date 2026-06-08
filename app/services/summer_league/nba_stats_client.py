"""Client utilities for the NBA Stats API."""

from __future__ import annotations

import time
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any, Callable, Mapping

from curl_cffi import requests as cffi_requests

NBA_API_ROOT = "https://stats.nba.com/stats"

NBA_API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "Accept-Language": "en-US,en;q=0.9",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}


class NBAStatsAPIError(RuntimeError):
    """Raised when NBA Stats returns an unusable response."""


@dataclass(frozen=True)
class NBAStatsResultSet:
    """One normalized NBA Stats result set."""

    name: str
    headers: list[str]
    rows: list[list[Any]]


def extract_result_sets(payload: Mapping[str, Any]) -> list[NBAStatsResultSet]:
    """Normalize NBA Stats result-set shapes.

    Args:
        payload: Raw NBA Stats JSON payload.

    Returns:
        Result sets normalized to ``NBAStatsResultSet`` instances.
    """
    raw_sets = payload.get("resultSets")
    if isinstance(raw_sets, Mapping):
        sets: list[Any] = [raw_sets]
    elif isinstance(raw_sets, list):
        sets = raw_sets
    else:
        single = payload.get("resultSet")
        sets = [single] if isinstance(single, Mapping) else []

    result_sets: list[NBAStatsResultSet] = []
    for raw_set in sets:
        if not isinstance(raw_set, Mapping):
            continue
        raw_headers = raw_set.get("headers") or []
        raw_rows = raw_set.get("rowSet") or []
        headers = [str(header) for header in raw_headers]
        rows = [list(row) for row in raw_rows if isinstance(row, list)]
        result_sets.append(
            NBAStatsResultSet(
                name=str(raw_set.get("name") or "?"),
                headers=headers,
                rows=rows,
            )
        )
    return result_sets


def result_set_row_counts(payload: Mapping[str, Any]) -> dict[str, int]:
    """Return row counts keyed by result-set name."""
    return {
        result_set.name: len(result_set.rows)
        for result_set in extract_result_sets(payload)
    }


class NBAStatsClient:
    """Small NBA Stats API client using Chrome TLS impersonation.

    Plain HTTP clients are tarpitted by ``stats.nba.com``. ``curl_cffi`` with
    ``impersonate="chrome"`` reproduces a browser-like TLS fingerprint.
    """

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_delay_seconds: float = 2.0,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initialize the client.

        Args:
            timeout: Request timeout in seconds for the owned session.
            max_retries: Number of retry attempts after the first request.
            retry_delay_seconds: Base delay between retry attempts.
            session: Optional injected session for tests.
            sleep: Injectable sleep function for tests.
        """
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self._owns_session = session is None
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.sleep = sleep
        self._session = session or cffi_requests.Session(
            headers=NBA_API_HEADERS,
            impersonate="chrome",
            timeout=timeout,
        )

    def __enter__(self) -> "NBAStatsClient":
        """Return this client for context-manager usage."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Close the owned HTTP session."""
        self.close()

    def close(self) -> None:
        """Close the underlying session when this client owns it."""
        if self._owns_session:
            close = getattr(self._session, "close", None)
            if callable(close):
                close()

    def fetch_json(self, endpoint: str, params: Mapping[str, str]) -> dict[str, Any]:
        """Fetch one NBA Stats JSON endpoint.

        Args:
            endpoint: Endpoint name, such as ``"leaguegamelog"``.
            params: Query parameters.

        Returns:
            Decoded JSON payload.

        Raises:
            NBAStatsAPIError: If the response status is not successful or JSON
                decoding fails.
        """
        clean_endpoint = endpoint.strip("/")
        url = f"{NBA_API_ROOT}/{clean_endpoint}"
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            try:
                response = self._session.get(url, params=dict(params))
            except Exception as exc:
                error = NBAStatsAPIError(
                    f"NBA Stats request failed for {clean_endpoint}: "
                    f"{type(exc).__name__}"
                )
                if attempt < self.max_retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise error from exc

            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code >= 400:
                error = NBAStatsAPIError(
                    f"NBA Stats request failed for {clean_endpoint}: "
                    f"HTTP {status_code}"
                )
                if _is_retryable_status(status_code) and attempt < self.max_retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise error
            break
        else:  # pragma: no cover - loop always exits by break or raise.
            raise NBAStatsAPIError(f"NBA Stats request failed for {clean_endpoint}")

        try:
            payload = response.json()
        except (JSONDecodeError, ValueError) as exc:
            raise NBAStatsAPIError(
                f"NBA Stats request failed for {clean_endpoint}: non-JSON response"
            ) from exc
        if not isinstance(payload, dict):
            raise NBAStatsAPIError(
                f"NBA Stats request failed for {clean_endpoint}: unexpected JSON shape"
            )
        return payload

    def _sleep_before_retry(self, attempt: int) -> None:
        if self.retry_delay_seconds <= 0:
            return
        self.sleep(self.retry_delay_seconds * (attempt + 1))


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599
