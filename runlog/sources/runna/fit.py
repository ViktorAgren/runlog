"""Parse a Runna ``.fit`` file into a per-second stream and workout laps.

Runna records the full per-second series (heart rate, cadence, the manually set
treadmill speed, distance, power) plus lap boundaries that mark the real workout
structure (warm-up, reps, recoveries). The lap messages' own HR fields are
written as zero, so each lap's heart rate is recomputed from the records that
fall inside it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from io import BytesIO

from fitparse import FitFile

from runlog.domain import Lap, StreamPoint


@dataclass(frozen=True)
class FitActivity:
    """The stream and laps parsed from one FIT file."""

    stream: tuple[StreamPoint, ...]
    laps: tuple[Lap, ...]


@dataclass(frozen=True)
class _Sample:
    """One per-second record, retained to compute lap heart rate by time."""

    time: datetime
    hr: float | None


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _coordinate(value: object) -> float | None:
    """FIT semicircles to degrees; indoor runs record 0, which is not a fix."""
    deg = _num(value)
    return deg * (180.0 / 2**31) if deg else None


def _stream_point(
    fields: dict[str, object], time: datetime, start: datetime
) -> StreamPoint:
    cadence = _num(fields.get("cadence"))
    speed = _num(fields.get("enhanced_speed"))
    if speed is None:
        speed = _num(fields.get("speed"))
    return StreamPoint(
        offset_s=round((time - start).total_seconds()),
        distance_m=_num(fields.get("distance")),
        lat=_coordinate(fields.get("position_lat")),
        lng=_coordinate(fields.get("position_long")),
        altitude_m=_num(fields.get("enhanced_altitude") or fields.get("altitude")),
        hr=_num(fields.get("heart_rate")),
        # FIT running cadence is per-leg rpm; double it for full steps/min.
        cadence=cadence * 2 if cadence is not None else None,
        velocity_mps=speed,
        watts=_num(fields.get("power")),
    )


def _lap(index: int, fields: dict[str, object], samples: list[_Sample]) -> Lap:
    start, end = fields.get("start_time"), fields.get("timestamp")
    hrs = (
        [s.hr for s in samples if s.hr is not None and start <= s.time <= end]
        if isinstance(start, datetime) and isinstance(end, datetime)
        else []
    )
    distance_m = _num(fields.get("total_distance"))
    elapsed = _num(fields.get("total_timer_time"))
    return Lap(
        lap_index=index,
        elapsed_s=round(elapsed) if elapsed is not None else None,
        distance_m=distance_m,
        avg_hr=round(sum(hrs) / len(hrs), 1) if hrs else None,
        avg_pace_s_per_km=(
            elapsed / (distance_m / 1000)
            if elapsed and distance_m and distance_m > 0
            else None
        ),
    )


def parse_fit(data: bytes) -> FitActivity:
    """Stream points and laps from one FIT file's bytes."""
    records: list[dict[str, object]] = []
    laps: list[dict[str, object]] = []
    for message in FitFile(BytesIO(data)).get_messages(("record", "lap")):
        fields = {field.name: field.value for field in message}
        bucket = records if message.name == "record" else laps
        bucket.append(fields)

    timed: list[tuple[datetime, dict[str, object]]] = [
        (time, r) for r in records if isinstance((time := r.get("timestamp")), datetime)
    ]
    if not timed:
        return FitActivity(stream=(), laps=())
    start = min(time for time, _ in timed)
    points = tuple(_stream_point(fields, time, start) for time, fields in timed)
    samples = [_Sample(time, _num(f.get("heart_rate"))) for time, f in timed]
    return FitActivity(
        stream=points,
        laps=tuple(_lap(i, f, samples) for i, f in enumerate(laps)),
    )
