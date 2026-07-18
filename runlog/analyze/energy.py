"""Energy expenditure: resting (BMR), active, and total daily (TDEE).

Base expenditure is not measured by Apple Health today, so BMR is estimated
with the Mifflin-St Jeor equation from the stored ``body_mass`` series plus the
athlete's demographics; a measured ``basal_energy`` reading is preferred when
present. Apple's ``active_energy`` already excludes resting energy, so the daily
total is additive: ``TDEE = BMR + active``.

Pure and read-only. Mirrors :mod:`runlog.analyze.lifestyle` (daily series,
trailing means, training/rest contrast) and reuses its helpers.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import TYPE_CHECKING

from runlog.analyze import lifestyle, metrics, stats

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import date

    from runlog.analyze.metrics import Run
    from runlog.config import Athlete, Sex


@dataclass(frozen=True)
class EnergyDay:
    """One day's expenditure breakdown, in kcal."""

    day: date
    bmr: float
    active: float
    tdee: float
    weight_kg: float | None


@dataclass(frozen=True)
class EnergySummary:
    bmr_latest: float
    active_30d: float | None
    tdee_30d: float | None
    weight_latest: float | None
    active_trend: stats.TrendTest | None
    tdee_trend: stats.TrendTest | None
    weight_trend: stats.TrendTest | None
    tdee_contrast: lifestyle.DayContrast | None  # training days vs rest days
    method: str  # "mifflin" | "measured-basal"


def mifflin_bmr(sex: Sex, weight_kg: float, height_cm: float, age_years: int) -> float:
    """Resting metabolic rate (kcal/day) via Mifflin-St Jeor."""
    base = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age_years
    return base + (5.0 if sex == "male" else -161.0)


def weight_on(body_mass_daily: Sequence[tuple[date, float]], day: date) -> float | None:
    """Most recent body mass on or before ``day`` (carry-forward), else None.

    ``body_mass_daily`` must be ascending by date (as produced by
    :func:`runlog.analyze.metrics.daily_means`).
    """
    days = [d for d, _ in body_mass_daily]
    idx = bisect.bisect_right(days, day)
    return body_mass_daily[idx - 1][1] if idx > 0 else None


def energy_series(
    conn: sqlite3.Connection, athlete: Athlete | None, since: date | None = None
) -> list[EnergyDay]:
    """Per-day BMR/active/TDEE breakdown, one entry per day with active energy.

    Days without an estimable BMR (no measured basal and no known body mass) are
    skipped. Returns ``[]`` when no athlete demographics are configured.
    """
    if athlete is None:
        return []
    active = metrics.daily_means(metrics.metric_series(conn, "active_energy", since))
    if not active:
        return []
    # Weight changes slowly; carry forward the latest reading regardless of the
    # window start, so a stale-but-known weight still grounds the estimate.
    body = metrics.daily_means(metrics.metric_series(conn, "body_mass"))
    basal = dict(
        metrics.daily_means(metrics.metric_series(conn, "basal_energy", since))
    )
    days: list[EnergyDay] = []
    for day, active_kcal in active:
        weight = weight_on(body, day)
        measured = basal.get(day)
        if measured is not None:
            bmr = measured
        elif weight is not None:
            bmr = mifflin_bmr(
                athlete.sex, weight, athlete.height_cm, athlete.age_on(day)
            )
        else:
            continue
        days.append(
            EnergyDay(
                day=day,
                bmr=round(bmr, 1),
                active=round(active_kcal, 1),
                tdee=round(bmr + active_kcal, 1),
                weight_kg=round(weight, 1) if weight is not None else None,
            )
        )
    return days


def energy_cost_series(runs: Sequence[Run]) -> list[tuple[date, float]]:
    """Per-run energy cost (kcal per km), dropping runs without calories."""
    series: list[tuple[date, float]] = []
    for run in runs:
        if run.calories is None or not run.distance_m:
            continue
        series.append(
            (run.start.date(), round(run.calories / (run.distance_m / 1000), 1))
        )
    return series


def build_energy(
    conn: sqlite3.Connection,
    athlete: Athlete | None,
    training_days: frozenset[date],
    since: date | None = None,
    today: date | None = None,
) -> EnergySummary | None:
    """Assemble the expenditure summary, or None when it can't be estimated."""
    days = energy_series(conn, athlete, since=since)
    if not days:
        return None
    active_daily = [(d.day, d.active) for d in days]
    tdee_daily = [(d.day, d.tdee) for d in days]
    weight_daily = [(d.day, d.weight_kg) for d in days if d.weight_kg is not None]
    basal_present = bool(metrics.metric_series(conn, "basal_energy", since))
    return EnergySummary(
        bmr_latest=days[-1].bmr,
        active_30d=lifestyle.trailing_mean(active_daily, today=today),
        tdee_30d=lifestyle.trailing_mean(tdee_daily, today=today),
        weight_latest=weight_daily[-1][1] if weight_daily else None,
        active_trend=stats.trend_test(active_daily),
        tdee_trend=stats.trend_test(tdee_daily),
        weight_trend=stats.trend_test(weight_daily) if weight_daily else None,
        tdee_contrast=lifestyle.training_rest_contrast(tdee_daily, training_days),
        method="measured-basal" if basal_present else "mifflin",
    )
