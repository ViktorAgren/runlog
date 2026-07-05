"""Unit tests for the rate-limit-aware Strava client."""

from __future__ import annotations

from typing import Any

import pytest
import requests

from runlog.sources.strava.client import StravaClient


class _FakeResponse:
    def __init__(
        self,
        payload: Any,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


def _client_with_responses(
    responses: list[_FakeResponse],
) -> tuple[StravaClient, list[float]]:
    slept: list[float] = []
    client = StravaClient("token", sleep=slept.append)
    queue = iter(responses)

    def fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        return next(queue)

    client._session.get = fake_get  # type: ignore[method-assign, assignment]
    return client, slept


def test_get_json_returns_payload_without_sleeping() -> None:
    client, slept = _client_with_responses([_FakeResponse({"ok": True})])
    assert (client.get_json("/athlete"), slept) == ({"ok": True}, [])


def test_get_json_retries_once_after_429() -> None:
    client, slept = _client_with_responses(
        [_FakeResponse(None, status_code=429), _FakeResponse({"ok": True})]
    )
    assert (client.get_json("/athlete"), len(slept)) == ({"ok": True}, 1)


def test_get_json_throttles_when_window_nearly_exhausted() -> None:
    client, slept = _client_with_responses(
        [
            _FakeResponse(
                {"ok": True},
                headers={
                    "X-RateLimit-Limit": "100,1000",
                    "X-RateLimit-Usage": "99,500",
                },
            )
        ]
    )
    client.get_json("/athlete")
    assert len(slept) == 1


def test_get_json_raises_on_persistent_error() -> None:
    client, _ = _client_with_responses([_FakeResponse(None, status_code=500)])
    with pytest.raises(requests.HTTPError):
        client.get_json("/athlete")
