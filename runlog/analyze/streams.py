"""Per-run analysis over the full per-second stream (GPS + elevation + velocity).

Read-only and pure: every metric takes an already-loaded stream (a list of
:class:`StreamSample`) and returns plain values, so it is unit-testable on
synthetic streams. Only :func:`full_stream` and :func:`run_stream_series` touch
the database. These power the analyses that need the point-by-point trace rather
than a per-run average: elevation-based grade-adjusted pace, climb metrics, and
pacing quality.
"""

from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING

from runlog.analyze.metrics import PLAUSIBLE_PACE_S_PER_KM

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence
    from datetime import date

    from runlog.analyze.metrics import Run
    from runlog.domain import ActivityId

# Minetti (2002) metabolic cost of running vs. gradient, normalized to level
# cost so the value is a direct "flat-equivalent distance" multiplier. Valid for
# gradients in [-0.45, 0.45]; steeper segments are clamped.
_LEVEL_COST = 3.6
_MAX_GRADE = 0.45
# Group raw ~1 Hz points into >= this many metres before taking a grade, so GPS
# altitude jitter does not produce wild per-sample gradients.
_MIN_SEGMENT_M = 10.0


@dataclass(frozen=True)
class StreamSample:
    offset_s: int
    distance_m: float
    altitude_m: float | None
    lat: float | None
    lng: float | None
    hr: float | None
    velocity_mps: float | None


def full_stream(
    conn: sqlite3.Connection, activity_id: ActivityId
) -> list[StreamSample]:
    """Load a run's full per-point stream, ordered by position."""
    return [
        StreamSample(
            offset_s=int(row["offset_s"]),
            distance_m=float(row["distance_m"]),
            altitude_m=row["altitude_m"],
            lat=row["lat"],
            lng=row["lng"],
            hr=row["hr"],
            velocity_mps=row["velocity_mps"],
        )
        for row in conn.execute(
            """
            SELECT offset_s, distance_m, altitude_m, lat, lng, hr, velocity_mps
            FROM stream_points
            WHERE activity_id = ? AND distance_m IS NOT NULL AND offset_s IS NOT NULL
            ORDER BY seq
            """,
            (int(activity_id),),
        )
    ]


def lap_hr_stats(
    stream: Sequence[StreamSample], lap_end_offsets: Sequence[int]
) -> list[float | None]:
    """Max HR within each lap, from the point stream and the laps' end times.

    ``lap_end_offsets`` are the cumulative end offsets (seconds from the run
    start) of each lap, ascending. A point at ``offset_s`` belongs to the first
    lap whose end exceeds it; points past the last boundary fall in the final
    lap. Returns one max HR per lap (``None`` for a lap with no HR samples), so
    a per-rep max can sit next to the stored per-rep average.
    """
    ends = list(lap_end_offsets)
    maxes: list[float | None] = [None] * len(ends)
    if not ends:
        return maxes
    for sample in stream:
        if sample.hr is None:
            continue
        lap = min(bisect.bisect_right(ends, sample.offset_s), len(ends) - 1)
        current = maxes[lap]
        if current is None or sample.hr > current:
            maxes[lap] = sample.hr
    return maxes


def grade_adjust_factor(grade: float) -> float:
    """Flat-equivalent cost multiplier for a running gradient (rise/run).

    1.0 on the flat, >1 uphill, <1 on gentle descents (per Minetti's cost curve).
    """
    g = max(-_MAX_GRADE, min(_MAX_GRADE, grade))
    cost = (
        155.4 * g**5 - 30.4 * g**4 - 43.3 * g**3 + 46.3 * g**2 + 19.5 * g + _LEVEL_COST
    )
    return cost / _LEVEL_COST


def _segments(
    stream: Sequence[StreamSample], min_segment_m: float = _MIN_SEGMENT_M
) -> list[tuple[float, float]]:
    """Group consecutive points into (distance, altitude-delta) segments.

    Accumulates until at least ``min_segment_m`` of distance has passed, which
    smooths GPS-altitude noise before any gradient is derived.
    """
    segments: list[tuple[float, float]] = []
    acc_dist = acc_alt = 0.0
    prev_dist: float | None = None
    prev_alt: float | None = None
    for sample in stream:
        if sample.altitude_m is None:
            continue
        if prev_dist is not None and prev_alt is not None:
            acc_dist += sample.distance_m - prev_dist
            acc_alt += sample.altitude_m - prev_alt
            if acc_dist >= min_segment_m:
                segments.append((acc_dist, acc_alt))
                acc_dist = acc_alt = 0.0
        prev_dist, prev_alt = sample.distance_m, sample.altitude_m
    if acc_dist > 0:
        segments.append((acc_dist, acc_alt))
    return segments


