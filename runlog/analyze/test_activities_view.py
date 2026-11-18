"""Tests for the Activities-tab view model and per-workout detail builder."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from runlog.analyze import activities_view, metrics
from runlog.db import store
from runlog.domain import (
    Activity,
    ActivityId,
    ActivityRecord,
    Lap,
    SourceId,
    StreamPoint,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(connection)
    return connection


def _store_run(
    conn: sqlite3.Connection,
    source_id: str,
    source: str,
    *,
    is_indoor: bool = False,
    stream: tuple[StreamPoint, ...] = (),
    laps: tuple[Lap, ...] = (),
) -> None:
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source=source,  # type: ignore[arg-type]
                source_id=SourceId(source_id),
                sport_type="Run",
                start_time_utc=datetime(2026, 6, 1, 7, tzinfo=UTC),  # a Monday
                distance_m=5000.0,
                moving_s=1500,
                avg_pace_s_per_km=300.0,
                avg_hr=150.0,
                max_hr=170.0,
                is_indoor=is_indoor,
            ),
            stream=stream,
            laps=laps,
        ),
    )


# Six samples all in Z2 (HR 140 at max 190 / rest 50 = 0.64 reserve); flat, so no
# elevation chart and the detected session is a plain Easy run.
_EASY_STREAM = tuple(
    StreamPoint(
        offset_s=i * 60,
        distance_m=float(i) * 180.0,
        hr=140.0,
        cadence=160.0,
        velocity_mps=3.0,
    )
    for i in range(6)
)


def test_build_activities_row_has_indoor_flag_and_detected_session(
    conn: sqlite3.Connection,
) -> None:
    _store_run(conn, "r:1", "runna", is_indoor=True, stream=_EASY_STREAM)
    runs = metrics.canonical_run_activities(conn)

    rows, _ = activities_view.build_activities(conn, runs, hr_max=190.0, hr_rest=50.0)

    assert rows == [
        activities_view.ActivityRow(
            activity_id=1,
            page="activities/1.html",
            date="2026-06-01",
            weekday="Mon",
            sport="Treadmill",
            indoor=True,
            session="Easy",
            source="runna",
            distance_km=5.0,
            duration_s=1500,
            duration_disp="25:00",
            pace_s=300.0,
            pace_disp="5:00/km",
            avg_hr=150,
            max_hr=170,
            cadence=None,
            elevation_m=None,
            calories=None,
            load=45,
            ef=1.33,
            gap_s=None,
            gap_disp="-",
            decoupling_pct=0.0,
            easy_pct=100.0,
            moderate_pct=0.0,
            hard_pct=0.0,
            is_pr=True,
            is_off=False,
        )
    ]


# Varying altitude and three laps: an outdoor run with an Elevation chart and
# surfaced laps.
_OUTDOOR_STREAM = tuple(
    StreamPoint(
        offset_s=i * 60,
        distance_m=float(i) * 180.0,
        hr=140.0 + i,
        cadence=160.0,
        velocity_mps=3.0,
        altitude_m=float(i),
    )
    for i in range(6)
)
_LAPS = (
    Lap(lap_index=0, elapsed_s=120, distance_m=1000.0, avg_hr=145.0),
    Lap(lap_index=1, elapsed_s=120, distance_m=1000.0, avg_hr=150.0),
    Lap(lap_index=2, elapsed_s=60, distance_m=1000.0, avg_hr=152.0),
)


def test_build_detail_has_charts_laps_and_five_zone_bands(
    conn: sqlite3.Connection,
) -> None:
    _store_run(conn, "s:d", "strava", stream=_OUTDOOR_STREAM, laps=_LAPS)
    run = metrics.canonical_run_activities(conn)[0]

    detail = activities_view.build_detail(conn, run, hr_max=190.0, hr_rest=50.0)

    assert [c.title for c in detail.charts] == [
        "Pace",
        "Heart rate",
        "Efficiency",
        "Cadence",
        "Stride",
        "Elevation",
    ]
    assert [lap.index for lap in detail.laps] == [1, 2, 3]
    assert [z.label for z in detail.zones] == ["Z1", "Z2", "Z3", "Z4", "Z5"]
    assert [label for label, _ in detail.thresholds] == [
        "LT1 (aerobic)",
        "LT2 (lactate threshold)",
    ]


def test_build_detail_falls_back_to_the_apple_twins_stream(
    conn: sqlite3.Connection,
) -> None:
    # Strava is canonical but carries no stream; its linked Apple twin has it.
    _store_run(conn, "111", "strava")
    _store_run(conn, "a:twin", "apple_health", stream=_OUTDOOR_STREAM, laps=_LAPS)
    strava_id = conn.execute(
        "SELECT id FROM activities WHERE source_id = '111'"
    ).fetchone()["id"]
    apple_id = conn.execute(
        "SELECT id FROM activities WHERE source_id = 'a:twin'"
    ).fetchone()["id"]
    store.insert_link(conn, ActivityId(int(strava_id)), ActivityId(int(apple_id)), 1.0)
    run = next(
        r for r in metrics.canonical_run_activities(conn) if r.source == "strava"
    )

    detail = activities_view.build_detail(conn, run, hr_max=190.0, hr_rest=50.0)

    assert [c.title for c in detail.charts] == [
        "Pace",
        "Heart rate",
        "Efficiency",
        "Cadence",
        "Stride",
        "Elevation",
    ]
    assert [lap.index for lap in detail.laps] == [1, 2, 3]


def _pt(offset: int, **ch: float | None) -> activities_view._StreamPt:
    return activities_view._StreamPt(
        offset_s=offset,
        distance_m=ch.get("distance_m"),
        hr=ch.get("hr"),
        velocity_mps=ch.get("velocity_mps"),
        cadence=ch.get("cadence"),
        altitude_m=ch.get("altitude_m"),
        watts=ch.get("watts"),
    )


def test_resample_medians_per_bucket_and_caps_count() -> None:
    stream = [
        _pt(i, hr=h)
        for i, h in enumerate([100.0, 100.0, 100.0, 200.0, 150.0, 150.0, 150.0, 150.0])
    ]
    buckets = activities_view._resample(stream, target=2)

    assert [b.hr for b in buckets] == [100.0, 150.0] and len(buckets) == 2


def test_smooth_is_a_centered_median_that_preserves_gaps() -> None:
    values: list[float | None] = [170.0, 162.0, 178.0, None, 170.0, 162.0, 178.0]
    assert activities_view._smooth(values, window=3) == [
        166.0,
        170.0,
        170.0,
        None,
        166.0,
        170.0,
        170.0,
    ]


def test_charts_derive_pace_cadence_efficiency_stride() -> None:
    # Dense 1 Hz steady 3 m/s run; per-leg cadence 88 should read as 176 spm, pace
    # should come from distance (~5:33/km), no watts/altitude -> no Power/Elevation.
    stream = [
        _pt(i, distance_m=3.0 * i, hr=150.0, velocity_mps=3.0, cadence=88.0)
        for i in range(120)
    ]
    charts = {
        c.title: c
        for c in activities_view._charts(activities_view._resample(stream, target=20))
    }

    cadence = [y for _, y in charts["Cadence"].points if y is not None]
    pace = [y for _, y in charts["Pace"].points if y is not None]
    assert (
        set(charts) == {"Pace", "Heart rate", "Efficiency", "Cadence", "Stride"}
        and cadence == [176.0] * len(cadence)
        and all(abs(p - 1000 / 3) < 1.0 for p in pace)
    )


@pytest.mark.parametrize(
    ("distance_km", "zones", "is_intervals", "expected"),
    [
        (16.0, [0.0, 0.0, 0.0, 300.0, 0.0], False, "Threshold"),  # hard long != Long
        (16.0, [0.0, 600.0, 0.0, 0.0, 0.0], False, "Long"),  # easy long run
        (5.0, [0.0, 200.0, 0.0, 30.0, 0.0], True, "Intervals"),  # recovery -> intervals
        (5.0, [0.0, 0.0, 0.0, 0.0, 100.0], False, "VO2max"),
        (0.0, [0.0, 0.0, 0.0, 0.0, 0.0], False, "Run"),  # no HR time
    ],
)
def test_detect_session_puts_intensity_before_the_long_rule(
    distance_km: float,
    zones: list[float],
    is_intervals: bool,
    expected: str,
) -> None:
    assert activities_view._detect_session(distance_km, zones, is_intervals) == expected
