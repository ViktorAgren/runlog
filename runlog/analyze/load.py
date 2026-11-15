"""Training-load structure: Foster monotony, strain, and weekly ramp rate.

Monotony is a week's mean daily load divided by its SD (high = little day-to-day
variation); strain is monotony times the weekly load; ramp rate is the
week-over-week change in weekly load. Foster (1998). These describe load
*structure* that raw volume misses. Treat them as descriptive context, not as an
injury-risk threshold — the evidence for load-ratio injury cut-offs is contested.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import timedelta
from typing import TYPE_CHECKING

from runlog.analyze.analytics import fill_daily

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

_WEEK_DAYS = 7


def _full_weeks(
    daily: Sequence[tuple[date, float]],
) -> list[tuple[date, list[float]]]:
    """Gap-filled daily loads grouped into complete Monday-start weeks."""
    by_week: dict[date, list[float]] = defaultdict(list)
    for day, load in fill_daily(daily):
        monday = day - timedelta(days=day.weekday())
        by_week[monday].append(load)
    return [
        (monday, loads)
        for monday, loads in sorted(by_week.items())
        if len(loads) == _WEEK_DAYS
    ]


def _monotony(loads: Sequence[float]) -> float | None:
    """Mean / SD of the week's daily loads, or None when the SD is zero."""
    sd = statistics.pstdev(loads)
    if sd == 0:
        return None
    return statistics.mean(loads) / sd


def monotony_series(daily: Sequence[tuple[date, float]]) -> list[tuple[date, float]]:
    """Weekly training monotony (mean/SD of daily load)."""
    return [
        (monday, round(value, 3))
        for monday, loads in _full_weeks(daily)
        if (value := _monotony(loads)) is not None
    ]


def strain_series(daily: Sequence[tuple[date, float]]) -> list[tuple[date, float]]:
    """Weekly training strain (weekly load * monotony)."""
    series: list[tuple[date, float]] = []
    for monday, loads in _full_weeks(daily):
        value = _monotony(loads)
        if value is not None:
            series.append((monday, round(sum(loads) * value, 1)))
    return series


def ramp_series(daily: Sequence[tuple[date, float]]) -> list[tuple[date, float]]:
    """Week-over-week change in weekly load, as a percent."""
    series: list[tuple[date, float]] = []
    previous: float | None = None
    for monday, loads in _full_weeks(daily):
        total = sum(loads)
        if previous is not None and previous > 0:
            series.append((monday, round(100 * (total - previous) / previous, 1)))
        previous = total
    return series
