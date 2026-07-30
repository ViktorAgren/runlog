"""Deterministic progress report: what the athlete actually did since a plan
started.

The actuals analogue of :mod:`runlog.plan.profile`. Where ``build_profile``
snapshots *current* fitness to ground a new plan, ``build_progress`` summarizes
*what happened* over the block window so a follow-up review can compare it to
the plan. Pure aggregation over the analysis layer — no API, no plan parsing.
"""

from __future__ import annotations

import itertools
import math
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from runlog.analyze import analytics, anomaly, metrics, physiology, streams
from runlog.plan import profile

if TYPE_CHECKING:
    import sqlite3

    from runlog.analyze.metrics import Run

# A run is "Quality" when this share of time sits in Z4-5, "Easy" when most time
# is in Z1-2, else "Moderate" (mostly Z3 — often easy running under %HRmax zones).
_QUALITY_HARD_FRACTION = 30.0
_EASY_FRACTION = 50.0
# Rep-set detection: ignore sub-200 m transition laps, and treat every lap
# within 15% of the fastest lap's pace as a "work" rep (warm-up, jogs and
# cool-down are all clearly slower, so this isolates the reps).
_MIN_REP_M = 200.0
_REP_PACE_TOLERANCE = 1.15


@dataclass(frozen=True)
class LapSplit:
    """One lap/rep of a structured workout, as stored (from Apple segments)."""

    index: int
    seconds: int | None
    distance_m: float | None
    pace_s_per_km: float | None
    avg_hr: float | None
    max_hr: float | None = None  # peak HR within the lap, from the point stream


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
    # Rep-set fatigue signals (None unless it's a structured session): avg HR of
    # the last work rep minus the first, and the coefficient of variation of the
    # work reps' paces (how even the set was).
    rep_hr_drift: float | None = None
    rep_pace_cv: float | None = None
    # Running dynamics (Apple) and physiology, per run — None when unavailable.
    avg_cadence: float | None = None
    avg_power_w: float | None = None
    avg_stride_length_m: float | None = None
    cardiac_drift_pct: float | None = None  # +ve = HR drifted up for same pace
    running_economy: float | None = None  # m/s per watt


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
    advanced: profile.AdvancedFitness | None = None


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


def _laps(
    conn: sqlite3.Connection, run: Run, stream: list[streams.StreamSample]
) -> tuple[LapSplit, ...]:
    """Stored lap splits for a run (only when it has a structured set).

    Per-lap max HR is recovered by slicing the HR stream at the laps' cumulative
    end times, since the ``laps`` table stores only the average.
    """
    rows = conn.execute(
        """
        SELECT lap_index, elapsed_s, distance_m, avg_pace_s_per_km, avg_hr
        FROM laps WHERE activity_id = ? ORDER BY lap_index
        """,
        (int(run.activity_id),),
    ).fetchall()
    if len(rows) < 3:
        return ()
    ends = list(itertools.accumulate(int(row["elapsed_s"] or 0) for row in rows))
    max_hrs = streams.lap_hr_stats(stream, ends) if stream else [None] * len(rows)
    return tuple(
        LapSplit(
            index=int(row["lap_index"]),
            seconds=row["elapsed_s"],
            distance_m=row["distance_m"],
            pace_s_per_km=row["avg_pace_s_per_km"],
            avg_hr=row["avg_hr"],
            max_hr=max_hrs[i],
        )
        for i, row in enumerate(rows)
    )


def _rep_set_stats(
    laps: tuple[LapSplit, ...],
) -> tuple[float | None, float | None]:
    """HR drift (last minus first work rep) and pace CV over the work reps.

    Isolates the "work" reps from warm-up/jog/cool-down laps: drops sub-200 m
    transitions, then keeps laps within 15% of the fastest lap's pace (jogs,
    warm-up and cool-down are all clearly slower). Returns ``(None, None)`` when
    there is no clear rep set.
    """
    reps = [
        (lap.pace_s_per_km, lap.avg_hr)
        for lap in laps
        if lap.pace_s_per_km is not None
        and lap.distance_m is not None
        and lap.distance_m >= _MIN_REP_M
    ]
    if len(reps) < 3:
        return None, None
    fastest = min(pace for pace, _ in reps)
    work = [(pace, hr) for pace, hr in reps if pace <= fastest * _REP_PACE_TOLERANCE]
    if len(work) < 2:
        return None, None
    paces = [pace for pace, _ in work]
    hrs = [hr for _, hr in work if hr is not None]
    mean_pace = statistics.mean(paces)
    drift = round(hrs[-1] - hrs[0], 1) if len(hrs) >= 2 else None
    cv = round(statistics.pstdev(paces) / mean_pace * 100, 1) if mean_pace else None
    return drift, cv


def workout_detail(conn: sqlite3.Connection, run: Run, hr_max: float) -> WorkoutDetail:
    stream = streams.full_stream(conn, run.activity_id)
    zones = _zone_pcts(stream, hr_max) if stream else None
    pacing = streams.pacing_stats(stream) if stream else None
    climb = streams.climb_stats(stream) if stream else None
    laps = _laps(conn, run, stream)
    rep_hr_drift, rep_pace_cv = _rep_set_stats(laps)
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
        laps=laps,
        rep_hr_drift=rep_hr_drift,
        rep_pace_cv=rep_pace_cv,
        avg_cadence=run.avg_cadence,
        avg_power_w=run.avg_power_w,
        avg_stride_length_m=run.avg_stride_length_m,
        cardiac_drift_pct=physiology.cardiac_drift_pct(stream) if stream else None,
        running_economy=metrics.running_economy(run),
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
    hr_rest = metrics.resting_hr_median(conn)
    daily_load = analytics.daily_trimp(all_runs, hr_max_value, hr_rest)
    pmc = analytics.performance_management(daily_load)
    acwr = analytics.acwr_series(daily_load)
    ef_trend = analytics.linear_trend(analytics.efficiency_factor(window))
    intensity = physiology.training_intensity_distribution(conn, window, hr_max_value)
    flags = anomaly.analyze(conn, window, since=start)
    best = _plausible_best_efforts(analytics.best_effort_progressions(conn, window))
    workouts = [workout_detail(conn, run, hr_max_value) for run in window]

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
        advanced=profile.build_advanced_fitness(conn, all_runs),
    )
