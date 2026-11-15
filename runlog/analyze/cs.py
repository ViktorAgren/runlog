"""Critical Speed (CS) modelling from an athlete's best efforts.

The two-parameter critical-power model applied to running: across the
middle-distance range the best distance covered in time ``t`` follows the line
``d = CS * t + D'``, where ``CS`` (m/s) is the highest sustainable aerobic speed
and ``D'`` (m) is the finite anaerobic distance reserve. Fitting that line to
best efforts at a spread of distances (400 m / 1 k / 2 k / 5 k) recovers both
parameters and lets us predict a time for any distance in range.

Pure and read-only. Efforts come from each run's per-point distance/time stream
via :func:`runlog.analyze.analytics.best_effort_seconds`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from runlog.analyze import analytics, streams

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import date

    from runlog.analyze.metrics import Run

# Best-effort distances used to fit the line: wide enough to separate the
# anaerobic reserve (short) from the aerobic asymptote (long).
CS_DISTANCES_M: tuple[float, ...] = (400.0, 1000.0, 2000.0, 5000.0)
# A meaningful fit needs efforts at this many distinct distances.
_MIN_POINTS = 2


@dataclass(frozen=True)
class CsPoint:
    """One (distance, fastest-time) best effort feeding the CS fit."""

    distance_m: float
    seconds: float


@dataclass(frozen=True)
class CsModel:
    cs_mps: float  # critical speed (aerobic asymptote)
    d_prime_m: float  # anaerobic distance reserve
    r: float  # correlation of the linear fit
    points: list[CsPoint]

    def predict_seconds(self, distance_m: float) -> float | None:
        """Time to cover ``distance_m`` per the model (None inside the reserve)."""
        if distance_m <= self.d_prime_m or self.cs_mps <= 0:
            return None
        return (distance_m - self.d_prime_m) / self.cs_mps


def fit_cs(points: Sequence[CsPoint]) -> CsModel | None:
    """OLS fit of ``d = CS*t + D'``; None if degenerate or CS is non-positive.

    Distance is regressed on time (x = seconds, y = metres): the slope is CS and
    the intercept is D'. A non-positive slope means the efforts don't describe a
    coherent speed-duration curve, so no model is returned.
    """
    if len(points) < _MIN_POINTS:
        return None
    ts = [p.seconds for p in points]
    ds = [p.distance_m for p in points]
    n = len(points)
    mean_t = sum(ts) / n
    mean_d = sum(ds) / n
    stt = sum((t - mean_t) ** 2 for t in ts)
    sdd = sum((d - mean_d) ** 2 for d in ds)
    if stt == 0:
        return None
    std = sum((t - mean_t) * (d - mean_d) for t, d in zip(ts, ds, strict=True))
    cs = std / stt
    if cs <= 0:
        return None
    d_prime = mean_d - cs * mean_t
    r = std / math.sqrt(stt * sdd) if sdd else 0.0
    return CsModel(
        cs_mps=round(cs, 3),
        d_prime_m=round(d_prime, 1),
        r=round(r, 3),
        points=list(points),
    )


def best_efforts_for_cs(
    conn: sqlite3.Connection,
    runs: Sequence[Run],
    distances: Sequence[float] = CS_DISTANCES_M,
) -> list[CsPoint]:
    """All-time fastest time at each distance, across every run's stream."""
    bests: dict[float, float] = {}
    for run in runs:
        stream = [
            (s.offset_s, s.distance_m)
            for s in streams.full_stream(conn, run.activity_id)
        ]
        if not stream:
            continue
        for target in distances:
            effort = analytics.best_effort_seconds(stream, target)
            if effort is not None and (target not in bests or effort < bests[target]):
                bests[target] = effort
    return [CsPoint(distance_m=t, seconds=bests[t]) for t in distances if t in bests]


def critical_speed(
    conn: sqlite3.Connection,
    runs: Sequence[Run],
    distances: Sequence[float] = CS_DISTANCES_M,
) -> CsModel | None:
    """Fit the CS/D' model from the athlete's best efforts, or None if too few."""
    return fit_cs(best_efforts_for_cs(conn, runs, distances))


@dataclass(frozen=True)
class _DatedEffort:
    day: date
    distance_m: float
    seconds: float


def _dated_efforts(
    conn: sqlite3.Connection, runs: Sequence[Run], distances: Sequence[float]
) -> list[_DatedEffort]:
    """Each run's best effort at each CS distance, with the run's date."""
    efforts: list[_DatedEffort] = []
    for run in runs:
        stream = [
            (s.offset_s, s.distance_m)
            for s in streams.full_stream(conn, run.activity_id)
        ]
        if not stream:
            continue
        for target in distances:
            seconds = analytics.best_effort_seconds(stream, target)
            if seconds is not None:
                efforts.append(_DatedEffort(run.start.date(), target, seconds))
    return efforts


def critical_speed_series(
    conn: sqlite3.Connection,
    runs: Sequence[Run],
    today: date,
    window_days: int = 120,
    step_days: int = 14,
    distances: Sequence[float] = CS_DISTANCES_M,
) -> list[tuple[date, CsModel]]:
    """Critical speed re-fitted over a trailing window at regular sample dates.

    Critical speed is a lactate-threshold / MLSS proxy, so its rise over time is
    the athlete's threshold fitness trend. Each sample fits the model from the
    best efforts inside the preceding ``window_days``; samples that cannot fit
    (too little distance spread in the window) are skipped. One stream pass per
    run, so the cost matches a single all-time fit.
    """
    efforts = _dated_efforts(conn, runs, distances)
    if not efforts:
        return []
    window = timedelta(days=window_days)
    sample = min(e.day for e in efforts) + window
    series: list[tuple[date, CsModel]] = []
    while sample <= today:
        bests: dict[float, float] = {}
        for effort in efforts:
            if sample - window <= effort.day <= sample and (
                effort.distance_m not in bests
                or effort.seconds < bests[effort.distance_m]
            ):
                bests[effort.distance_m] = effort.seconds
        model = fit_cs([CsPoint(dist, secs) for dist, secs in bests.items()])
        if model is not None:
            series.append((sample, model))
        sample += timedelta(days=step_days)
    return series


# Aerobic-threshold (LT1) velocity as a fraction of the lactate-threshold (LT2)
# velocity. Critical speed is the LT2/MLSS proxy; LT1 sits ~12% slower.
LT1_OF_LT2 = 0.88


def lactate_threshold_speeds(
    series: Sequence[tuple[date, CsModel]],
) -> list[tuple[date, float, float]]:
    """Estimated (date, LT1 km/h, LT2 km/h) from a critical-speed series.

    Critical speed is the lactate-threshold (LT2/MLSS) velocity; LT1 (aerobic
    threshold) is taken at :data:`LT1_OF_LT2` of it. Speeds are km/h so a rising
    line reads as improving threshold pace.
    """
    return [
        (day, round(model.cs_mps * 3.6 * LT1_OF_LT2, 2), round(model.cs_mps * 3.6, 2))
        for day, model in series
    ]
