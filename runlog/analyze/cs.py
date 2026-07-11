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
from typing import TYPE_CHECKING

from runlog.analyze import analytics, streams

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

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
