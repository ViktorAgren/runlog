"""Passive daily-life patterns: steps, energy, and sleep rhythm.

These signals live in their own ``lifestyle/`` outputs and summary block,
never mixed into the training analysis. The training-day contrast is an
explicit comparison against the set of days with any recorded workout.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from runlog.analyze import metrics

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

_RECENT_DAYS = 30
_WEEKEND = (5, 6)  # Saturday, Sunday


@dataclass(frozen=True)
class DayContrast:
    training_mean: float
    rest_mean: float
    training_n: int
    rest_n: int


@dataclass(frozen=True)
class WeekdayProfile:
    # Mon..Sun means; None where a weekday has no data.
    steps: tuple[float | None, ...]
    sleep_hours: tuple[float | None, ...]


@dataclass(frozen=True)
class LifestyleSummary:
    steps_30d: float | None
    sleep_30d: float | None
    sleep_sd_30d: float | None  # night-to-night pstdev, trailing window
    weekend_sleep_shift_h: float | None  # weekend mean minus weekday mean
    steps_contrast: DayContrast | None  # training days vs rest days


def weekday_means(
    daily: Sequence[tuple[date, float]],
) -> tuple[float | None, ...]:
    """Mean value per weekday (Mon..Sun); None for weekdays without data."""
    buckets: dict[int, list[float]] = {i: [] for i in range(7)}
    for day, value in daily:
        buckets[day.weekday()].append(value)
    return tuple(
        round(statistics.mean(values), 2) if (values := buckets[i]) else None
        for i in range(7)
    )


def training_rest_contrast(
    daily: Sequence[tuple[date, float]], training_days: frozenset[date]
) -> DayContrast | None:
    """Mean on days with a workout vs without; None when either side is empty."""
    training = [value for day, value in daily if day in training_days]
    rest = [value for day, value in daily if day not in training_days]
    if not training or not rest:
        return None
    return DayContrast(
        training_mean=round(statistics.mean(training), 1),
        rest_mean=round(statistics.mean(rest), 1),
        training_n=len(training),
        rest_n=len(rest),
    )


def _trailing(
    daily: Sequence[tuple[date, float]], days: int, today: date | None
) -> list[float]:
    today = today or date.today()
    window_start = today - timedelta(days=days)
    return [value for day, value in daily if day >= window_start]


def trailing_mean(
    daily: Sequence[tuple[date, float]],
    days: int = _RECENT_DAYS,
    today: date | None = None,
) -> float | None:
    values = _trailing(daily, days, today)
    return round(statistics.mean(values), 2) if values else None


def trailing_pstdev(
    daily: Sequence[tuple[date, float]],
    days: int = _RECENT_DAYS,
    today: date | None = None,
) -> float | None:
    values = _trailing(daily, days, today)
    return round(statistics.pstdev(values), 2) if len(values) > 1 else None


def weekday_profile(
    steps_daily: Sequence[tuple[date, float]],
    sleep_daily: Sequence[tuple[date, float]],
) -> WeekdayProfile | None:
    """Mean steps and sleep per weekday; None when both series are empty."""
    if not steps_daily and not sleep_daily:
        return None
    return WeekdayProfile(
        steps=weekday_means(steps_daily),
        sleep_hours=weekday_means(sleep_daily),
    )


def _weekend_shift(sleep_by_weekday: tuple[float | None, ...]) -> float | None:
    weekend = [v for i in _WEEKEND if (v := sleep_by_weekday[i]) is not None]
    weekdays = [
        v
        for i in range(7)
        if i not in _WEEKEND
        if (v := sleep_by_weekday[i]) is not None
    ]
    if not weekend or not weekdays:
        return None
    return round(statistics.mean(weekend) - statistics.mean(weekdays), 1)


def build_lifestyle(
    conn: sqlite3.Connection,
    training_days: frozenset[date],
    since: date | None = None,
    today: date | None = None,
) -> LifestyleSummary:
    """Assemble the lifestyle summary from the daily health metrics."""
    steps = metrics.daily_means(metrics.metric_series(conn, "steps", since=since))
    sleep = metrics.daily_means(metrics.metric_series(conn, "sleep_hours", since=since))
    return LifestyleSummary(
        steps_30d=trailing_mean(steps, today=today),
        sleep_30d=trailing_mean(sleep, today=today),
        sleep_sd_30d=trailing_pstdev(sleep, today=today),
        weekend_sleep_shift_h=_weekend_shift(weekday_means(sleep)),
        steps_contrast=training_rest_contrast(steps, training_days),
    )
