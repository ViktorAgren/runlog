"""Rate-limit-aware HTTP client for the Strava API.

Strava enforces a short-window (15 min) and a daily quota, reported on every
response via ``X-RateLimit-Limit``/``X-RateLimit-Usage``. This client backs off
when the short window is nearly exhausted and retries once on a ``429``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from collections.abc import Mapping

BASE_URL = "https://www.strava.com/api/v3"
# Fraction of the 15-minute quota at which we pause until the next window.
_THROTTLE_AT = 0.95
_WINDOW_S = 900


def _short_window_usage(headers: Mapping[str, str]) -> tuple[int, int] | None:
    """Return (used, limit) for the 15-minute window, if the headers carry it."""
    limit = headers.get("X-RateLimit-Limit")
    usage = headers.get("X-RateLimit-Usage")
    if not limit or not usage:
        return None
    try:
        return int(usage.split(",")[0]), int(limit.split(",")[0])
    except (ValueError, IndexError):
        return None


class StravaClient:
    """Minimal GET client that authenticates and respects rate limits."""

    def __init__(
        self,
        access_token: str,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session = session or requests.Session()
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._sleep = sleep

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``path`` and return parsed JSON, retrying once on a 429."""
        response = self._request(path, params)
        if response.status_code == 429:
            self._sleep(_WINDOW_S)
            response = self._request(path, params)
        response.raise_for_status()
        self._throttle_if_needed(response.headers)
        return response.json()

    def _request(self, path: str, params: dict[str, Any] | None) -> requests.Response:
        return self._session.get(
            f"{BASE_URL}{path}",
            headers=self._headers,
            params=params,
            timeout=30,
        )

    def _throttle_if_needed(self, headers: Mapping[str, str]) -> None:
        usage = _short_window_usage(headers)
        if usage is None:
            return
        used, limit = usage
        if limit > 0 and used / limit >= _THROTTLE_AT:
            self._sleep(_WINDOW_S)
