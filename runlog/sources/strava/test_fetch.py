"""Unit tests for Strava JSON parsers and pagination."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from runlog.domain import Activity, Lap, SourceId, StreamPoint
from runlog.sources.strava import fetch

_DETAIL: dict[str, Any] = {
    "id": 987654321,
    "name": "Interval Session",
    "sport_type": "Run",
    "type": "Run",
    "start_date": "2026-06-03T05:30:00Z",
    "timezone": "(GMT+01:00) Europe/Stockholm",
    "elapsed_time": 1800,
    "moving_time": 1700,
    "distance": 5000.0,
    "average_heartrate": 155.0,
    "max_heartrate": 178.0,
    "average_cadence": 85.0,
    "total_elevation_gain": 30.0,
    "calories": 420.0,
    "laps": [
        {
            "elapsed_time": 200,
            "moving_time": 200,
            "distance": 800.0,
            "average_heartrate": 165.0,
        },
        {
            "elapsed_time": 120,
            "moving_time": 120,
            "distance": 400.0,
            "average_heartrate": 150.0,
        },
    ],
}

_STREAMS: dict[str, Any] = {
    "time": {"data": [0, 1, 2]},
    "distance": {"data": [0.0, 3.0, 6.5]},
    "latlng": {"data": [[59.3, 18.0], [59.30001, 18.00001], [59.30002, 18.00002]]},
    "altitude": {"data": [10.0, 10.5, 11.0]},
    "heartrate": {"data": [120, 122, 125]},
    "cadence": {"data": [80, 81, 82]},
    "velocity_smooth": {"data": [0.0, 3.0, 3.5]},
    "watts": {"data": [None, 200, 210]},
}


def test_parse_activity_maps_summary_fields() -> None:
    assert fetch.parse_activity(_DETAIL) == Activity(
        source="strava",
        source_id=SourceId("987654321"),
        sport_type="Run",
        start_time_utc=datetime(2026, 6, 3, 5, 30, tzinfo=UTC),
        tz="Europe/Stockholm",
        elapsed_s=1800,
        moving_s=1700,
        distance_m=5000.0,
        avg_hr=155.0,
        max_hr=178.0,
        avg_pace_s_per_km=340.0,
        avg_cadence=85.0,
        elevation_gain_m=30.0,
        calories=420.0,
        name="Interval Session",
    )


def test_parse_laps_indexes_and_computes_pace() -> None:
    assert fetch.parse_laps(_DETAIL) == (
        Lap(
            lap_index=0,
            elapsed_s=200,
            distance_m=800.0,
            avg_hr=165.0,
            avg_pace_s_per_km=250.0,
        ),
        Lap(
            lap_index=1,
            elapsed_s=120,
            distance_m=400.0,
            avg_hr=150.0,
            avg_pace_s_per_km=300.0,
        ),
    )


def test_parse_streams_zips_series_by_index() -> None:
    assert fetch.parse_streams(_STREAMS) == (
        StreamPoint(0, 0.0, 59.3, 18.0, 10.0, 120, 80, 0.0, None),
        StreamPoint(1, 3.0, 59.30001, 18.00001, 10.5, 122, 81, 3.0, 200),
        StreamPoint(2, 6.5, 59.30002, 18.00002, 11.0, 125, 82, 3.5, 210),
    )


class _FakeClient:
    """Returns queued JSON payloads and records the params of each call."""

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self._pages = pages
        self.calls: list[dict[str, Any] | None] = []

    def get_json(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append(params)
        index = (params or {}).get("page", 1) - 1
        return self._pages[index] if index < len(self._pages) else []


def test_iter_activity_summaries_follows_pagination() -> None:
    full_page = [{"id": i} for i in range(100)]
    last_page = [{"id": 100}, {"id": 101}]
    client = _FakeClient([full_page, last_page])

    summaries = list(fetch.iter_activity_summaries(client, after_epoch=123))  # type: ignore[arg-type]

    assert (len(summaries), client.calls[0]) == (
        102,
        {"per_page": 100, "page": 1, "after": 123},
    )
