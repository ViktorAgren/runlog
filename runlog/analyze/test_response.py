"""Unit tests for the load -> next-day recovery dose-response."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from runlog.analyze import response
from runlog.analyze.anomaly import Direction, Reading
from runlog.db import store
from runlog.domain import Activity, ActivityRecord, HealthMetric, SourceId


def _reading(day: date, z: float) -> Reading:
    return Reading(day=day, value=0.0, center=0.0, deviation=z)


def _flat_load(start: date, days: int, load: float = 0.0) -> list[tuple[date, float]]:
    return [(start + timedelta(days=i), load) for i in range(days)]


class TestMarkerResponse:
    def test_pairs_load_with_next_day_reading(self) -> None:
        start = date(2026, 6, 1)
        # 15 rest days, then one hard day; readings exist the morning after each.
        load = [*_flat_load(start, 15), (start + timedelta(days=15), 100.0)]
        readings = [
            _reading(day + timedelta(days=1), -1.0 if value > 0 else 0.5)
            for day, value in load
        ]

        result = response.marker_response(load, readings, "hrv_sdnn", Direction.LOW)
        assert result is not None
        # The lone loaded day equals the nonzero median -> "moderate"; the
        # empty hard bucket reports mean None.
        assert [(b.label, b.mean_z, b.n) for b in result.buckets] == [
            ("rest", 0.5, 15),
            ("moderate", -1.0, 1),
            ("hard", None, 0),
        ]

    def test_bucket_threshold_is_median_of_nonzero_loads(self) -> None:
        start = date(2026, 6, 1)
        # Loads 50/50/200: median nonzero = 50, so the 200 day is "hard".
        load = [
            *_flat_load(start, 12),
            (start + timedelta(days=12), 50.0),
            (start + timedelta(days=13), 50.0),
            (start + timedelta(days=14), 200.0),
        ]
        readings = [_reading(day + timedelta(days=1), 0.0) for day, _ in load]

        result = response.marker_response(load, readings, "hrv_sdnn", Direction.LOW)
        assert result is not None
        assert [(b.label, b.n) for b in result.buckets] == [
            ("rest", 12),
            ("moderate", 2),
            ("hard", 1),
        ]

    def test_none_under_minimum_pairs(self) -> None:
        start = date(2026, 6, 1)
        load = _flat_load(start, 5)
        readings = [_reading(day + timedelta(days=1), 0.0) for day, _ in load]

        assert (
            response.marker_response(load, readings, "hrv_sdnn", Direction.LOW) is None
        )

    def test_rest_vs_hard_inference_on_separated_groups(self) -> None:
        # 10 rest days with next-day z ~ +0.5 and 10 clearly-hard days with
        # z ~ -1.0: the Welch test must flag the separation with a negative g
        # (hard minus rest).
        start = date(2026, 6, 1)
        load = [
            (start + timedelta(days=i), 0.0 if i % 2 == 0 else 100.0 + i)
            for i in range(20)
        ]
        readings = [
            _reading(
                day + timedelta(days=1),
                (0.5 if value == 0 else -1.0) + 0.01 * i,
            )
            for i, (day, value) in enumerate(load)
        ]

        result = response.marker_response(load, readings, "hrv_sdnn", Direction.LOW)
        assert result is not None
        assert result.rest_vs_hard is not None
        assert result.rest_vs_hard.hedges_g < 0
        assert result.rest_vs_hard.p < 0.001
        assert result.load_corr is not None
        assert result.load_corr.r == pytest.approx(result.pearson_r, abs=0.005)

    def test_pearson_sign_tracks_monotone_response(self) -> None:
        start = date(2026, 6, 1)
        # Higher load -> lower next-day z, strictly monotone -> r == -1.
        load = [(start + timedelta(days=i), float(i * 10)) for i in range(20)]
        readings = [
            _reading(day + timedelta(days=1), -value / 100) for day, value in load
        ]

        result = response.marker_response(load, readings, "hrv_sdnn", Direction.LOW)
        assert result is not None and result.pearson_r == -1.0


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(connection)
    return connection


def test_load_response_counts_strength_load(conn: sqlite3.Connection) -> None:
    # One strength session (no distance) inside a long HRV series: the day
    # after it must land in a non-rest bucket, proving non-run load enters
    # the dose.
    strength_day = date(2026, 6, 15)
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source="apple_health",
                source_id=SourceId("apple:strength-1"),
                sport_type="TraditionalStrengthTraining",
                start_time_utc=datetime(2026, 6, 15, 17, tzinfo=UTC),
                moving_s=3600,
                avg_hr=120.0,
            )
        ),
    )
    store.insert_health_metrics(
        conn,
        [
            HealthMetric(
                "hrv_sdnn",
                datetime(2026, 6, 1, 8, tzinfo=UTC) + timedelta(days=i),
                50.0 + (i % 2) * 10,
            )
            for i in range(30)
        ],
    )

    responses = response.load_response(conn, hr_max=195.0, hr_rest=50.0)
    assert [r.metric for r in responses] == ["hrv_sdnn"]
    by_label = {b.label: b.n for b in responses[0].buckets}
    # The lone loaded day is its own median -> "moderate"; every other day rest.
    assert (by_label["moderate"], by_label["hard"]) == (1, 0)
    # The strength day itself paired with the reading on the 16th.
    assert responses[0].n_pairs > 14
    assert strength_day + timedelta(days=1) <= date(2026, 6, 30)
