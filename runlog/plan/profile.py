"""Build a compact athlete profile from the runlog DB to ground the plan.

This is the differentiator: rather than a generic template, the plan is built
from the athlete's real numbers — recent volume, typical pace, best efforts,
and current Fitness/Fatigue/Form — by reusing the analysis layer.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from runlog.analyze import analytics, cs, metrics
from runlog.plan import targets

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from runlog.analyze.analytics import EffortRecord
    from runlog.analyze.metrics import Run
    from runlog.plan.targets import TrainingZone

_DEFAULT_HR_REST = 50.0
# Volume/consistency describe *current* training, so use a recent window rather
# than the whole (possibly sparse early) history.
_RECENT_DAYS = 90


@dataclass(frozen=True)
class PlanRequest:
    """The goal and real-world constraints for a plan."""

    goal: str
    race_date: date
    training_days: tuple[str, ...]
    weeks_to_goal: int
    target_time: str | None = None
    max_distance_km: float | None = None
    max_time_min: int | None = None


@dataclass(frozen=True)
class AdvancedFitness:
    """Measured-performance signals shared by the planner and the review.

    The critical-speed model and recent best efforts anchor training pace to
    what the athlete actually runs, as a cross-check on the VDOT-derived zones;
    decoupling and cadence add aerobic-durability and form context.
    """

    critical_speed: cs.CsModel | None
    best_effort_records: list[EffortRecord]
    aerobic_decoupling: list[tuple[date, float]]
    avg_cadence: float | None


def build_advanced_fitness(
    conn: sqlite3.Connection, runs: Sequence[Run]
) -> AdvancedFitness:
    """Fit the critical-speed model and gather best efforts, decoupling, cadence."""
    cadences = [run.avg_cadence for run in runs if run.avg_cadence]
    return AdvancedFitness(
        critical_speed=cs.critical_speed(conn, runs),
        best_effort_records=analytics.best_effort_records(conn, runs),
        aerobic_decoupling=analytics.aerobic_decoupling(conn, runs),
        avg_cadence=round(statistics.mean(cadences), 1) if cadences else None,
    )


@dataclass(frozen=True)
class AthleteProfile:
    """A snapshot of current running fitness, grounded in stored data."""

    run_count: int
    total_km: float
    avg_weekly_km: float
    recent_weekly_km: list[float]
    runs_per_week: float
    longest_run_km: float
    longest_layoff_days: int
    typical_pace_s_per_km: float | None
    best_efforts: dict[str, float] = field(default_factory=dict)
    predicted_races: dict[str, float] = field(default_factory=dict)
    fitness_ctl: float | None = None
    fatigue_atl: float | None = None
    form_tsb: float | None = None
    acwr: float | None = None
    efficiency_trend_per_month: float | None = None
    vo2max: float | None = None
    resting_hr: float | None = None
    hr_max: float | None = None
    hr_rest: float | None = None
    vdot: float | None = None
    zones: list[TrainingZone] = field(default_factory=list)
    advanced: AdvancedFitness | None = None


def _median_resting_hr(conn: sqlite3.Connection) -> float:
    daily = metrics.daily_means(metrics.metric_series(conn, "resting_hr"))
    if not daily:
        return _DEFAULT_HR_REST
    return statistics.median(v for _, v in daily)


def _latest(series: Sequence[tuple[datetime, float]]) -> float | None:
    return series[-1][1] if series else None


def build_profile(
    conn: sqlite3.Connection,
    hr_max: float | None = None,
    today: date | None = None,
) -> AthleteProfile:
    """Assemble an :class:`AthleteProfile`.

    All-time data (totals, best efforts, current Fitness/Fatigue/Form) plus
    recent-window volume/consistency/pace. ``hr_max`` (the athlete's true max)
    anchors the Karvonen HR zones; falls back to the highest recorded sample.
    """
    today = today or date.today()
    runs = metrics.canonical_run_activities(conn)
    recent = metrics.canonical_run_activities(
        conn, since=today - timedelta(days=_RECENT_DAYS)
    )
    weekly = metrics.weekly_volume(recent)
    consistency = metrics.consistency_summary(recent)
    overall = metrics.overall_summary(runs)
    paces = [p.pace_s_per_km for p in metrics.pace_points(recent)]

    hr_samples = metrics.hr_samples(conn, [r.activity_id for r in runs])
    hr_max_value = hr_max if hr_max else metrics.estimated_hr_max(hr_samples)
    hr_rest = _median_resting_hr(conn)
    daily_load = analytics.daily_trimp(runs, hr_max_value, hr_rest)
    pmc = analytics.performance_management(daily_load)
    acwr = analytics.acwr_series(daily_load)
    ef_trend = analytics.linear_trend(analytics.efficiency_factor(runs))
    best = {
        effort.label: min(s for _, s in effort.progression)
        for effort in analytics.best_effort_progressions(conn, runs)
    }
    predictions = {p.label: p.seconds for p in metrics.predict_races(runs)}

    # VDOT from the best 5k (or the Riegel-predicted 5k), then the zone table.
    best_5k = best.get("5k") or predictions.get("5k")
    vdot = targets.vdot_from_effort(5000, best_5k) if best_5k else None
    zones = (
        targets.build_training_zones(vdot, hr_max_value, hr_rest)
        if vdot is not None
        else []
    )

    return AthleteProfile(
        run_count=overall.run_count,
        total_km=overall.total_km,
        avg_weekly_km=(
            round(statistics.mean([w.distance_km for w in weekly]), 1)
            if weekly
            else 0.0
        ),
        recent_weekly_km=[w.distance_km for w in weekly[-8:]],
        runs_per_week=consistency.runs_per_week,
        longest_run_km=overall.longest_km,
        longest_layoff_days=consistency.longest_layoff_days,
        typical_pace_s_per_km=round(statistics.median(paces), 1) if paces else None,
        best_efforts=best,
        predicted_races=predictions,
        fitness_ctl=round(pmc[-1].fitness, 1) if pmc else None,
        fatigue_atl=round(pmc[-1].fatigue, 1) if pmc else None,
        form_tsb=round(pmc[-1].form, 1) if pmc else None,
        acwr=acwr[-1][1] if acwr else None,
        efficiency_trend_per_month=round(ef_trend.per_30_days, 4) if ef_trend else None,
        vo2max=_latest(metrics.metric_series(conn, "vo2max")),
        resting_hr=_latest(metrics.metric_series(conn, "resting_hr")),
        hr_max=hr_max_value,
        hr_rest=hr_rest,
        vdot=round(vdot, 1) if vdot is not None else None,
        zones=zones,
        advanced=build_advanced_fitness(conn, runs),
    )
