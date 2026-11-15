"""Mean-maximal speed curve and its critical-speed / D' parameters.

The mean-maximal curve is the fastest average speed sustained for each of a grid
of durations, taken across the whole history — the running analogue of the
cycling power-duration curve. Fitting the two-parameter critical-power model to
it gives Critical Speed (a lactate-threshold / MLSS proxy, the hyperbola's
asymptote) and D' (the finite distance available above CS, its curvature).

References: the critical-power concept (Jones et al.) and running CS/D' models.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from runlog.analyze import cs, streams

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from runlog.analyze.metrics import Run
    from runlog.analyze.streams import StreamSample

# Durations (s) in the severe/heavy domains where the CS model holds: ~2–30 min.
DURATIONS_S: tuple[int, ...] = (120, 180, 240, 300, 420, 600, 900, 1200, 1800)


def mean_maximal_speed(stream: Sequence[StreamSample], duration_s: int) -> float | None:
    """Fastest average speed (m/s) sustained over any ``duration_s`` window."""
    pts = [(s.offset_s, s.distance_m) for s in stream if s.distance_m is not None]
    if len(pts) < 2:
        return None
    offsets = np.array([p[0] for p in pts], dtype=float)
    distances = np.array([p[1] for p in pts], dtype=float)
    if offsets[-1] - offsets[0] < duration_s:
        return None
    # For each start i, the first index whose offset is >= offsets[i] + duration.
    ends = np.searchsorted(offsets, offsets + duration_s, side="left")
    valid = ends < offsets.size
    if not valid.any():
        return None
    starts = np.nonzero(valid)[0]
    stops = ends[valid]
    dt = offsets[stops] - offsets[starts]
    dd = distances[stops] - distances[starts]
    good = dt > 0
    if not good.any():
        return None
    best = float(np.max(dd[good] / dt[good]))
    return best if best > 0 else None


def mean_maximal_curve(
    conn: sqlite3.Connection,
    runs: Sequence[Run],
    durations: Sequence[int] = DURATIONS_S,
) -> list[tuple[int, float]]:
    """Best mean-maximal speed across all runs, per duration (ascending)."""
    best: dict[int, float] = {}
    for run in runs:
        stream = streams.full_stream(conn, run.activity_id)
        for duration in durations:
            speed = mean_maximal_speed(stream, duration)
            if speed is not None:
                best[duration] = max(best.get(duration, 0.0), speed)
    return sorted(best.items())


def fit(curve: Sequence[tuple[int, float]]) -> cs.CsModel | None:
    """Fit Critical Speed + D' to a mean-maximal curve of ``(duration_s, mps)``.

    Each point is the effort ``distance = speed * duration`` held for ``duration``
    seconds, which is exactly the shape :func:`runlog.analyze.cs.fit_cs` expects.
    """
    points = [
        cs.CsPoint(distance_m=speed * duration, seconds=float(duration))
        for duration, speed in curve
    ]
    return cs.fit_cs(points)
