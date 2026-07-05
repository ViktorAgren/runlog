"""Streaming parser for an Apple Health ``export.xml``.

The file can be hundreds of MB, so it is parsed with ``iterparse`` and elements
are cleared as they are consumed. Two things are extracted:

* ``Workout`` elements -> :class:`AppleWorkout` (running/interval sessions,
  their heart-rate statistics, segment laps, and route file reference).
* A curated set of ``Record`` types -> :class:`~runlog.domain.HealthMetric`
  (resting HR, HRV, VO2max, ...), i.e. the data Strava does not expose.

General per-second ``HeartRate`` records are intentionally skipped: per-workout
heart rate comes from the route/Strava stream, and keeping every sample would
add millions of rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from xml.etree.ElementTree import iterparse

from runlog.domain import HealthMetric, Lap

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import IO
    from xml.etree.ElementTree import Element

# Apple Health timestamps look like "2026-06-01 07:30:00 +0100".
_DT_FORMAT = "%Y-%m-%d %H:%M:%S %z"

# Curated non-workout metrics worth storing (identifier -> our metric_type).
_METRIC_TYPES = {
    "HKQuantityTypeIdentifierRestingHeartRate": "resting_hr",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv_sdnn",
    "HKQuantityTypeIdentifierVO2Max": "vo2max",
    "HKQuantityTypeIdentifierWalkingHeartRateAverage": "walking_hr_avg",
    "HKQuantityTypeIdentifierRespiratoryRate": "respiratory_rate",
}
_DISTANCE_TO_M = {"km": 1000.0, "mi": 1609.344, "m": 1.0, "yd": 0.9144, "ft": 0.3048}
_DURATION_TO_S = {"s": 1.0, "min": 60.0, "hr": 3600.0}
_LAP_EVENT_TYPES = {"HKWorkoutEventTypeSegment", "HKWorkoutEventTypeLap"}


@dataclass(frozen=True)
class AppleWorkout:
    """A workout parsed from ``export.xml`` (source-specific, pre-normalization)."""

    activity_type: str
    start_time_utc: datetime
    end_time_utc: datetime
    duration_s: int | None = None
    distance_m: float | None = None
    calories: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    avg_cadence: float | None = None
    route_file: str | None = None
    laps: tuple[Lap, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AppleExport:
    """Everything we pull from an Apple Health export."""

    workouts: tuple[AppleWorkout, ...]
    metrics: tuple[HealthMetric, ...]


def _parse_dt(value: str) -> datetime:
    return datetime.strptime(value, _DT_FORMAT).astimezone(UTC)


def _convert(
    value: str | None, table: dict[str, float], unit: str | None
) -> float | None:
    if value is None:
        return None
    return float(value) * table.get(unit or "", 1.0)


def _strip_prefix(activity_type: str) -> str:
    return activity_type.removeprefix("HKWorkoutActivityType") or "Unknown"


def _workout_hr(elem: Element) -> tuple[float | None, float | None]:
    """Return (average, maximum) heart rate from WorkoutStatistics, if present."""
    stat = _find_stat(elem, lambda t: t == "HKQuantityTypeIdentifierHeartRate")
    if stat is None:
        return None, None
    avg = stat.get("average")
    mx = stat.get("maximum")
    return (float(avg) if avg else None, float(mx) if mx else None)


def _find_stat(elem: Element, matches: Callable[[str], bool]) -> Element | None:
    """Return the first WorkoutStatistics whose type satisfies ``matches``."""
    for stat in elem.findall("WorkoutStatistics"):
        if matches(stat.get("type", "")):
            return stat
    return None


def _distance_m(elem: Element) -> float | None:
    """Distance in meters, from the Workout attribute or its statistics.

    Older exports carry ``totalDistance`` on the Workout; newer ones only have
    a ``HKQuantityTypeIdentifierDistance*`` statistic (sum + unit).
    """
    attr = _convert(
        elem.get("totalDistance"), _DISTANCE_TO_M, elem.get("totalDistanceUnit")
    )
    if attr is not None:
        return attr
    stat = _find_stat(elem, lambda t: t.startswith("HKQuantityTypeIdentifierDistance"))
    if stat is None:
        return None
    return _convert(stat.get("sum"), _DISTANCE_TO_M, stat.get("unit"))


def _calories(elem: Element) -> float | None:
    """Active energy in kcal, from the Workout attribute or its statistics."""
    attr = _as_float(elem.get("totalEnergyBurned"))
    if attr is not None:
        return attr
    stat = _find_stat(elem, lambda t: t == "HKQuantityTypeIdentifierActiveEnergyBurned")
    return _as_float(stat.get("sum")) if stat is not None else None


def _workout_laps(elem: Element) -> tuple[Lap, ...]:
    """Build laps from segment/lap WorkoutEvents (interval sessions)."""
    laps: list[Lap] = []
    for event in elem.findall("WorkoutEvent"):
        if event.get("type") not in _LAP_EVENT_TYPES:
            continue
        elapsed = _convert(
            event.get("duration"), _DURATION_TO_S, event.get("durationUnit")
        )
        laps.append(
            Lap(
                lap_index=len(laps),
                elapsed_s=int(elapsed) if elapsed is not None else None,
            )
        )
    return tuple(laps)


def _route_file(elem: Element) -> str | None:
    """Return the route GPX basename referenced by the workout, if any."""
    route = elem.find("WorkoutRoute")
    reference = route.find("FileReference") if route is not None else None
    path = reference.get("path") if reference is not None else None
    return path.rsplit("/", 1)[-1] if path else None


def _cadence_spm(elem: Element, duration_s: int | None) -> float | None:
    """Full running cadence (steps/min) from total workout StepCount."""
    stat = _find_stat(elem, lambda t: t == "HKQuantityTypeIdentifierStepCount")
    steps = _as_float(stat.get("sum")) if stat is not None else None
    if steps is None or not duration_s:
        return None
    return round(steps / (duration_s / 60), 1)


def _parse_workout(elem: Element) -> AppleWorkout:
    avg_hr, max_hr = _workout_hr(elem)
    duration_s = _as_int(
        _convert(elem.get("duration"), _DURATION_TO_S, elem.get("durationUnit"))
    )
    return AppleWorkout(
        activity_type=_strip_prefix(elem.get("workoutActivityType", "")),
        start_time_utc=_parse_dt(elem.get("startDate", "")),
        end_time_utc=_parse_dt(elem.get("endDate", "")),
        duration_s=duration_s,
        distance_m=_distance_m(elem),
        calories=_calories(elem),
        avg_hr=avg_hr,
        max_hr=max_hr,
        avg_cadence=_cadence_spm(elem, duration_s),
        route_file=_route_file(elem),
        laps=_workout_laps(elem),
    )


def _parse_metric(elem: Element) -> HealthMetric | None:
    metric_type = _METRIC_TYPES.get(elem.get("type", ""))
    value = elem.get("value")
    if metric_type is None or value is None:
        return None
    try:
        numeric = float(value)
    except ValueError:
        return None
    return HealthMetric(
        metric_type=metric_type,
        start_time_utc=_parse_dt(elem.get("startDate", "")),
        end_time_utc=_parse_dt(elem.get("endDate", "")),
        value=numeric,
        unit=elem.get("unit"),
        source=elem.get("sourceName"),
    )


def _as_int(value: float | None) -> int | None:
    return int(value) if value is not None else None


def _as_float(value: str | None) -> float | None:
    return float(value) if value is not None else None


def parse_export(source: IO[bytes]) -> AppleExport:
    """Parse an ``export.xml`` stream into workouts and curated metrics."""
    workouts: list[AppleWorkout] = []
    metrics: list[HealthMetric] = []
    for _event, elem in iterparse(source, events=("end",)):
        if elem.tag == "Workout":
            workouts.append(_parse_workout(elem))
            elem.clear()
        elif elem.tag == "Record":
            metric = _parse_metric(elem)
            if metric is not None:
                metrics.append(metric)
            elem.clear()
    return AppleExport(workouts=tuple(workouts), metrics=tuple(metrics))
