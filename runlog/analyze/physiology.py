"""Stream-based physiological metrics: per-run HR zones, intensity distribution,
integrated training load, and regression cardiac drift.

Pure over a loaded stream (:class:`~runlog.analyze.streams.StreamSample`); only
the ``*_for_runs`` orchestrators touch the DB (via ``streams.full_stream``).
Each HR sample is weighted by the time to the next point, so these are true
time-in-state measures rather than raw sample counts — and, unlike the pooled
:func:`runlog.analyze.metrics.hr_zone_seconds`, they are computed per run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from runlog.analyze import metrics
from runlog.analyze.streams import full_stream

# Banister TRIMP weighting (same coefficients as analytics.run_trimp), applied
# per stream sample instead of once on the whole-run average HR.
_TRIMP_A = 0.64
_TRIMP_B = 1.92

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from runlog.analyze.metrics import Run
    from runlog.analyze.streams import StreamSample

_ZONE_COUNT = 5


def zone_seconds_for_run(stream: Sequence[StreamSample], hr_max: float) -> list[float]:
    """Seconds in each HR zone (Z1..Z5) for one run, dt-weighted per sample."""
    buckets = [0.0] * _ZONE_COUNT
    if hr_max <= 0:
        return buckets
    for current, nxt in zip(stream, stream[1:], strict=False):
        if current.hr is None:
            continue
        dt = nxt.offset_s - current.offset_s
        index = metrics.hr_zone_index(current.hr / hr_max)
        if dt > 0 and index is not None:
            buckets[index] += dt
    return buckets


@dataclass(frozen=True)
class IntensityDistribution:
    easy_pct: float  # Z1-2
    moderate_pct: float  # Z3
    hard_pct: float  # Z4-5
    easy_to_hard: float  # ratio of easy to hard time (polarization proxy)


def intensity_distribution(
    zone_totals: Sequence[float],
) -> IntensityDistribution | None:
    """Collapse Z1..Z5 seconds into easy/moderate/hard percentages + a ratio."""
    total = sum(zone_totals)
    if total <= 0:
        return None
    easy = zone_totals[0] + zone_totals[1]
    hard = zone_totals[3] + zone_totals[4]
    return IntensityDistribution(
        easy_pct=round(easy / total * 100, 1),
        moderate_pct=round(zone_totals[2] / total * 100, 1),
        hard_pct=round(hard / total * 100, 1),
        easy_to_hard=round(easy / hard, 1) if hard > 0 else float("inf"),
    )


def training_intensity_distribution(
    conn: sqlite3.Connection, runs: Sequence[Run], hr_max: float
) -> IntensityDistribution | None:
    """Aggregate zone seconds across all runs into one intensity distribution."""
    totals = [0.0] * _ZONE_COUNT
    for run in runs:
        for index, seconds in enumerate(
            zone_seconds_for_run(full_stream(conn, run.activity_id), hr_max)
        ):
            totals[index] += seconds
    return intensity_distribution(totals)


def stream_trimp(
    stream: Sequence[StreamSample], hr_max: float, hr_rest: float
) -> float | None:
    """Banister TRIMP integrated over the HR stream (accurate internal load)."""
    if hr_max <= hr_rest:
        return None
    total = 0.0
    counted = False
    for current, nxt in zip(stream, stream[1:], strict=False):
        if current.hr is None:
            continue
        dt = nxt.offset_s - current.offset_s
        if dt <= 0:
            continue
        reserve = max(0.0, min(1.0, (current.hr - hr_rest) / (hr_max - hr_rest)))
        total += (dt / 60) * reserve * _TRIMP_A * math.exp(_TRIMP_B * reserve)
        counted = True
    return round(total, 1) if counted else None


def cardiac_drift_pct(stream: Sequence[StreamSample]) -> float | None:
    """Cardiac drift as the % change in speed-per-HR across the run.

    Fits speed/HR against elapsed seconds (ordinary least squares) and expresses
    the slope over the run's duration as a percentage of the starting
    efficiency — a more robust signal than a first-half/second-half split.
    Positive means efficiency fell (HR drifted up for the same pace).
    """
    xs: list[float] = []
    ys: list[float] = []
    for sample in stream:
        if sample.hr and sample.velocity_mps and sample.hr > 0:
            xs.append(float(sample.offset_s))
            ys.append(sample.velocity_mps / sample.hr)
    if len(xs) < 4:
        return None
    n = len(xs)
    mean_x = sum(xs) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return None
    mean_y = sum(ys) / n
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sxx
    start_ratio = mean_y + slope * (xs[0] - mean_x)
    if start_ratio <= 0:
        return None
    return round(-slope * (xs[-1] - xs[0]) / start_ratio * 100, 1)
