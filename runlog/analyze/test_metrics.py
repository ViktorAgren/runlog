"""Unit tests for the analysis metrics."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

import pytest

from runlog.analyze import metrics
from runlog.db import store
from runlog.domain import (
    Activity,
    ActivityId,
    ActivityRecord,
    HealthMetric,
    Source,
    SourceId,
    StreamPoint,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(connection)
    return connection


def _add_run(
    conn: sqlite3.Connection,
    when: datetime,
    *,
    source: Source = "strava",
    distance_m: float | None = 5000.0,
    pace: float | None = 300.0,
    avg_hr: float | None = 150.0,
    hr_stream: list[float] | None = None,
) -> ActivityId:
    stream = tuple(
        StreamPoint(offset_s=i, hr=hr) for i, hr in enumerate(hr_stream or [])
    )
    return store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source=source,
                source_id=SourceId(f"{source}:{when.isoformat()}"),
                sport_type="Run" if source == "strava" else "Running",
                start_time_utc=when,
                distance_m=distance_m,
                moving_s=1500,
                avg_pace_s_per_km=pace,
                avg_hr=avg_hr,
            ),
            stream=stream,
        ),
    )


def test_canonical_drops_linked_apple_twin(conn: sqlite3.Connection) -> None:
    strava = _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC), source="strava")
    apple = _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC), source="apple_health")
    store.insert_link(conn, strava, apple, 1.0)

    canonical = metrics.canonical_run_activities(conn)
    assert [r.source for r in canonical] == ["strava"]


def test_canonical_keeps_unlinked_from_both_sources(conn: sqlite3.Connection) -> None:
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC), source="strava")
    _add_run(conn, datetime(2026, 6, 2, 7, tzinfo=UTC), source="apple_health")

    assert len(metrics.canonical_run_activities(conn)) == 2


def test_weekly_volume_gap_fills_and_rolls(conn: sqlite3.Connection) -> None:
    # Week A: 5km. Skip a week (gap). Week C: 10km. Rolling mean spans 4 weeks.
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC), distance_m=5000.0)
    _add_run(conn, datetime(2026, 6, 15, 7, tzinfo=UTC), distance_m=10000.0)
    runs = metrics.canonical_run_activities(conn)

    weekly = metrics.weekly_volume(runs)
    assert [(w.week_start, w.distance_km, w.rolling_km) for w in weekly] == [
        (date(2026, 6, 1), 5.0, 5.0),
        (date(2026, 6, 8), 0.0, 2.5),
        (date(2026, 6, 15), 10.0, 5.0),
    ]


def test_active_week_streak_counts_trailing_run(conn: sqlite3.Connection) -> None:
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC))
    _add_run(conn, datetime(2026, 6, 15, 7, tzinfo=UTC))
    _add_run(conn, datetime(2026, 6, 22, 7, tzinfo=UTC))
    weekly = metrics.weekly_volume(metrics.canonical_run_activities(conn))

    # Weeks: active, gap, active, active -> current streak 2, longest 2.
    assert metrics.active_week_streak(weekly) == (2, 2)


def test_fastest_by_bucket_picks_minimum_pace(conn: sqlite3.Connection) -> None:
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC), distance_m=4000.0, pace=320.0)
    _add_run(conn, datetime(2026, 6, 2, 7, tzinfo=UTC), distance_m=4500.0, pace=290.0)
    _add_run(conn, datetime(2026, 6, 3, 7, tzinfo=UTC), distance_m=8000.0, pace=310.0)
    runs = metrics.canonical_run_activities(conn)

    result = {
        b.label: (b.fastest_pace_s_per_km, b.count)
        for b in metrics.fastest_by_bucket(runs)
    }
    assert result == {
        "<3k": (None, 0),
        "3-5k": (290.0, 2),
        "5-10k": (310.0, 1),
        "10k+": (None, 0),
    }


def test_hr_drift_splits_run_in_halves(conn: sqlite3.Connection) -> None:
    _add_run(
        conn,
        datetime(2026, 6, 1, 7, tzinfo=UTC),
        distance_m=12000.0,
        hr_stream=[140.0, 142.0, 158.0, 160.0],
    )
    runs = metrics.canonical_run_activities(conn)

    drift = metrics.hr_drift(conn, runs, top_n=1)
    assert [(d.first_half_hr, d.second_half_hr) for d in drift] == [(141.0, 159.0)]


def test_canonical_drops_tiny_junk_activities(conn: sqlite3.Connection) -> None:
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC), distance_m=5000.0)
    _add_run(conn, datetime(2026, 6, 2, 7, tzinfo=UTC), distance_m=50.0)  # 50 m junk

    runs = metrics.canonical_run_activities(conn)
    assert [r.distance_m for r in runs] == [5000.0]


def test_canonical_since_filters_by_date(conn: sqlite3.Connection) -> None:
    _add_run(conn, datetime(2025, 12, 1, 7, tzinfo=UTC))
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC))

    runs = metrics.canonical_run_activities(conn, since=date(2026, 1, 1))
    assert [r.start.date() for r in runs] == [date(2026, 6, 1)]


def test_implausible_pace_excluded_from_pace_metrics(conn: sqlite3.Connection) -> None:
    # A real 5k run and a GPS-glitch run reporting 1 min/km.
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC), distance_m=5000.0, pace=300.0)
    _add_run(conn, datetime(2026, 6, 2, 7, tzinfo=UTC), distance_m=5000.0, pace=60.0)
    runs = metrics.canonical_run_activities(conn)

    assert [p.pace_s_per_km for p in metrics.pace_points(runs)] == [300.0]


def test_metric_series_drops_out_of_range_values(conn: sqlite3.Connection) -> None:
    store.insert_health_metrics(
        conn,
        [
            HealthMetric("hrv_sdnn", datetime(2026, 6, 1, tzinfo=UTC), 65.0),
            HealthMetric(
                "hrv_sdnn", datetime(2026, 6, 2, tzinfo=UTC), 342.0
            ),  # artifact
        ],
    )
    assert [v for _, v in metrics.metric_series(conn, "hrv_sdnn")] == [65.0]


def test_metric_series_is_time_ordered(conn: sqlite3.Connection) -> None:
    store.insert_health_metrics(
        conn,
        [
            HealthMetric("vo2max", datetime(2026, 6, 2, tzinfo=UTC), 52.0),
            HealthMetric("vo2max", datetime(2026, 6, 1, tzinfo=UTC), 50.0),
        ],
    )
    series = metrics.metric_series(conn, "vo2max")
    assert [v for _, v in series] == [50.0, 52.0]


def test_daily_means_averages_within_day() -> None:
    series = [
        (datetime(2026, 6, 1, 8, tzinfo=UTC), 60.0),
        (datetime(2026, 6, 1, 20, tzinfo=UTC), 70.0),
        (datetime(2026, 6, 2, 8, tzinfo=UTC), 50.0),
    ]
    assert metrics.daily_means(series) == [
        (date(2026, 6, 1), 65.0),
        (date(2026, 6, 2), 50.0),
    ]


def test_best_efforts_records_pace_and_date(conn: sqlite3.Connection) -> None:
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC), distance_m=5000.0, pace=310.0)
    _add_run(conn, datetime(2026, 6, 5, 7, tzinfo=UTC), distance_m=5000.0, pace=290.0)
    runs = metrics.canonical_run_activities(conn)

    efforts = {e.label: (e.pace_s_per_km, e.when) for e in metrics.best_efforts(runs)}
    assert efforts["5-10k"] == (290.0, date(2026, 6, 5))


def test_predict_races_uses_riegel_from_fastest(conn: sqlite3.Connection) -> None:
    # 5 km in 1500 s (5:00/km). Predicted 10 km = 1500 * 2**1.06.
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC), distance_m=5000.0, pace=300.0)
    runs = metrics.canonical_run_activities(conn)

    predictions = {p.label: p.seconds for p in metrics.predict_races(runs)}
    assert predictions["5k"] == pytest.approx(1500.0, rel=1e-6)
    assert predictions["10k"] == pytest.approx(1500.0 * 2**1.06, rel=1e-6)


def test_run_gap_days_between_run_days(conn: sqlite3.Connection) -> None:
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC))
    _add_run(conn, datetime(2026, 6, 4, 7, tzinfo=UTC))  # +3 days
    _add_run(conn, datetime(2026, 6, 5, 7, tzinfo=UTC))  # +1 day
    runs = metrics.canonical_run_activities(conn)

    assert metrics.run_gap_days(runs) == [3, 1]


def test_consistency_summary(conn: sqlite3.Connection) -> None:
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC))
    _add_run(conn, datetime(2026, 6, 8, 7, tzinfo=UTC))  # 7-day layoff
    runs = metrics.canonical_run_activities(conn)

    result = metrics.consistency_summary(runs)
    assert (result.active_days, result.span_days, result.longest_layoff_days) == (
        2,
        8,
        7,
    )


def test_hr_zone_seconds_buckets_by_fraction() -> None:
    # hr_max 200: 100 -> Z1 (0.50), 150 -> Z3 (0.75), 190 -> Z5 (0.95), 80 -> below Z1.
    zones = metrics.hr_zone_seconds([100.0, 150.0, 190.0, 80.0], hr_max=200.0)
    seconds = {z.label: z.seconds for z in zones}
    assert seconds == {"Z1": 1, "Z2": 0, "Z3": 1, "Z4": 0, "Z5": 1}
    # Z1 spans 50-60% of 200 bpm; Z5 is open-ended.
    assert (zones[0].low_bpm, zones[0].high_bpm, zones[-1].high_bpm) == (100, 120, None)


def test_weekly_training_load_weights_by_intensity(conn: sqlite3.Connection) -> None:
    # 30 min (1800s moving) at avg_hr 180 with hr_max 180 -> load = 30 * 1.0 = 30.
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC), avg_hr=180.0)
    runs = metrics.canonical_run_activities(conn)

    load = metrics.weekly_training_load(runs, hr_max=180.0)
    assert [round(w.load) for w in load] == [25]  # moving_s fixture is 1500 -> 25 min


def test_cumulative_distance_by_year_accumulates(conn: sqlite3.Connection) -> None:
    _add_run(conn, datetime(2026, 1, 2, 7, tzinfo=UTC), distance_m=5000.0)
    _add_run(conn, datetime(2026, 1, 5, 7, tzinfo=UTC), distance_m=3000.0)
    runs = metrics.canonical_run_activities(conn)

    assert metrics.cumulative_distance_by_year(runs) == {2026: [(2, 5.0), (5, 8.0)]}


def test_local_hour_uses_timezone(conn: sqlite3.Connection) -> None:
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source="strava",
                source_id=SourceId("tz-run"),
                sport_type="Run",
                start_time_utc=datetime(2026, 6, 1, 5, 30, tzinfo=UTC),
                tz="Europe/Stockholm",  # +2h in summer -> 07:30 local
                distance_m=5000.0,
                moving_s=1500,
                avg_pace_s_per_km=300.0,
            )
        ),
    )
    runs = metrics.canonical_run_activities(conn)
    assert metrics.start_hour_distribution(runs) == [7]


def test_overall_summary_totals(conn: sqlite3.Connection) -> None:
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC), distance_m=5000.0)
    _add_run(conn, datetime(2026, 6, 20, 7, tzinfo=UTC), distance_m=12000.0)
    runs = metrics.canonical_run_activities(conn)

    summary = metrics.overall_summary(runs, today=date(2026, 6, 22))
    assert (
        summary.run_count,
        summary.total_km,
        summary.longest_km,
        summary.this_month_km,
    ) == (
        2,
        17.0,
        12.0,
        17.0,
    )
