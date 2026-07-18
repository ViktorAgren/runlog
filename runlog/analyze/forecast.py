"""Race-time forecast that updates as fitness changes.

Estimator: each run's best 1 km (from streams) is Riegel-scaled to the race
distance, giving a per-run race-equivalent time; the fastest such time per ISO
week over a trailing window is one honest fitness sample. A linear trend over
those weekly minima is projected to race day, with a 95% band from the slope
CI. When there is too little data to fit a trend, it falls back to a plain
Riegel projection of the current best (no band). Projections are clamped so
they can never promise more than 5% faster than the current best.

Pure and read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from runlog.analyze import analytics, stats

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import date

    from runlog.analyze.metrics import Run

_RIEGEL_EXP = 1.06
_WINDOW_DAYS = 91  # ~13 weeks of trailing fitness samples
_MIN_WEEKS = 6  # below this, fall back to a point Riegel estimate
_FLOOR_FRACTION = 0.95  # projections never beat 95% of the current best


@dataclass(frozen=True)
class RaceForecast:
    race_day: date
    distance_m: float
    predicted_s: float
    ci_low_s: float | None  # None for the riegel-current fallback
    ci_high_s: float | None
    method: str  # "trend" | "riegel-current"
    n_weeks: int
    current_best_s: float


def format_race_time(seconds: float) -> str:
    """Race time as ``M:SS`` (or ``H:MM:SS`` for an hour or more)."""
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _riegel(seconds_1k: float, distance_m: float) -> float:
    return float(seconds_1k * (distance_m / 1000.0) ** _RIEGEL_EXP)


def _weekly_minima(
    points: Sequence[tuple[date, float]],
) -> list[tuple[date, float]]:
    """Fastest race-equivalent per ISO week, dated to that week's Monday."""
    best: dict[date, float] = {}
    for day, seconds in points:
        monday = day - timedelta(days=day.weekday())
        if monday not in best or seconds < best[monday]:
            best[monday] = seconds
    return sorted(best.items())


def race_equivalent_series(
    conn: sqlite3.Connection,
    runs: Sequence[Run],
    distance_m: float,
    today: date,
    window_days: int = _WINDOW_DAYS,
) -> list[tuple[date, float]]:
    """Weekly-best race-equivalent times over the trailing window."""
    since = today - timedelta(days=window_days)
    efforts = analytics.run_effort_series(conn, runs, 1000.0, since=since)
    equivalent = [(day, _riegel(seconds, distance_m)) for day, seconds in efforts]
    return _weekly_minima(equivalent)


def _forecast_from_series(
    series: Sequence[tuple[date, float]],
    race_day: date,
    distance_m: float,
    today: date,
) -> RaceForecast | None:
    if not series:
        return None
    current_best = min(seconds for _, seconds in series)
    floor = _FLOOR_FRACTION * current_best
    n_weeks = len(series)
    trend = analytics.linear_trend(series)
    test = stats.trend_test(series)
    if n_weeks < _MIN_WEEKS or trend is None or test is None:
        return RaceForecast(
            race_day=race_day,
            distance_m=distance_m,
            predicted_s=current_best,
            ci_low_s=None,
            ci_high_s=None,
            method="riegel-current",
            n_weeks=n_weeks,
            current_best_s=current_best,
        )
    base = series[0][0].toordinal()
    center_today = trend.intercept + trend.slope_per_day * (today.toordinal() - base)
    horizon = (race_day - today).days
    predicted = max(center_today + trend.slope_per_day * horizon, floor)
    low = center_today + test.ci_low_per_day * horizon
    high = center_today + test.ci_high_per_day * horizon
    ci_low = max(min(low, high), floor)
    ci_high = max(max(low, high), floor)
    return RaceForecast(
        race_day=race_day,
        distance_m=distance_m,
        predicted_s=predicted,
        ci_low_s=ci_low,
        ci_high_s=ci_high,
        method="trend",
        n_weeks=n_weeks,
        current_best_s=current_best,
    )


def race_forecast(
    conn: sqlite3.Connection,
    runs: Sequence[Run],
    race_day: date,
    distance_m: float,
    today: date,
) -> RaceForecast | None:
    """Project a race time to ``race_day``; None when no 1 km efforts exist."""
    series = race_equivalent_series(conn, runs, distance_m, today)
    return _forecast_from_series(series, race_day, distance_m, today)
