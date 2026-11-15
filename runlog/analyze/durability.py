"""Durability: within-long-run deterioration of running efficiency.

Fatigue resistance, or "durability" (Maunder et al. 2021; Jones 2023), is how
much endurance markers deteriorate during prolonged exercise, and it varies
widely between individuals. As a field proxy this compares the efficiency factor
(speed per heart beat) in the fresh first third of a long run against its
fatigued last third; the percent decline is that run's durability cost. Tracking
it over time shows whether fatigue resistance is improving. It is a proxy, not a
lab threshold measurement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from runlog.analyze import streams

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import date

    from runlog.analyze.metrics import Run
    from runlog.analyze.streams import StreamSample

# Only runs of at least an hour carry a meaningful durability signal.
_MIN_DURATION_S = 60 * 60


def _segment_ef(
    stream: Sequence[StreamSample], low_frac: float, high_frac: float
) -> float | None:
    """Efficiency factor (m/s per bpm) over an elapsed-time window of the run."""
    pts = [s for s in stream if s.hr and s.distance_m is not None]
    if len(pts) < 4:
        return None
    start, end = pts[0].offset_s, pts[-1].offset_s
    span = end - start
    if span <= 0:
        return None
    low, high = start + low_frac * span, start + high_frac * span
    window = [s for s in pts if low <= s.offset_s <= high]
    if len(window) < 2:
        return None
    distance = (window[-1].distance_m or 0.0) - (window[0].distance_m or 0.0)
    duration = window[-1].offset_s - window[0].offset_s
    hrs = [s.hr for s in window if s.hr is not None]
    if duration <= 0 or not hrs:
        return None
    mean_hr = sum(hrs) / len(hrs)
    return (distance / duration) / mean_hr if mean_hr > 0 else None


def run_durability(stream: Sequence[StreamSample]) -> float | None:
    """Percent efficiency decline from the fresh first third to the last third."""
    fresh = _segment_ef(stream, 0.0, 1 / 3)
    fatigued = _segment_ef(stream, 2 / 3, 1.0)
    if fresh is None or fatigued is None or fresh <= 0:
        return None
    return (fresh - fatigued) / fresh * 100


def durability_series(
    conn: sqlite3.Connection,
    runs: Sequence[Run],
    min_duration_s: int = _MIN_DURATION_S,
) -> list[tuple[date, float]]:
    """Per-long-run efficiency fade (%), dated, for runs of at least an hour."""
    series: list[tuple[date, float]] = []
    for run in runs:
        if not run.moving_s or run.moving_s < min_duration_s:
            continue
        fade = run_durability(streams.full_stream(conn, run.activity_id))
        if fade is not None:
            series.append((run.start.date(), round(fade, 1)))
    return sorted(series)
