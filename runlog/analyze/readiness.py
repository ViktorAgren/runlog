"""Daily readiness score from passive recovery markers.

A single 0-100 signal answering "how recovered am I today?", built from the same
robust rolling-baseline machinery as :mod:`runlog.analyze.anomaly`. Each marker
(resting HR, HRV, sleep, HR-recovery) is scored as a signed robust z against its
own trailing baseline, oriented so higher always means *better recovered*, then
averaged over whichever markers reported that day and mapped onto 0-100 (50 is a
typical day; the 40-60 band is "normal").

Pure and read-only. Correlating the score against same-day efficiency-factor
residuals tells you how much readiness actually explains off-days.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from runlog.analyze import analytics, anomaly, metrics
from runlog.analyze.anomaly import Direction

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import date

    from runlog.analyze.metrics import Run

# Recovery markers and the tail that signals *reduced* readiness, mirroring
# anomaly._HEALTH_MARKERS minus SpO2 (too sparse/flat to score reliably).
_READINESS_MARKERS: tuple[tuple[str, Direction], ...] = (
    ("resting_hr", Direction.HIGH),
    ("hrv_sdnn", Direction.LOW),
    ("sleep_hours", Direction.LOW),
    ("hr_recovery_1min", Direction.LOW),
)
# Maps averaged robust-z onto points around the midpoint: +/-0.67 sigma spans the
# 40-60 "normal" band, +/-3.3 sigma saturates at 0 / 100.
_MIDPOINT = 50.0
_SCORE_SCALE = 15.0
# Correlation needs a few paired days to mean anything.
_MIN_PAIRS = 3


@dataclass(frozen=True)
class ReadinessDay:
    day: date
    score: float  # 0-100; 50 typical, 40-60 normal band
    contributors: dict[str, float]  # per-marker signed z (higher = better)


def _goodness_sign(direction: Direction) -> float:
    """+1 when higher readings are better, -1 when they are worse (resting HR)."""
    return -1.0 if direction is Direction.HIGH else 1.0


def readiness_series(
    conn: sqlite3.Connection, since: date | None = None
) -> list[ReadinessDay]:
    """Daily readiness score averaged across available recovery markers."""
    by_day: dict[date, dict[str, float]] = defaultdict(dict)
    for metric, direction in _READINESS_MARKERS:
        daily = metrics.daily_means(metrics.metric_series(conn, metric, since=since))
        sign = _goodness_sign(direction)
        for reading in anomaly.robust_z_series(daily):
            by_day[reading.day][metric] = round(sign * reading.deviation, 2)
    days: list[ReadinessDay] = []
    for day, contributors in sorted(by_day.items()):
        z_avg = sum(contributors.values()) / len(contributors)
        score = max(0.0, min(100.0, _MIDPOINT + _SCORE_SCALE * z_avg))
        days.append(
            ReadinessDay(
                day=day, score=round(score, 1), contributors=dict(contributors)
            )
        )
    return days


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson correlation, or None when it is undefined (flat input)."""
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    syy = sum((y - mean_y) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    return sxy / math.sqrt(sxx * syy)


def performance_correlation(
    readiness: Sequence[ReadinessDay], runs: Sequence[Run]
) -> float | None:
    """Correlate daily readiness with same-day efficiency-factor residuals.

    A positive r means higher-readiness days tend to run better than baseline
    (readiness "explains" off-days). ``None`` if too few days overlap.
    """
    score_by_day = {r.day: r.score for r in readiness}
    ef_residual = {
        reading.day: reading.deviation
        for reading in anomaly.robust_z_series(analytics.efficiency_factor(runs))
    }
    paired = [
        (score_by_day[day], residual)
        for day, residual in ef_residual.items()
        if day in score_by_day
    ]
    if len(paired) < _MIN_PAIRS:
        return None
    r = _pearson([p[0] for p in paired], [p[1] for p in paired])
    return round(r, 2) if r is not None else None
