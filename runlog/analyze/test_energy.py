"""Unit tests for the energy-expenditure (BMR / TDEE) model."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

import pytest

from runlog.analyze import energy
from runlog.analyze.energy import EnergyDay
from runlog.analyze.metrics import Run
from runlog.config import Athlete
from runlog.db import store
from runlog.domain import ActivityId, HealthMetric, Source

_ATHLETE = Athlete(sex="male", height_cm=180.0, birth_date=date(2000, 1, 1))


def _run(
    when: date,
    distance_m: float | None,
    calories: float | None,
    source: Source = "strava",
) -> Run:
    return Run(
        activity_id=ActivityId(1),
        source=source,
        start=datetime(when.year, when.month, when.day, tzinfo=UTC),
        distance_m=distance_m,
        moving_s=1800,
        avg_pace_s_per_km=300.0,
        avg_hr=150.0,
        max_hr=None,
        calories=calories,
    )


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(connection)
    return connection


def _insert_daily(
    conn: sqlite3.Connection, metric: str, series: list[tuple[date, float]]
) -> None:
    store.insert_health_metrics(
        conn,
        [
            HealthMetric(
                metric_type=metric,
                start_time_utc=datetime(day.year, day.month, day.day, tzinfo=UTC),
                value=value,
            )
            for day, value in series
        ],
    )


class TestMifflinBmr:
    def test_male_constant(self) -> None:
        # 10*70 + 6.25*180 - 5*26 + 5 = 1700
        assert energy.mifflin_bmr("male", 70.0, 180.0, 26) == 1700.0

    def test_female_constant(self) -> None:
        # 10*70 + 6.25*180 - 5*26 - 161 = 1534
        assert energy.mifflin_bmr("female", 70.0, 180.0, 26) == 1534.0


class TestWeightOn:
    def test_carry_forward_and_gap(self) -> None:
        body = [(date(2026, 6, 1), 70.0), (date(2026, 6, 10), 72.0)]
        assert (
            energy.weight_on(body, date(2026, 6, 5)),
            energy.weight_on(body, date(2026, 6, 10)),
            energy.weight_on(body, date(2026, 5, 31)),
        ) == (70.0, 72.0, None)


def test_energy_series_uses_carry_forward_weight_and_age(
    conn: sqlite3.Connection,
) -> None:
    _insert_daily(conn, "body_mass", [(date(2026, 6, 1), 70.0)])
    _insert_daily(
        conn, "active_energy", [(date(2026, 6, 2), 500.0), (date(2026, 6, 3), 600.0)]
    )
    assert energy.energy_series(conn, _ATHLETE) == [
        EnergyDay(date(2026, 6, 2), 1700.0, 500.0, 2200.0, 70.0),
        EnergyDay(date(2026, 6, 3), 1700.0, 600.0, 2300.0, 70.0),
    ]


def test_energy_series_prefers_measured_basal(conn: sqlite3.Connection) -> None:
    _insert_daily(conn, "body_mass", [(date(2026, 6, 1), 70.0)])
    _insert_daily(conn, "active_energy", [(date(2026, 6, 2), 500.0)])
    _insert_daily(conn, "basal_energy", [(date(2026, 6, 2), 1600.0)])
    # Measured basal (1600) overrides the Mifflin estimate (1700).
    assert energy.energy_series(conn, _ATHLETE) == [
        EnergyDay(date(2026, 6, 2), 1600.0, 500.0, 2100.0, 70.0),
    ]


def test_energy_series_empty_without_athlete(conn: sqlite3.Connection) -> None:
    _insert_daily(conn, "body_mass", [(date(2026, 6, 1), 70.0)])
    _insert_daily(conn, "active_energy", [(date(2026, 6, 2), 500.0)])
    assert energy.energy_series(conn, None) == []


def test_build_energy_summary_scalars(conn: sqlite3.Connection) -> None:
    _insert_daily(conn, "body_mass", [(date(2026, 6, 1), 70.0)])
    _insert_daily(
        conn,
        "active_energy",
        [
            (date(2026, 6, 2), 500.0),
            (date(2026, 6, 3), 550.0),
            (date(2026, 6, 4), 600.0),
        ],
    )
    summary = energy.build_energy(conn, _ATHLETE, frozenset(), today=date(2026, 6, 4))
    assert summary is not None
    assert (
        summary.bmr_latest,
        summary.active_30d,
        summary.tdee_30d,
        summary.weight_latest,
        summary.method,
    ) == (1700.0, 550.0, 2250.0, 70.0, "mifflin")


def test_build_energy_none_without_athlete(conn: sqlite3.Connection) -> None:
    _insert_daily(conn, "active_energy", [(date(2026, 6, 2), 500.0)])
    assert energy.build_energy(conn, None, frozenset()) is None


def test_energy_cost_series_kcal_per_km_and_skips_nulls() -> None:
    runs = [
        _run(date(2026, 6, 1), 10000.0, 700.0),  # 70.0 kcal/km
        _run(date(2026, 6, 2), 5000.0, None),  # no calories -> skipped
        _run(date(2026, 6, 3), None, 400.0),  # no distance -> skipped
    ]
    assert energy.energy_cost_series(runs) == [(date(2026, 6, 1), 70.0)]


def test_energy_cost_series_filters_to_one_source() -> None:
    # Providers disagree on calories, so a mixed series would show a device
    # step rather than a change in the athlete.
    runs = [
        _run(date(2026, 6, 1), 10000.0, 700.0, source="strava"),
        _run(date(2026, 6, 2), 10000.0, 900.0, source="apple_health"),
    ]
    assert energy.energy_cost_series(runs, source="strava") == [
        (date(2026, 6, 1), 70.0)
    ]


def test_dominant_calorie_source_picks_most_common() -> None:
    runs = [
        _run(date(2026, 6, 1), 10000.0, 700.0, source="strava"),
        _run(date(2026, 6, 2), 10000.0, 700.0, source="strava"),
        _run(date(2026, 6, 3), 10000.0, 900.0, source="apple_health"),
        _run(date(2026, 6, 4), 10000.0, None, source="apple_health"),
    ]
    assert energy.dominant_calorie_source(runs) == "strava"


def test_dominant_calorie_source_none_without_calories() -> None:
    assert (
        energy.dominant_calorie_source([_run(date(2026, 6, 1), 10000.0, None)]) is None
    )
