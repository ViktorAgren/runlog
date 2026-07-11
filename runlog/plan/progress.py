"""Deterministic progress report: what the athlete actually did since a plan
started.

The actuals analogue of :mod:`runlog.plan.profile`. Where ``build_profile``
snapshots *current* fitness to ground a new plan, ``build_progress`` summarizes
*what happened* over the block window so a follow-up review can compare it to
the plan. Pure aggregation over the analysis layer — no API, no plan parsing.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from runlog.analyze import analytics, anomaly, metrics, physiology, streams

if TYPE_CHECKING:
    import sqlite3

    from runlog.analyze.metrics import Run

_DEFAULT_HR_REST = 50.0
# A run is "Quality" when this share of time sits in Z4-5, "Easy" when most time
# is in Z1-2, else "Moderate" (mostly Z3 — often easy running under %HRmax zones).
_QUALITY_HARD_FRACTION = 30.0
_EASY_FRACTION = 50.0


@dataclass(frozen=True)
class LapSplit:
    """One lap/rep of a structured workout, as stored (from Apple segments)."""

    index: int
    seconds: int | None
    distance_m: float | None
    pace_s_per_km: float | None
    avg_hr: float | None


@dataclass(frozen=True)
class WorkoutDetail:
    """One performed run, in enough detail to compare to a planned session."""

    day: date
    weekday: str
    kind: str  # Recovery / Easy / Moderate / Quality
    distance_km: float | None
    moving_s: int | None
    avg_pace_s_per_km: float | None
    avg_hr: float | None
    max_hr: float | None
    easy_pct: float | None
    moderate_pct: float | None
    hard_pct: float | None
    gap_pace_s_per_km: float | None
    negative_split_pct: float | None
    elevation_gain_m: float | None
    laps: tuple[LapSplit, ...] = ()


@dataclass(frozen=True)
class PlanWeek:
    """Actuals bucketed to a plan week (week 1 = start .. start+7)."""

    week: int
    km: float
    runs: int
    kinds: dict[str, int]


@dataclass(frozen=True)
class ProgressReport:
    """What actually happened between a plan's start date and today."""

    start: date
    today: date
    weeks_elapsed: int
    total_runs: int
    total_km: float
    weekly_km: list[float]
    runs_per_week: float
    longest_run_km: float
    median_pace_s_per_km: float | None
    easy_pct: float | None
    moderate_pct: float | None
    hard_pct: float | None
    best_efforts: dict[str, float] = field(default_factory=dict)
    ctl_start: float | None = None
    ctl_now: float | None = None
    atl_now: float | None = None
    tsb_now: float | None = None
    acwr_now: float | None = None
    efficiency_trend_per_month: float | None = None
    red_flag_days: int = 0
    off_runs: int = 0
    workouts: list[WorkoutDetail] = field(default_factory=list)
    plan_weeks: list[PlanWeek] = field(default_factory=list)


def _resting_hr(conn: sqlite3.Connection) -> float:
    daily = metrics.daily_means(metrics.metric_series(conn, "resting_hr"))
    return statistics.median(v for _, v in daily) if daily else _DEFAULT_HR_REST


def _ctl_at(points: list[analytics.PmcPoint], start: date) -> float | None:
    """Fitness (CTL) on the last modelled day at or before ``start``."""
    before = [p.fitness for p in points if p.day <= start]
    if before:
        return round(before[-1], 1)
    return round(points[0].fitness, 1) if points else None


def _plausible_best_efforts(
    progressions: list[analytics.BestEffortProgression],
) -> dict[str, float]:
    """Best time per distance, dropping degenerate streams (e.g. a 1k in 0:00)."""
    low_pace, _high = metrics.PLAUSIBLE_PACE_S_PER_KM
    best: dict[str, float] = {}
    for effort in progressions:
        seconds = min(s for _, s in effort.progression)
        if seconds / (effort.distance_m / 1000) >= low_pace:
            best[effort.label] = seconds
    return best


def _zone_pcts(
    stream: list[streams.StreamSample], hr_max: float
) -> tuple[float, float, float] | None:
    """(easy Z1-2, moderate Z3, hard Z4-5) share of time, or None without HR."""
    seconds = physiology.zone_seconds_for_run(stream, hr_max)
    total = sum(seconds)
    if total <= 0:
        return None
    return (
        round((seconds[0] + seconds[1]) / total * 100, 1),
        round(seconds[2] / total * 100, 1),
        round((seconds[3] + seconds[4]) / total * 100, 1),
    )


def _classify(zones: tuple[float, float, float] | None) -> str:
    """Label a run from its HR-zone split."""
    if zones is None:
        return "Run"
    easy, moderate, hard = zones
    if hard >= _QUALITY_HARD_FRACTION:
        return "Quality"
    if easy >= _EASY_FRACTION:
        return "Easy"
    return "Moderate"


