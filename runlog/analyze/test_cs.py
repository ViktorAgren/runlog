"""Unit tests for the Critical Speed model."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from runlog.analyze import cs, metrics
from runlog.db import store
from runlog.domain import Activity, ActivityRecord, SourceId, StreamPoint

# A known athlete: CS = 5 m/s, D' = 200 m. Then t = (d - 200) / 5 for each d.
_CS = 5.0
_D_PRIME = 200.0


def _effort(distance_m: float) -> cs.CsPoint:
    return cs.CsPoint(distance_m=distance_m, seconds=(distance_m - _D_PRIME) / _CS)


class TestFitCs:
    def test_recovers_known_cs_and_d_prime(self) -> None:
        points = [_effort(d) for d in (400.0, 1000.0, 2000.0, 5000.0)]
        model = cs.fit_cs(points)
        assert model is not None
        assert (model.cs_mps, model.d_prime_m, model.r) == (5.0, 200.0, 1.0)

    def test_predict_seconds_inverts_the_line(self) -> None:
        model = cs.fit_cs([_effort(d) for d in (400.0, 1000.0, 5000.0)])
        assert model is not None
        # t(3000 m) = (3000 - 200) / 5 = 560 s.
        assert model.predict_seconds(3000.0) == 560.0

    def test_predict_none_inside_anaerobic_reserve(self) -> None:
        model = cs.fit_cs([_effort(d) for d in (400.0, 1000.0, 5000.0)])
        assert model is not None
        assert model.predict_seconds(_D_PRIME - 1) is None

    def test_none_with_too_few_points(self) -> None:
        assert cs.fit_cs([_effort(1000.0)]) is None

    def test_none_when_slope_non_positive(self) -> None:
        # Longer distances taking *less* time -> negative slope -> no model.
        points = [
            cs.CsPoint(distance_m=1000.0, seconds=300.0),
            cs.CsPoint(distance_m=2000.0, seconds=200.0),
        ]
        assert cs.fit_cs(points) is None


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(connection)
    return connection


def _add_run_with_stream(
    conn: sqlite3.Connection, when: datetime, speed_mps: float
) -> None:
    # Constant-velocity 6 km run: cumulative distance = speed * offset each second.
    seconds = int(6000 / speed_mps)
    stream = tuple(
        StreamPoint(offset_s=i, distance_m=speed_mps * i, velocity_mps=speed_mps)
        for i in range(seconds + 1)
    )
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source="strava",
                source_id=SourceId(f"strava:{when.isoformat()}"),
                sport_type="Run",
                start_time_utc=when,
                distance_m=6000.0,
                moving_s=seconds,
                avg_pace_s_per_km=1000 / speed_mps,
            ),
            stream=stream,
        ),
    )


def test_critical_speed_fits_from_constant_velocity_stream(
    conn: sqlite3.Connection,
) -> None:
    # A single 4 m/s run: every distance is covered at 4 m/s, so the best-effort
    # points are collinear through the origin -> CS = 4 m/s, D' = 0 m.
    _add_run_with_stream(conn, datetime(2026, 6, 1, 7, tzinfo=UTC), speed_mps=4.0)
    runs = metrics.canonical_run_activities(conn)
    model = cs.critical_speed(conn, runs)
    assert model is not None
    assert (model.cs_mps, model.d_prime_m) == (4.0, 0.0)