def grade_adjusted_pace_s_per_km(stream: Sequence[StreamSample]) -> float | None:
    """Flat-equivalent pace (s/km) from the actual elevation profile."""
    segments = _segments(stream)
    equivalent_m = sum(
        dist * grade_adjust_factor(alt / dist) for dist, alt in segments if dist > 0
    )
    if equivalent_m <= 0:
        return None
    duration_s = stream[-1].offset_s - stream[0].offset_s
    if duration_s <= 0:
        return None
    pace = duration_s / (equivalent_m / 1000)
    # Guard against corrupted streams (e.g. a paused workout whose offsets span
    # hours): drop paces outside the same plausibility band the rest of the
    # analysis uses, since GAP divides by the full offset span.
    low, high = PLAUSIBLE_PACE_S_PER_KM
    return pace if low <= pace <= high else None


@dataclass(frozen=True)
class ClimbStats:
    ascent_m: float
    descent_m: float
    vam_m_per_h: float  # vertical ascent metres per hour of moving time
    longest_climb_m: float


def climb_stats(stream: Sequence[StreamSample]) -> ClimbStats | None:
    """Ascent, descent, climbing rate (VAM), and the largest sustained climb."""
    segments = _segments(stream)
    if not segments:
        return None
    ascent = sum(alt for _dist, alt in segments if alt > 0)
    descent = -sum(alt for _dist, alt in segments if alt < 0)
    duration_h = (stream[-1].offset_s - stream[0].offset_s) / 3600
    longest = current = 0.0
    for _dist, alt in segments:
        current = current + alt if alt > 0 else 0.0
        longest = max(longest, current)
    return ClimbStats(
        ascent_m=round(ascent, 1),
        descent_m=round(descent, 1),
        vam_m_per_h=round(ascent / duration_h) if duration_h > 0 else 0.0,
        longest_climb_m=round(longest, 1),
    )


def km_split_paces(stream: Sequence[StreamSample]) -> list[float]:
    """Pace (s/km) for each completed kilometre, by interpolating the distance."""
    if len(stream) < 2:
        return []
    boundary_times = [float(stream[0].offset_s)]
    target = 1000.0
    for prev, cur in zip(stream, stream[1:], strict=False):
        span = cur.distance_m - prev.distance_m
        while cur.distance_m >= target:
            frac = (target - prev.distance_m) / span if span > 0 else 0.0
            boundary_times.append(prev.offset_s + frac * (cur.offset_s - prev.offset_s))
            target += 1000.0
    return [
        boundary_times[i] - boundary_times[i - 1] for i in range(1, len(boundary_times))
    ]


@dataclass(frozen=True)
class PacingStats:
    even_cv: float  # coefficient of variation of km-split paces (lower = steadier)
    negative_split_pct: float  # > 0 means the second half was faster


def pacing_stats(stream: Sequence[StreamSample]) -> PacingStats | None:
    """Pacing evenness and negative-split percentage from km splits."""
    paces = km_split_paces(stream)
    if len(paces) < 2:
        return None
    mean_pace = statistics.mean(paces)
    if mean_pace <= 0:
        return None
    mid = len(paces) // 2
    first = statistics.mean(paces[:mid])
    second = statistics.mean(paces[mid:])
    return PacingStats(
        even_cv=round(statistics.pstdev(paces) / mean_pace, 3),
        negative_split_pct=round((first - second) / first * 100, 1) if first else 0.0,
    )


def run_stream_series(
    conn: sqlite3.Connection,
    runs: Sequence[Run],
    value: Callable[[list[StreamSample]], float | None],
) -> list[tuple[date, float]]:
    """One ``(date, value)`` per run whose stream yields ``value`` (else skipped).

    Mirrors :func:`runlog.analyze.metrics.run_trend` but sources each point from
    the loaded per-second stream rather than a stored per-run column.
    """
    series: list[tuple[date, float]] = []
    for run in runs:
        reading = value(full_stream(conn, run.activity_id))
        if reading is not None:
            series.append((run.start.date(), reading))
    return series
