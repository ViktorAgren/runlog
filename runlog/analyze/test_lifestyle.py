"""Unit tests for passive daily-life patterns."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

import pytest

from runlog.analyze import lifestyle
from runlog.analyze.lifestyle import DayContrast, LifestyleSummary
from runlog.db import store
from runlog.domain import HealthMetric


def test_weekday_means_buckets_mon_to_sun() -> None:
    # 2026-06-01 is a Monday. Two Mondays average; Sunday has no data.
    daily = [
        (date(2026, 6, 1), 8000.0),
        (date(2026, 6, 8), 10000.0),
        (date(2026, 6, 2), 6000.0),
    ]
    assert lifestyle.weekday_means(daily) == (
        9000.0,
        6000.0,
        None,
        None,
        None,
        None,
        None,
    )


def test_training_rest_contrast_splits_by_day_set() -> None:
    daily = [
        (date(2026, 6, 1), 10000.0),
        (date(2026, 6, 2), 6000.0),
        (date(2026, 6, 3), 12000.0),
        (date(2026, 6, 4), 8000.0),
    ]
    training_days = frozenset({date(2026, 6, 1), date(2026, 6, 3)})
    assert lifestyle.training_rest_contrast(daily, training_days) == DayContrast(
        training_mean=11000.0, rest_mean=7000.0, training_n=2, rest_n=2
    )


def test_training_rest_contrast_none_when_one_side_empty() -> None:
    daily = [(date(2026, 6, 1), 10000.0)]
    assert (
        lifestyle.training_rest_contrast(daily, frozenset({date(2026, 6, 1)})) is None
    )


def test_trailing_mean_and_pstdev_respect_window() -> None:
    today = date(2026, 6, 30)
    daily = [
        (date(2026, 1, 1), 100.0),  # far outside the 30-day window
        (date(2026, 6, 20), 7.0),
        (date(2026, 6, 21), 9.0),
    ]
    assert (
        lifestyle.trailing_mean(daily, today=today),
        lifestyle.trailing_pstdev(daily, today=today),
    ) == (8.0, 1.0)


def test_weekend_sleep_shift_sign(conn: sqlite3.Connection) -> None:
    # Weekday nights 7 h (Mon Jun 1), weekend nights 9 h (Sat Jun 6): +2 h.
    _insert_daily(
        conn, "sleep_hours", [(date(2026, 6, 1), 7.0), (date(2026, 6, 6), 9.0)]
    )
    summary = lifestyle.build_lifestyle(conn, frozenset(), today=date(2026, 6, 30))
    assert summary.weekend_sleep_shift_h == 2.0


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


def test_build_lifestyle_from_db(conn: sqlite3.Connection) -> None:
    today = date(2026, 6, 10)
    _insert_daily(
        conn,
        "steps",
        [(date(2026, 6, 1), 10000.0), (date(2026, 6, 2), 6000.0)],
    )
    _insert_daily(
        conn,
        "sleep_hours",
        [(date(2026, 6, 1), 7.0), (date(2026, 6, 2), 9.0)],
    )

    summary = lifestyle.build_lifestyle(
        conn, frozenset({date(2026, 6, 1)}), today=today
    )
    assert summary == LifestyleSummary(
        steps_30d=8000.0,
        sleep_30d=8.0,
        sleep_sd_30d=1.0,
        weekend_sleep_shift_h=None,  # both nights are weekdays
        steps_contrast=DayContrast(
            training_mean=10000.0, rest_mean=6000.0, training_n=1, rest_n=1
        ),
    )
