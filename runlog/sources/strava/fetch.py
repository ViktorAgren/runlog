"""Fetch and parse Strava activities, laps, and streams.

The parsers (``parse_activity``/``parse_laps``/``parse_streams``) are pure and
operate on decoded JSON, so they are unit-tested without any network. The
``iter_*``/``fetch_*`` helpers layer paging and endpoint paths on top of a
``StravaClient``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from runlog.domain import Activity, Lap, SourceId, StreamPoint

if TYPE_CHECKING:
    from collections.abc import Iterator

    from runlog.sources.strava.client import StravaClient

_PER_PAGE = 100
# Stream types requested; key_by_type returns them under these names.
_STREAM_KEYS = "time,distance,latlng,altitude,heartrate,cadence,velocity_smooth,watts"


def _parse_start(value: str) -> datetime:
    """Parse a Strava UTC timestamp such as ``2026-06-01T07:30:00Z``."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _tz_name(timezone_field: str | None) -> str | None:
    """Extract the IANA name from ``(GMT+01:00) Europe/Stockholm``."""
    if not timezone_field:
        return None
    return timezone_field.rsplit(" ", 1)[-1]


def _pace_s_per_km(moving_s: int | None, distance_m: float | None) -> float | None:
    if not moving_s or not distance_m or distance_m <= 0:
        return None
    return moving_s / (distance_m / 1000)


def parse_activity(data: dict[str, Any]) -> Activity:
    """Normalize a summary or detailed activity JSON object into an Activity."""
    moving_s = data.get("moving_time")
    distance_m = data.get("distance")
    return Activity(
        source="strava",
        source_id=SourceId(str(data["id"])),
        sport_type=data.get("sport_type") or data.get("type") or "Unknown",
        start_time_utc=_parse_start(data["start_date"]),
        tz=_tz_name(data.get("timezone")),
        elapsed_s=data.get("elapsed_time"),
        moving_s=moving_s,
        distance_m=distance_m,
        avg_hr=data.get("average_heartrate"),
        max_hr=data.get("max_heartrate"),
        avg_pace_s_per_km=_pace_s_per_km(moving_s, distance_m),
        avg_cadence=data.get("average_cadence"),
        elevation_gain_m=data.get("total_elevation_gain"),
        calories=data.get("calories"),
        name=data.get("name"),
    )


def parse_laps(data: dict[str, Any]) -> tuple[Lap, ...]:
    """Extract laps from a detailed activity (empty for activities without any)."""
    laps: list[Lap] = []
    for index, lap in enumerate(data.get("laps") or []):
        distance_m = lap.get("distance")
        moving_s = lap.get("moving_time")
        laps.append(
            Lap(
                lap_index=index,
                elapsed_s=lap.get("elapsed_time"),
                distance_m=distance_m,
                avg_hr=lap.get("average_heartrate"),
                avg_pace_s_per_km=_pace_s_per_km(moving_s, distance_m),
            )
        )
    return tuple(laps)


def parse_streams(data: dict[str, Any]) -> tuple[StreamPoint, ...]:
    """Zip a ``key_by_type`` streams payload into per-point ``StreamPoint`` rows."""

    def series(key: str) -> list[Any]:
        entry = data.get(key)
        return entry["data"] if entry else []

    times = series("time")
    distance = series("distance")
    latlng = series("latlng")
    altitude = series("altitude")
    heartrate = series("heartrate")
    cadence = series("cadence")
    velocity = series("velocity_smooth")
    watts = series("watts")

    count = len(times) or max(
        (len(s) for s in (distance, latlng, altitude, heartrate)), default=0
    )

    def at(series_data: list[Any], i: int) -> Any:
        return series_data[i] if i < len(series_data) else None

    points: list[StreamPoint] = []
    for i in range(count):
        pair = at(latlng, i)
        points.append(
            StreamPoint(
                offset_s=int(at(times, i) or i),
                distance_m=at(distance, i),
                lat=pair[0] if pair else None,
                lng=pair[1] if pair else None,
                altitude_m=at(altitude, i),
                hr=at(heartrate, i),
                cadence=at(cadence, i),
                velocity_mps=at(velocity, i),
                watts=at(watts, i),
            )
        )
    return tuple(points)


def iter_activity_summaries(
    client: StravaClient, after_epoch: int | None = None
) -> Iterator[dict[str, Any]]:
    """Yield summary activities newest-page-first, following pagination."""
    page = 1
    while True:
        params: dict[str, Any] = {"per_page": _PER_PAGE, "page": page}
        if after_epoch is not None:
            params["after"] = after_epoch
        batch: list[dict[str, Any]] = client.get_json("/athlete/activities", params)
        if not batch:
            return
        yield from batch
        if len(batch) < _PER_PAGE:
            return
        page += 1


def fetch_detail(client: StravaClient, activity_id: SourceId) -> dict[str, Any]:
    """Fetch a detailed activity (includes calories and laps)."""
    detail: dict[str, Any] = client.get_json(f"/activities/{activity_id}")
    return detail


def fetch_streams(client: StravaClient, activity_id: SourceId) -> dict[str, Any]:
    """Fetch the per-point streams for an activity, keyed by type."""
    streams: dict[str, Any] = client.get_json(
        f"/activities/{activity_id}/streams",
        {"keys": _STREAM_KEYS, "key_by_type": "true"},
    )
    return streams
