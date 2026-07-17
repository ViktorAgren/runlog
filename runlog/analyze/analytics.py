"""High-level derived training analytics (the calculations, not raw plots).

Everything here is pure and read-only. It builds on :mod:`runlog.analyze.
metrics` for canonical runs and adds the heavier models:

* Banister TRIMP -> Performance Management Chart (Fitness/Fatigue/Form).
* Acute:chronic workload ratio (injury-risk indicator).
* Efficiency factor (speed per heartbeat) with a fitted linear trend.
* Aerobic decoupling (cardiac drift) from the HR + velocity streams.
* True best efforts (fastest continuous 1k/5k/10k) via a sliding window over
  the per-point distance/time streams.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from runlog.analyze.metrics import PLAUSIBLE_PACE_S_PER_KM

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from runlog.analyze.metrics import Run
    from runlog.domain import ActivityId

# Banister TRIMP (male coefficients); HRr is the heart-rate reserve fraction.
_TRIMP_A = 0.64
_TRIMP_B = 1.92
_CTL_DAYS = 42  # "Fitness" time constant
_ATL_DAYS = 7  # "Fatigue" time constant
_ACWR_ACUTE_DAYS = 7
_ACWR_CHRONIC_DAYS = 28
_BEST_EFFORT_DISTANCES_M: tuple[tuple[str, float], ...] = (
    ("1k", 1000.0),
    ("5k", 5000.0),
    ("10k", 10000.0),
)


# --- Linear trend -----------------------------------------------------------


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
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


@dataclass(frozen=True)
class Trend:
    slope_per_day: float
    intercept: float
    r: float
    fitted: list[tuple[date, float]]

    @property
    def per_30_days(self) -> float:
        return self.slope_per_day * 30


def linear_trend(points: Sequence[tuple[date, float]]) -> Trend | None:
    """Ordinary least-squares fit of value vs. day; None if under two points."""
    if len(points) < 2:
        return None
    base = points[0][0].toordinal()
    xs = [p[0].toordinal() - base for p in points]
    ys = [p[1] for p in points]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    syy = sum((y - mean_y) ** 2 for y in ys)
    slope = sxy / sxx if sxx else 0.0
    intercept = mean_y - slope * mean_x
    r = sxy / math.sqrt(sxx * syy) if sxx and syy else 0.0
    fitted = [(p[0], slope * x + intercept) for p, x in zip(points, xs, strict=True)]
    return Trend(slope_per_day=slope, intercept=intercept, r=r, fitted=fitted)


# --- TRIMP & Performance Management Chart ------------------------------------


def run_trimp(run: Run, hr_max: float, hr_rest: float) -> float | None:
    """Banister training impulse for one run (needs avg HR and duration)."""
    if run.avg_hr is None or not run.moving_s or hr_max <= hr_rest:
        return None
    reserve = (run.avg_hr - hr_rest) / (hr_max - hr_rest)
    reserve = max(0.0, min(1.0, reserve))
    minutes = run.moving_s / 60
    return minutes * reserve * _TRIMP_A * math.exp(_TRIMP_B * reserve)


def daily_trimp(
    runs: Sequence[Run], hr_max: float, hr_rest: float
) -> list[tuple[date, float]]:
    """Total TRIMP per calendar day (only days with a scored run)."""
    totals: dict[date, float] = {}
    for run in runs:
        score = run_trimp(run, hr_max, hr_rest)
        if score is not None:
            totals[run.start.date()] = totals.get(run.start.date(), 0.0) + score
    return sorted(totals.items())


def fill_daily(daily: Sequence[tuple[date, float]]) -> list[tuple[date, float]]:
    """Expand a sparse daily series to every calendar day (0 on rest days)."""
    if not daily:
        return []
    lookup = dict(daily)
    day, last = daily[0][0], daily[-1][0]
    filled: list[tuple[date, float]] = []
    while day <= last:
        filled.append((day, lookup.get(day, 0.0)))
        day += timedelta(days=1)
    return filled


@dataclass(frozen=True)
class PmcPoint:
    day: date
    fitness: float  # CTL
    fatigue: float  # ATL
    form: float  # TSB = yesterday's (CTL - ATL)


def performance_management(
    daily: Sequence[tuple[date, float]],
) -> list[PmcPoint]:
    """Fitness/Fatigue/Form via exponentially weighted TRIMP (the PMC)."""
    ctl = atl = 0.0
    points: list[PmcPoint] = []
    for day, load in fill_daily(daily):
        form = ctl - atl  # form reflects balance *before* today's load
        ctl += (load - ctl) / _CTL_DAYS
        atl += (load - atl) / _ATL_DAYS
        points.append(PmcPoint(day=day, fitness=ctl, fatigue=atl, form=form))
    return points


def acwr_series(
    daily: Sequence[tuple[date, float]],
) -> list[tuple[date, float]]:
    """Acute:chronic workload ratio (7-day vs 28-day mean daily TRIMP)."""
    filled = fill_daily(daily)
    loads = [load for _, load in filled]
    series: list[tuple[date, float]] = []
    for i, (day, _load) in enumerate(filled):
        if i + 1 < _ACWR_CHRONIC_DAYS:
            continue
        acute = sum(loads[i - _ACWR_ACUTE_DAYS + 1 : i + 1]) / _ACWR_ACUTE_DAYS
        chronic = sum(loads[i - _ACWR_CHRONIC_DAYS + 1 : i + 1]) / _ACWR_CHRONIC_DAYS
        if chronic > 0:
            series.append((day, round(acute / chronic, 2)))
    return series


# --- Efficiency factor ------------------------------------------------------


def efficiency_factor(runs: Sequence[Run]) -> list[tuple[date, float]]:
    """Speed-per-heartbeat (m/min per bpm x100) per run; rising = fitter."""
    points: list[tuple[date, float]] = []
    for run in runs:
        if run.avg_hr and run.moving_s and run.distance_m and run.avg_hr > 0:
            speed_m_per_min = run.distance_m / (run.moving_s / 60)
            points.append((run.start.date(), round(speed_m_per_min / run.avg_hr, 3)))
    return points


# --- Stream-based analytics -------------------------------------------------


def _stream(
    conn: sqlite3.Connection, activity_id: ActivityId
) -> list[tuple[int, float, float | None, float | None]]:
    """(offset_s, distance_m, hr, velocity_mps) for a run, ordered by position."""
    return [
        (int(row["offset_s"]), float(row["distance_m"]), row["hr"], row["velocity_mps"])
        for row in conn.execute(
            """
            SELECT offset_s, distance_m, hr, velocity_mps FROM stream_points
            WHERE activity_id = ? AND distance_m IS NOT NULL
            ORDER BY seq
            """,
            (int(activity_id),),
        )
    ]


def best_effort_seconds(
    points: Sequence[tuple[int, float]], target_m: float
) -> float | None:
    """Fastest time to cover ``target_m`` in a run via a sliding window.

    ``points`` are (offset_s, cumulative_distance_m) ordered by time. Windows
    implying an impossibly fast pace (a corrupted stream where distance jumps
    with near-zero time) are rejected via the shared plausibility floor.
    """
    low_pace, _high = PLAUSIBLE_PACE_S_PER_KM
    min_elapsed = low_pace * target_m / 1000
    best: float | None = None
    start = 0
    for end in range(len(points)):
        while points[end][1] - points[start][1] >= target_m:
            elapsed = points[end][0] - points[start][0]
            if elapsed >= min_elapsed and (best is None or elapsed < best):
                best = float(elapsed)
            start += 1
    return best


@dataclass(frozen=True)
class BestEffortProgression:
    label: str
    distance_m: float
    # (date, all-time-best seconds as of that date)
    progression: list[tuple[date, float]]


def best_effort_progressions(
    conn: sqlite3.Connection, runs: Sequence[Run]
) -> list[BestEffortProgression]:
    """All-time-best 1k/5k/10k over time, from per-point distance/time streams."""
    results: list[BestEffortProgression] = []
    for label, target in _BEST_EFFORT_DISTANCES_M:
        best_so_far: float | None = None
        curve: list[tuple[date, float]] = []
        for run in sorted(runs, key=lambda r: r.start):
            stream = [(o, d) for o, d, _hr, _v in _stream(conn, run.activity_id)]
            effort = best_effort_seconds(stream, target)
            if effort is None:
                continue
            if best_so_far is None or effort < best_so_far:
                best_so_far = effort
            curve.append((run.start.date(), best_so_far))
        if curve:
            results.append(
                BestEffortProgression(label=label, distance_m=target, progression=curve)
            )
    return results


def run_effort_series(
    conn: sqlite3.Connection,
    runs: Sequence[Run],
    target_m: float,
    since: date | None = None,
) -> list[tuple[date, float]]:
    """Per-run best effort at ``target_m`` (one point per qualifying run).

    Unlike :func:`best_effort_progressions`, this is NOT the monotone all-time
    staircase: it reports each run's own fastest ``target_m``, so a slower run
    yields a larger value. That makes the series usable for regression (a
    fitness *trend*, not a censored record).
    """
    points: list[tuple[date, float]] = []
    for run in sorted(runs, key=lambda r: r.start):
        day = run.start.date()
        if since is not None and day < since:
            continue
        stream = [(o, d) for o, d, _hr, _v in _stream(conn, run.activity_id)]
        effort = best_effort_seconds(stream, target_m)
        if effort is not None:
            points.append((day, effort))
    return points


def aerobic_decoupling(
    conn: sqlite3.Connection,
    runs: Sequence[Run],
    min_distance_km: float = 8.0,
    top_n: int = 20,
) -> list[tuple[date, float]]:
    """Cardiac drift %: how much speed-per-HR fades from first to second half.

    Positive means efficiency dropped in the second half (HR drifted up for the
    same pace) — the classic aerobic-decoupling signal on longer runs.
    """
    candidates = sorted(
        (r for r in runs if r.distance_km and r.distance_km >= min_distance_km),
        key=lambda r: r.start,
    )[-top_n:]
    series: list[tuple[date, float]] = []
    for run in candidates:
        stream = _stream(conn, run.activity_id)
        usable = [(hr, v) for _o, _d, hr, v in stream if hr and v]
        if len(usable) < 4:
            continue
        mid = len(usable) // 2
        ef_first = _mean_ef(usable[:mid])
        ef_second = _mean_ef(usable[mid:])
        if ef_first:
            series.append(
                (run.start.date(), round((ef_first - ef_second) / ef_first * 100, 1))
            )
    return series


def _mean_ef(samples: Sequence[tuple[float, float]]) -> float:
    """Mean speed-per-HR (velocity / HR) over (hr, velocity) samples."""
    ratios = [v / hr for hr, v in samples if hr > 0]
    return sum(ratios) / len(ratios) if ratios else 0.0
