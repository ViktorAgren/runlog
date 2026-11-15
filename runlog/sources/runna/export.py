"""Parse Runna's ``activities.json`` into activity summaries.

Only native recordings (phone/watch) are returned; ``recordType == "STRAVA"``
rows are echoes of Strava activities we already ingest directly, and carry no
FIT file, so they are skipped. The per-second stream for each returned activity
lives in ``fit-files/<runna_id>.fit`` (see :mod:`runlog.sources.runna.fit`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

# Runna stores the same session once per device. The phone recording carries the
# manually set treadmill speed, so it is preferred over the watch's arm-swing
# copies when collapsing a session to one row.
_RECORD_PRIORITY: dict[str, int] = {"PHONE": 0, "APPLE_WATCH": 1, "APPLE_FITNESS": 2}
_SESSION_WINDOW_S = 180


@dataclass(frozen=True)
class RunnaActivity:
    """One Runna-recorded run, from the ``activities.json`` summary block."""

    runna_id: str
    strava_activity_id: int | None
    is_indoor: bool
    record_type: str
    start_time_utc: datetime
    name: str | None
    distance_m: float | None
    elapsed_s: int | None
    moving_s: int | None
    avg_hr: float | None
    max_hr: float | None
    avg_cadence: float | None
    avg_speed_mps: float | None
    max_speed_mps: float | None
    calories: float | None

    @property
    def fit_file(self) -> str:
        return f"{self.runna_id}.fit"


def _opt_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _ms_to_s(ms: float | None) -> int | None:
    return round(ms / 1000) if ms else None


def _strava_id(raw: object) -> int | None:
    if not isinstance(raw, list) or not raw:
        return None
    first = raw[0]
    value = first.get("stravaActivityId") if isinstance(first, dict) else None
    return int(value) if isinstance(value, int) else None


def _parse_activity(obj: dict[str, object]) -> RunnaActivity:
    raw_summary = obj.get("summary")
    summary: dict[str, object] = raw_summary if isinstance(raw_summary, dict) else {}
    start_ms = _opt_float(summary.get("startTimestamp")) or _opt_float(
        obj.get("createdOn")
    )
    if start_ms is None:
        raise ValueError(f"Runna activity {obj.get('id')} has no start timestamp")
    return RunnaActivity(
        runna_id=str(obj["id"]),
        strava_activity_id=_strava_id(obj.get("stravaActivities")),
        is_indoor=obj.get("type") == "INDOOR_RUN" or bool(obj.get("treadmillType")),
        record_type=_opt_str(obj.get("recordType")) or "",
        start_time_utc=datetime.fromtimestamp(start_ms / 1000, tz=UTC),
        name=_opt_str(obj.get("name")),
        distance_m=_opt_float(summary.get("distance")),
        elapsed_s=_ms_to_s(_opt_float(summary.get("totalTime"))),
        moving_s=_ms_to_s(_opt_float(summary.get("movingTime"))),
        avg_hr=_opt_float(summary.get("averageHeartRate")),
        max_hr=_opt_float(summary.get("maxHeartRate")),
        avg_cadence=_opt_float(summary.get("averageCadence")),
        avg_speed_mps=_opt_float(summary.get("averageSpeed")),
        max_speed_mps=_opt_float(summary.get("maxSpeed")),
        calories=_opt_float(summary.get("calories")),
    )


def parse_activities(data: bytes) -> list[RunnaActivity]:
    """Native Runna recordings from an ``activities.json`` payload.

    The same session is logged once per device; recordings within
    ``_SESSION_WINDOW_S`` of each other collapse to one, keeping the phone's
    manual-speed recording over the watch's arm-swing copies.
    """
    activities: list[dict[str, object]] = json.loads(data)
    native = [
        _parse_activity(obj) for obj in activities if obj.get("recordType") != "STRAVA"
    ]
    return _collapse_sessions(native)


def _rank(activity: RunnaActivity) -> tuple[int, bool, datetime]:
    return (
        _RECORD_PRIORITY.get(activity.record_type, 3),
        activity.strava_activity_id is None,
        activity.start_time_utc,
    )


def _collapse_sessions(activities: list[RunnaActivity]) -> list[RunnaActivity]:
    groups: list[list[RunnaActivity]] = []
    for activity in sorted(activities, key=lambda a: a.start_time_utc):
        same_session = (
            groups
            and (activity.start_time_utc - groups[-1][0].start_time_utc).total_seconds()
            <= _SESSION_WINDOW_S
        )
        if same_session:
            groups[-1].append(activity)
        else:
            groups.append([activity])
    return [min(group, key=_rank) for group in groups]