def _laps(conn: sqlite3.Connection, run: Run) -> tuple[LapSplit, ...]:
    """Stored lap splits for a run (only when it has a structured set)."""
    rows = conn.execute(
        """
        SELECT lap_index, elapsed_s, distance_m, avg_pace_s_per_km, avg_hr
        FROM laps WHERE activity_id = ? ORDER BY lap_index
        """,
        (int(run.activity_id),),
    ).fetchall()
    if len(rows) < 3:
        return ()
    return tuple(
        LapSplit(
            index=int(row["lap_index"]),
            seconds=row["elapsed_s"],
            distance_m=row["distance_m"],
            pace_s_per_km=row["avg_pace_s_per_km"],
            avg_hr=row["avg_hr"],
        )
        for row in rows
    )


def _workout_detail(conn: sqlite3.Connection, run: Run, hr_max: float) -> WorkoutDetail:
    stream = streams.full_stream(conn, run.activity_id)
    zones = _zone_pcts(stream, hr_max) if stream else None
    pacing = streams.pacing_stats(stream) if stream else None
    climb = streams.climb_stats(stream) if stream else None
    return WorkoutDetail(
        day=run.start.date(),
        weekday=run.start.strftime("%a"),
        kind=_classify(zones),
        distance_km=run.distance_km,
        moving_s=run.moving_s,
        avg_pace_s_per_km=run.avg_pace_s_per_km,
        avg_hr=run.avg_hr,
        max_hr=run.max_hr,
        easy_pct=zones[0] if zones else None,
        moderate_pct=zones[1] if zones else None,
        hard_pct=zones[2] if zones else None,
        gap_pace_s_per_km=(
            round(gap)
            if (gap := streams.grade_adjusted_pace_s_per_km(stream))
            else None
        ),
        negative_split_pct=pacing.negative_split_pct if pacing else None,
        elevation_gain_m=round(climb.ascent_m) if climb else run.elevation_gain_m,
        laps=_laps(conn, run),
    )


def _plan_weeks(start: date, workouts: list[WorkoutDetail]) -> list[PlanWeek]:
    """Bucket performed workouts by plan week (week 1 = start .. start+6)."""
    weeks: dict[int, list[WorkoutDetail]] = {}
    for workout in workouts:
        index = (workout.day - start).days // 7 + 1
        weeks.setdefault(index, []).append(workout)
    result: list[PlanWeek] = []
    for index in sorted(weeks):
        members = weeks[index]
        kinds: dict[str, int] = {}
        for member in members:
            kinds[member.kind] = kinds.get(member.kind, 0) + 1
        result.append(
            PlanWeek(
                week=index,
                km=round(sum(w.distance_km or 0.0 for w in members), 1),
                runs=len(members),
                kinds=kinds,
            )
        )
    return result


def build_progress(
    conn: sqlite3.Connection,
    start: date,
    hr_max: float | None = None,
    today: date | None = None,
) -> ProgressReport:
    """Summarize training between ``start`` and ``today`` for a plan follow-up."""
    today = today or date.today()
    all_runs = metrics.canonical_run_activities(conn)
    window = metrics.canonical_run_activities(conn, since=start)
    weekly = metrics.weekly_volume(window)
    consistency = metrics.consistency_summary(window)
    overall = metrics.overall_summary(window)
    paces = [p.pace_s_per_km for p in metrics.pace_points(window)]

    hr_samples = metrics.hr_samples(conn, [r.activity_id for r in all_runs])
    hr_max_value = hr_max if hr_max else metrics.estimated_hr_max(hr_samples)
    hr_rest = _resting_hr(conn)
    daily_load = analytics.daily_trimp(all_runs, hr_max_value, hr_rest)
    pmc = analytics.performance_management(daily_load)
    acwr = analytics.acwr_series(daily_load)
    ef_trend = analytics.linear_trend(analytics.efficiency_factor(window))
    intensity = physiology.training_intensity_distribution(conn, window, hr_max_value)
    flags = anomaly.analyze(conn, window, since=start)
    best = _plausible_best_efforts(analytics.best_effort_progressions(conn, window))
    workouts = [_workout_detail(conn, run, hr_max_value) for run in window]

    return ProgressReport(
        start=start,
        today=today,
        weeks_elapsed=max(1, math.ceil((today - start).days / 7)),
        total_runs=overall.run_count,
        total_km=overall.total_km,
        weekly_km=[w.distance_km for w in weekly],
        runs_per_week=consistency.runs_per_week,
        longest_run_km=overall.longest_km,
        median_pace_s_per_km=round(statistics.median(paces), 1) if paces else None,
        easy_pct=intensity.easy_pct if intensity else None,
        moderate_pct=intensity.moderate_pct if intensity else None,
        hard_pct=intensity.hard_pct if intensity else None,
        best_efforts=best,
        ctl_start=_ctl_at(pmc, start),
        ctl_now=round(pmc[-1].fitness, 1) if pmc else None,
        atl_now=round(pmc[-1].fatigue, 1) if pmc else None,
        tsb_now=round(pmc[-1].form, 1) if pmc else None,
        acwr_now=acwr[-1][1] if acwr else None,
        efficiency_trend_per_month=(
            round(ef_trend.per_30_days, 4) if ef_trend else None
        ),
        red_flag_days=len(flags.red_flag_days),
        off_runs=len(flags.performance),
        workouts=workouts,
        plan_weeks=_plan_weeks(start, workouts),
    )
