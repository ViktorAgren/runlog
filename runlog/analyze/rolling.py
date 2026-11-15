"""Rolling (windowed) correlation between two dated series.

A single Pearson r summarizes a whole history; a rolling r(t) shows how a
relationship drifts over a season — e.g. whether training load's coupling to
next-day HRV strengthens or fades. Each day's r is computed over a trailing
window of paired points, with the marker optionally lagged behind the driver.
Exploratory: a rolling correlation describes association, not causation.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from runlog.analyze import analytics

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date


def rolling_correlation(
    driver: Sequence[tuple[date, float]],
    marker: Sequence[tuple[date, float]],
    *,
    window_days: int = 42,
    lag_days: int = 1,
    min_points: int = 10,
) -> list[tuple[date, float]]:
    """Trailing-window Pearson r(t) between ``driver`` and a lagged ``marker``.

    Pairs ``driver`` on day ``d`` with ``marker`` on day ``d + lag_days``, then
    for each driver day computes r over the pairs within the trailing window.
    """
    marker_by_day = dict(marker)
    pairs: list[tuple[date, float, float]] = []
    for day, value in driver:
        lagged = marker_by_day.get(day + timedelta(days=lag_days))
        if lagged is not None:
            pairs.append((day, value, lagged))
    pairs.sort()
    series: list[tuple[date, float]] = []
    for day, _, _ in pairs:
        start = day - timedelta(days=window_days)
        window = [(x, y) for other, x, y in pairs if start <= other <= day]
        if len(window) < min_points:
            continue
        r = analytics.pearson([x for x, _ in window], [y for _, y in window])
        if r is not None:
            series.append((day, round(r, 3)))
    return series
