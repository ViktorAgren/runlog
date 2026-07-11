"""Streaming parser for an Apple Health ``export.xml``.

The file can be hundreds of MB, so it is parsed with ``iterparse`` and elements
are cleared as they are consumed. Two things are extracted:

* ``Workout`` elements -> :class:`AppleWorkout` (running/interval sessions,
  their heart-rate statistics, segment laps, and route file reference).
* A curated set of ``Record`` types -> :class:`~runlog.domain.HealthMetric`
  (resting HR, HRV, VO2max, ...), i.e. the data Strava does not expose.

Per-sample ``HeartRate`` records are collected (sorted by time) so the ingest
layer can reconstruct per-second HR streams for Apple-only runs, whose route
GPX carries GPS only. High-volume cumulative records (energy, steps) are
aggregated to daily totals; ``SleepAnalysis`` to nightly asleep hours.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from xml.etree.ElementTree import iterparse

from runlog.domain import HealthMetric, Lap

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import IO
    from xml.etree.ElementTree import Element

# Apple Health timestamps look like "2026-06-01 07:30:00 +0100".
_DT_FORMAT = "%Y-%m-%d %H:%M:%S %z"

# Curated periodic metrics stored one-per-reading (identifier -> metric_type).
_METRIC_TYPES = {
    "HKQuantityTypeIdentifierRestingHeartRate": "resting_hr",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv_sdnn",
    "HKQuantityTypeIdentifierVO2Max": "vo2max",
    "HKQuantityTypeIdentifierWalkingHeartRateAverage": "walking_hr_avg",
    "HKQuantityTypeIdentifierRespiratoryRate": "respiratory_rate",
    "HKQuantityTypeIdentifierOxygenSaturation": "spo2",
    "HKQuantityTypeIdentifierHeartRateRecoveryOneMinute": "hr_recovery_1min",
    "HKQuantityTypeIdentifierBodyMass": "body_mass",
    "HKQuantityTypeIdentifierWalkingSpeed": "walking_speed",
    "HKQuantityTypeIdentifierWalkingAsymmetryPercentage": "walking_asymmetry",
    "HKQuantityTypeIdentifierAppleWalkingSteadiness": "walking_steadiness",
    "HKQuantityTypeIdentifierPhysicalEffort": "physical_effort",
}
# High-volume cumulative metrics aggregated to one daily total.
_DAILY_SUM_TYPES = {
    "HKQuantityTypeIdentifierActiveEnergyBurned": "active_energy",
    "HKQuantityTypeIdentifierAppleExerciseTime": "exercise_minutes",
    "HKQuantityTypeIdentifierStepCount": "steps",
    "HKQuantityTypeIdentifierFlightsClimbed": "flights_climbed",
}
_DISTANCE_TO_M = {"km": 1000.0, "mi": 1609.344, "m": 1.0, "yd": 0.9144, "ft": 0.3048}
_DURATION_TO_S = {"s": 1.0, "min": 60.0, "hr": 3600.0}
_LAP_EVENT_TYPES = {"HKWorkoutEventTypeSegment", "HKWorkoutEventTypeLap"}

# Running dynamics that newer exports store as standalone per-second Record
# time-series (not WorkoutStatistics), because the run itself was logged via a
# third party (Strava). Identifier -> (activity column, unit divisor to the
# column's unit). These are averaged over each run's window at ingest time.
_DYNAMICS_RECORD_TYPES: dict[str, tuple[str, float]] = {
    "HKQuantityTypeIdentifierRunningPower": ("avg_power_w", 1.0),
    "HKQuantityTypeIdentifierRunningStrideLength": ("avg_stride_length_m", 1.0),
    "HKQuantityTypeIdentifierRunningVerticalOscillation": (
        "avg_vertical_oscillation_cm",
        1.0,
    ),
    "HKQuantityTypeIdentifierRunningGroundContactTime": ("avg_ground_contact_ms", 1.0),
    "HKQuantityTypeIdentifierRunningSpeed": ("avg_running_speed_mps", 3.6),  # km/h->m/s
}


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
    avg_power_w: float | None = None
    avg_stride_length_m: float | None = None
    avg_vertical_oscillation_cm: float | None = None
    avg_ground_contact_ms: float | None = None
    avg_running_speed_mps: float | None = None
    route_file: str | None = None
    laps: tuple[Lap, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AppleExport:
    """Everything we pull from an Apple Health export."""

    workouts: tuple[AppleWorkout, ...]
    metrics: tuple[HealthMetric, ...]
    # All per-sample HeartRate readings (time, bpm), sorted, for reconstructing
    # per-second HR streams on Apple-only workouts whose route GPX lacks HR.
    heart_rate_samples: tuple[tuple[datetime, float], ...] = ()
    # Standalone running-dynamics samples (column name -> sorted (time, value)),
    # averaged over each run's window at ingest to fill Strava-logged runs.
    dynamics_samples: dict[str, tuple[tuple[datetime, float], ...]] = field(
        default_factory=dict
    )


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


def _activity_laps(elem: Element) -> tuple[Lap, ...]:
    """Laps from ``WorkoutActivity`` segments (warm-up / work / cool-down).

    Newer Apple exports nest each workout phase in a ``WorkoutActivity`` element
    with its own ``WorkoutStatistics`` (distance, HR, ...). These are the real
    per-phase splits — far richer than the bare ``Segment`` event durations — so
    one lap is built per activity with a pace derived from distance and time.
    """
    laps: list[Lap] = []
    for activity in elem.findall("WorkoutActivity"):
        start, end = activity.get("startDate"), activity.get("endDate")
        if not (start and end):
            continue
        duration = (_parse_dt(end) - _parse_dt(start)).total_seconds()
        if duration <= 0:
            continue
        stats = {st.get("type", ""): st for st in activity.findall("WorkoutStatistics")}
        dist = stats.get("HKQuantityTypeIdentifierDistanceWalkingRunning")
        distance = (
            _convert(dist.get("sum"), _DISTANCE_TO_M, dist.get("unit"))
            if dist is not None
            else None
        )
        hr = stats.get("HKQuantityTypeIdentifierHeartRate")
        pace = duration / (distance / 1000) if distance and distance > 0 else None
        laps.append(
            Lap(
                lap_index=len(laps),
                elapsed_s=int(duration),
                distance_m=round(distance, 1) if distance else None,
                avg_hr=_as_float(hr.get("average")) if hr is not None else None,
                avg_pace_s_per_km=round(pace, 1) if pace else None,
            )
        )
    return tuple(laps)


def _event_laps(elem: Element) -> tuple[Lap, ...]:
    """Fallback laps from ``Segment`` WorkoutEvents (duration only).

    Apple often exports overlapping and duplicated ``Segment`` events (several
    sharing a start time with different durations, and the whole set repeated),
    which naively summed to ~2x the workout. We dedupe by (start, duration) and
    keep only segments that do not overlap the previously kept one.
    """
    segments: set[tuple[datetime, float]] = set()
    for event in elem.findall("WorkoutEvent"):
        if event.get("type") not in _LAP_EVENT_TYPES:
            continue
        start = event.get("date")
        duration = _convert(
            event.get("duration"), _DURATION_TO_S, event.get("durationUnit")
        )
        if start and duration and duration > 0:
            segments.add((_parse_dt(start), duration))
    laps: list[Lap] = []
    last_end: datetime | None = None
    for seg_start, duration in sorted(segments):
        if last_end is not None and seg_start < last_end:
            continue  # overlaps the previous segment — an Apple export artifact
        laps.append(Lap(lap_index=len(laps), elapsed_s=int(duration)))
        last_end = seg_start + timedelta(seconds=duration)
    return tuple(laps)


def _workout_laps(elem: Element) -> tuple[Lap, ...]:
    """Per-phase laps: prefer the rich WorkoutActivity segments, else events."""
    activity_laps = _activity_laps(elem)
    if len(activity_laps) >= 2:
        return activity_laps
    return _event_laps(elem)


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


def _stat_average(elem: Element, type_id: str) -> float | None:
    """The ``average`` of a named WorkoutStatistics (running dynamics), if present."""
    stat = _find_stat(elem, lambda t: t == type_id)
    return _as_float(stat.get("average")) if stat is not None else None


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
        avg_power_w=_stat_average(elem, "HKQuantityTypeIdentifierRunningPower"),
        avg_stride_length_m=_stat_average(
            elem, "HKQuantityTypeIdentifierRunningStrideLength"
        ),
        avg_vertical_oscillation_cm=_stat_average(
            elem, "HKQuantityTypeIdentifierRunningVerticalOscillation"
        ),
        avg_ground_contact_ms=_stat_average(
            elem, "HKQuantityTypeIdentifierRunningGroundContactTime"
        ),
        avg_running_speed_mps=_stat_average(
            elem, "HKQuantityTypeIdentifierRunningSpeed"
        ),
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


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _consume_record(
    elem: Element,
    metrics: list[HealthMetric],
    daily: dict[tuple[str, date], float],
    sleep_seconds: dict[date, float],
    hr_samples: list[tuple[datetime, float]],
    dynamics: dict[str, list[tuple[datetime, float]]],
) -> None:
    """Route a Record to a periodic metric, daily sum, sleep, HR, or dynamics."""
    type_id = elem.get("type", "")
    if type_id == "HKQuantityTypeIdentifierHeartRate":
        value = _as_float(elem.get("value"))
        start = elem.get("startDate")
        if value is not None and start:
            hr_samples.append((_parse_dt(start), value))
    elif type_id in _DYNAMICS_RECORD_TYPES:
        column, divisor = _DYNAMICS_RECORD_TYPES[type_id]
        value = _as_float(elem.get("value"))
        start = elem.get("startDate")
        if value is not None and start:
            dynamics[column].append((_parse_dt(start), value / divisor))
    elif type_id in _METRIC_TYPES:
        metric = _parse_metric(elem)
        if metric is not None:
            metrics.append(metric)
    elif type_id in _DAILY_SUM_TYPES:
        value = _as_float(elem.get("value"))
        start = elem.get("startDate")
        if value is not None and start:
            daily[(_DAILY_SUM_TYPES[type_id], date.fromisoformat(start[:10]))] += value
    elif type_id == "HKCategoryTypeIdentifierSleepAnalysis":
        if "Asleep" in elem.get("value", ""):  # ignore InBed / Awake segments
            start, end = elem.get("startDate"), elem.get("endDate")
            if start and end:
                begin, finish = _parse_dt(start), _parse_dt(end)
                sleep_seconds[finish.date()] += (finish - begin).total_seconds()


def parse_export(source: IO[bytes]) -> AppleExport:
    """Parse ``export.xml`` into workouts + periodic/daily/sleep health metrics."""
    workouts: list[AppleWorkout] = []
    metrics: list[HealthMetric] = []
    daily: dict[tuple[str, date], float] = defaultdict(float)
    sleep_seconds: dict[date, float] = defaultdict(float)
    hr_samples: list[tuple[datetime, float]] = []
    dynamics: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for _event, elem in iterparse(source, events=("end",)):
        if elem.tag == "Workout":
            workouts.append(_parse_workout(elem))
            elem.clear()
        elif elem.tag == "Record":
            _consume_record(elem, metrics, daily, sleep_seconds, hr_samples, dynamics)
            elem.clear()
    for (metric_type, day), total in daily.items():
        metrics.append(
            HealthMetric(metric_type, _midnight(day), round(total, 1), source="apple")
        )
    for day, seconds in sleep_seconds.items():
        metrics.append(
            HealthMetric(
                "sleep_hours",
                _midnight(day),
                round(seconds / 3600, 2),
                unit="h",
                source="apple",
            )
        )
    hr_samples.sort(key=lambda pair: pair[0])
    return AppleExport(
        workouts=tuple(workouts),
        metrics=tuple(metrics),
        heart_rate_samples=tuple(hr_samples),
        dynamics_samples={
            column: tuple(sorted(samples, key=lambda pair: pair[0]))
            for column, samples in dynamics.items()
        },
    )
