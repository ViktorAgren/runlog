"""Rolling-baseline anomaly detection over health and performance series.

Pure and read-only. Each metric is judged against its own trailing baseline
(robust median +/- MAD), so a reading is "anomalous" only relative to the
athlete's recent norm -- not a fixed population threshold. Two families:

* *Health/readiness* -- elevated resting HR, depressed HRV, low SpO2, short
  sleep, or blunted HR-recovery. When two or more fire on the same day it is a
  ``RedFlagDay`` (a plausible illness / overtraining signal).
* *Performance* -- runs whose efficiency factor drops well below its rolling
  baseline (unexpectedly slow for the effort).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from runlog.analyze import analytics, metrics

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import date

    from runlog.analyze.metrics import Run

# Trailing window used as each reading's baseline, and the minimum number of
# prior readings required before a day can be judged at all.
_BASELINE_DAYS = 42
_MIN_BASELINE = 7
# Threshold in robust-sigma (MAD-scaled) units; ~2.5 keeps false positives low.
_DEFAULT_K = 2.5
# Scale factor making the MAD a consistent estimator of the standard deviation.
_MAD_TO_SIGMA = 1.4826
# A red-flag day needs at least this many distinct health signals together.
_RED_FLAG_SIGNALS = 2


class Direction(StrEnum):
    """Which tail of a metric is the concern."""

    HIGH = "high"  # readings ABOVE baseline are bad (e.g. resting HR)
    LOW = "low"  # readings BELOW baseline are bad (e.g. HRV, SpO2)


# Health markers and the direction that signals reduced readiness.
_HEALTH_MARKERS: tuple[tuple[str, Direction], ...] = (
    ("resting_hr", Direction.HIGH),
    ("hrv_sdnn", Direction.LOW),
    ("spo2", Direction.LOW),
    ("sleep_hours", Direction.LOW),
    ("hr_recovery_1min", Direction.LOW),
)


@dataclass(frozen=True)
class Anomaly:
    day: date
    metric: str
    value: float
    baseline: float
    deviation: float  # signed robust z-score against the trailing baseline
    direction: Direction


@dataclass(frozen=True)
class RedFlagDay:
    day: date
    metrics: tuple[str, ...]


@dataclass(frozen=True)
class AnomalyReport:
    health: list[Anomaly]
    red_flag_days: list[RedFlagDay]
    performance: list[Anomaly]


def _robust_scale(values: Sequence[float], center: float) -> float:
    """MAD-based sigma estimate, falling back to stdev when the MAD is zero."""
    mad = statistics.median([abs(v - center) for v in values])
    scale = _MAD_TO_SIGMA * mad
    if scale == 0 and len(values) > 1:
        scale = statistics.pstdev(values)
    return scale


@dataclass(frozen=True)
class Reading:
    """One reading scored against its trailing baseline (median +/- MAD)."""

    day: date
    value: float
    center: float  # trailing-baseline median
    deviation: float  # signed robust z-score (value - center) / scale


def robust_z_series(
    series: Sequence[tuple[date, float]],
    *,
    window_days: int = _BASELINE_DAYS,
    min_baseline: int = _MIN_BASELINE,
) -> list[Reading]:
    """Signed robust z-score of each reading vs. its trailing baseline.

    The shared core behind :func:`detect_series` (which just thresholds these)
    and the readiness score (which averages them across markers). Days without
    enough prior baseline, or with a degenerate zero scale, are skipped.
    """
    ordered = sorted(series)
    readings: list[Reading] = []
    for i, (day, value) in enumerate(ordered):
        window_start = day - timedelta(days=window_days)
        baseline = [v for d, v in ordered[:i] if d >= window_start]
        if len(baseline) < min_baseline:
            continue
        center = statistics.median(baseline)
        scale = _robust_scale(baseline, center)
        if scale == 0:
            continue
        readings.append(
            Reading(
                day=day, value=value, center=center, deviation=(value - center) / scale
            )
        )
    return readings


def detect_series(
    series: Sequence[tuple[date, float]],
    metric: str,
    direction: Direction,
    *,
    window_days: int = _BASELINE_DAYS,
    k: float = _DEFAULT_K,
    min_baseline: int = _MIN_BASELINE,
) -> list[Anomaly]:
    """Flag readings that deviate from their trailing baseline in ``direction``."""
    anomalies: list[Anomaly] = []
    for reading in robust_z_series(
        series, window_days=window_days, min_baseline=min_baseline
    ):
        flagged = (
            reading.deviation >= k
            if direction is Direction.HIGH
            else reading.deviation <= -k
        )
        if flagged:
            anomalies.append(
                Anomaly(
                    day=reading.day,
                    metric=metric,
                    value=reading.value,
                    baseline=round(reading.center, 2),
                    deviation=round(reading.deviation, 2),
                    direction=direction,
                )
            )
    return anomalies


def health_anomalies(
    conn: sqlite3.Connection, since: date | None = None, k: float = _DEFAULT_K
) -> list[Anomaly]:
    """Readiness anomalies across all curated health markers, date-ordered."""
    found: list[Anomaly] = []
    for metric, direction in _HEALTH_MARKERS:
        daily = metrics.daily_means(metrics.metric_series(conn, metric, since=since))
        found.extend(detect_series(daily, metric, direction, k=k))
    return sorted(found, key=lambda a: (a.day, a.metric))


def red_flag_days(
    anomalies: Sequence[Anomaly], min_signals: int = _RED_FLAG_SIGNALS
) -> list[RedFlagDay]:
    """Days where several distinct health signals fire together."""
    by_day: dict[date, set[str]] = defaultdict(set)
    for anomaly in anomalies:
        by_day[anomaly.day].add(anomaly.metric)
    return [
        RedFlagDay(day=day, metrics=tuple(sorted(signals)))
        for day, signals in sorted(by_day.items())
        if len(signals) >= min_signals
    ]


def performance_anomalies(runs: Sequence[Run], k: float = _DEFAULT_K) -> list[Anomaly]:
    """Runs whose efficiency factor drops well below its rolling baseline."""
    return detect_series(
        analytics.efficiency_factor(runs), "efficiency_factor", Direction.LOW, k=k
    )


def analyze(
    conn: sqlite3.Connection,
    runs: Sequence[Run],
    since: date | None = None,
    k: float = _DEFAULT_K,
) -> AnomalyReport:
    """Full anomaly report: health markers, red-flag days, and performance."""
    health = health_anomalies(conn, since=since, k=k)
    return AnomalyReport(
        health=health,
        red_flag_days=red_flag_days(health),
        performance=performance_anomalies(runs, k=k),
    )
