"""Unit tests for the daily readiness score."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from runlog.analyze import readiness
from runlog.db import store
from runlog.domain import HealthMetric


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(connection)
    return connection


def _insert(
    conn: sqlite3.Connection, metric: str, series: list[tuple[date, float]]
) -> None:
    store.insert_health_metrics(
        conn,
        [
            HealthMetric(
                metric_type=metric,
                start_time_utc=datetime(day.year, day.month, day.day, 8, tzinfo=UTC),
                value=value,
            )
            for day, value in series
        ],
    )


def _wobble(metric_day: date, days: int = 20) -> list[tuple[date, float]]:
    # Alternating 48/52 -> baseline median 50, MAD 2 (robust sigma 1.4826*2).
    return [(metric_day + timedelta(days=i), 48.0 + (i % 2) * 4.0) for i in range(days)]


def _readiness_on(conn: sqlite3.Connection, day: date) -> readiness.ReadinessDay:
    return next(r for r in readiness.readiness_series(conn) if r.day == day)


def test_score_is_midpoint_when_marker_sits_at_baseline(
    conn: sqlite3.Connection,
) -> None:
    start = date(2026, 1, 1)
    at_baseline = start + timedelta(days=20)
    _insert(conn, "resting_hr", [*_wobble(start), (at_baseline, 50.0)])

    assert _readiness_on(conn, at_baseline).score == 50.0


def test_score_drops_when_resting_hr_is_elevated(conn: sqlite3.Connection) -> None:
    start = date(2026, 1, 1)
    bad_day = start + timedelta(days=20)
    _insert(conn, "resting_hr", [*_wobble(start), (bad_day, 56.0)])

    # z = (56-50)/(1.4826*2) = 2.0235, stored as the -2.02 contributor; resting
    # HR up is bad (sign -1), so score = 50 + 15 * (-2.02) = 19.7.
    assert _readiness_on(conn, bad_day).score == 19.7


def test_performance_correlation_none_without_overlap(conn: sqlite3.Connection) -> None:
    # Readiness exists but there are no runs, so no efficiency residuals overlap.
    _insert(conn, "resting_hr", [*_wobble(date(2026, 1, 1)), (date(2026, 1, 21), 50.0)])
    ready = readiness.readiness_series(conn)
    assert readiness.performance_correlation(ready, runs=[]) is None
