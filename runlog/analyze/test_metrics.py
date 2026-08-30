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
    # Moving time follows distance at ``pace`` so fixtures stay physiologically
    # plausible (a fixed 1500 s made a 12 km run read as 2:05/km and get flagged).
    moving_s = int(distance_m / 1000 * pace) if distance_m and pace else 1500
    return store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source=source,
                source_id=SourceId(f"{source}:{when.isoformat()}"),
                sport_type="Run" if source == "strava" else "Running",
                start_time_utc=when,
                distance_m=distance_m,
                moving_s=moving_s,
                avg_pace_s_per_km=pace,
                avg_hr=avg_hr,
            ),
            stream=stream,
        ),
    )


def test_canonical_carries_sport_type(conn: sqlite3.Connection) -> None:
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC), source="strava")
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source="apple_health",
                source_id=SourceId("apple:strength-1"),
                sport_type="TraditionalStrengthTraining",
                start_time_utc=datetime(2026, 6, 2, 17, tzinfo=UTC),
                moving_s=3600,
                avg_hr=110.0,
            )
        ),
    )

    activities = metrics.canonical_run_activities(
        conn, metrics.ALL_SPORT_TYPES, min_distance_km=0.0
    )
    assert [(a.sport_type, a.distance_m) for a in activities] == [
        ("Run", 5000.0),
        ("TraditionalStrengthTraining", None),
    ]


def test_canonical_surfaces_calories(conn: sqlite3.Connection) -> None:
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source="strava",
                source_id=SourceId("strava:cal-1"),
                sport_type="Run",
                start_time_utc=datetime(2026, 6, 1, 7, tzinfo=UTC),
                distance_m=5000.0,
                moving_s=1500,
                avg_pace_s_per_km=300.0,
                calories=420.0,
            )
        ),
    )
    assert [r.calories for r in metrics.canonical_run_activities(conn)] == [420.0]


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


def test_canonical_quarantines_corrupted_runs(conn: sqlite3.Connection) -> None:
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC), source="strava")
    # 5 km in 60 s -> implausible pace, flagged at ingest and excluded here.
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source="strava",
                source_id=SourceId("s:bad"),
                sport_type="Run",
                start_time_utc=datetime(2026, 6, 2, 7, tzinfo=UTC),
                distance_m=5000.0,
                moving_s=60,
                avg_pace_s_per_km=12.0,
            ),
        ),
    )

    runs = metrics.canonical_run_activities(conn)
    assert (len(runs), metrics.quarantined_count(conn)) == (1, 1)


def test_canonical_inherits_dynamics_from_dropped_apple_twin(
    conn: sqlite3.Connection,
) -> None:
    # Strava keeps the pair but lacks running dynamics; the Apple twin carries
    # them and is dropped, so the canonical run must inherit its dynamics.
    when = datetime(2026, 6, 1, 7, tzinfo=UTC)
    strava = _add_run(conn, when, source="strava")
    apple = store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source="apple_health",
                source_id=SourceId("apple:dyn"),
                sport_type="Running",
                start_time_utc=when,
                distance_m=5000.0,
                moving_s=1500,
                avg_stride_length_m=1.15,
                avg_power_w=245.0,
                avg_ground_contact_ms=250.0,
            ),
        ),
    )
    store.insert_link(conn, strava, apple, 1.0)

    run = metrics.canonical_run_activities(conn)[0]
    assert (run.source, run.avg_stride_length_m, run.avg_power_w) == (
        "strava",
        1.15,
        245.0,
    )


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


def test_resting_hr_median_from_daily_means(conn: sqlite3.Connection) -> None:
    store.insert_health_metrics(
        conn,
        [
            HealthMetric("resting_hr", datetime(2026, 6, 1, tzinfo=UTC), 50.0),
            HealthMetric("resting_hr", datetime(2026, 6, 2, tzinfo=UTC), 54.0),
            HealthMetric("resting_hr", datetime(2026, 6, 3, tzinfo=UTC), 58.0),
        ],
    )
    assert metrics.resting_hr_median(conn) == 54.0


def test_resting_hr_median_defaults_without_data(conn: sqlite3.Connection) -> None:
    assert metrics.resting_hr_median(conn) == metrics.DEFAULT_HR_REST


def _activity(
    when: datetime, sport_type: str, moving_s: int | None = 3600
) -> metrics.Run:
    return metrics.Run(
        activity_id=ActivityId(1),
        source="apple_health",
        start=when,
        distance_m=None,
        moving_s=moving_s,
        avg_pace_s_per_km=None,
        avg_hr=110.0,
        max_hr=None,
        sport_type=sport_type,
    )


class TestWeeklySportHours:
    def test_stacks_and_gap_fills(self) -> None:
        week_a = datetime(2026, 6, 1, 7, tzinfo=UTC)  # a Monday
        week_c = datetime(2026, 6, 15, 7, tzinfo=UTC)
        activities = [
            _activity(week_a, "Run", moving_s=1800),
            _activity(week_a, "TraditionalStrengthTraining", moving_s=3600),
            _activity(week_c, "Walking", moving_s=7200),
        ]
        assert [
            (w.week_start, w.hours_by_sport)
            for w in metrics.weekly_sport_hours(activities)
        ] == [
            (date(2026, 6, 1), {"Run": 0.5, "Strength": 1.0}),
            (date(2026, 6, 8), {}),
            (date(2026, 6, 15), {"Walk": 2.0}),
        ]

    def test_collapses_run_and_running_labels(self) -> None:
        when = datetime(2026, 6, 1, 7, tzinfo=UTC)
        activities = [
            _activity(when, "Run", moving_s=1800),
            _activity(when, "Running", moving_s=1800),
        ]
        assert [w.hours_by_sport for w in metrics.weekly_sport_hours(activities)] == [
            {"Run": 1.0}
        ]


class TestSportMix:
    def test_totals_and_recent_window(self) -> None:
        old = datetime(2026, 1, 5, 7, tzinfo=UTC)  # outside 12-week window
        recent = datetime(2026, 6, 1, 7, tzinfo=UTC)
        activities = [
            _activity(old, "Run", moving_s=7200),
            _activity(recent, "Run", moving_s=3600),
            _activity(recent, "TraditionalStrengthTraining", moving_s=3600),
        ]
        mix = metrics.sport_mix(activities, recent_weeks=12, today=date(2026, 6, 7))
        assert [(m.label, m.sessions, m.total_hours, m.recent_hours) for m in mix] == [
            ("Run", 2, 3.0, 1.0),
            ("Strength", 1, 1.0, 1.0),
        ]

    def test_counts_session_without_moving_time(self) -> None:
        when = datetime(2026, 6, 1, 7, tzinfo=UTC)
        mix = metrics.sport_mix(
            [_activity(when, "Run", moving_s=None)], today=date(2026, 6, 7)
        )
        assert [(m.label, m.sessions, m.total_hours) for m in mix] == [("Run", 1, 0.0)]


def test_strength_week_count_over_trailing_window() -> None:
    week_a = datetime(2026, 6, 1, 7, tzinfo=UTC)
    week_b = datetime(2026, 6, 8, 7, tzinfo=UTC)
    activities = [
        _activity(week_a, "TraditionalStrengthTraining"),
        _activity(week_b, "Run"),
    ]
    weekly = metrics.weekly_sport_hours(activities)
    assert metrics.strength_week_count(weekly, recent_weeks=12) == (1, 2)


def test_metric_series_filters_walking_hr_and_respiratory_rate(
    conn: sqlite3.Connection,
) -> None:
    store.insert_health_metrics(
        conn,
        [
            HealthMetric("walking_hr_avg", datetime(2026, 6, 1, tzinfo=UTC), 82.0),
            HealthMetric(
                "walking_hr_avg", datetime(2026, 6, 2, tzinfo=UTC), 250.0
            ),  # artifact
            HealthMetric("respiratory_rate", datetime(2026, 6, 1, tzinfo=UTC), 14.5),
            HealthMetric(
                "respiratory_rate", datetime(2026, 6, 2, tzinfo=UTC), 90.0
            ),  # artifact
        ],
    )
    assert (
        [v for _, v in metrics.metric_series(conn, "walking_hr_avg")],
        [v for _, v in metrics.metric_series(conn, "respiratory_rate")],
    ) == ([82.0], [14.5])


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


def test_hr_zone_seconds_karvonen_places_easy_run_in_z2() -> None:
    # A low-resting-HR runner (max 193, rest 47) at 148 bpm: %HRmax says 77% -> Z3
    # "moderate", but reserve is (148-47)/(193-47)=69% -> Z2 easy. Karvonen fixes it.
    zones = metrics.hr_zone_seconds([148.0], hr_max=193.0, hr_rest=47.0)
    seconds = {z.label: z.seconds for z in zones}
    assert seconds == {"Z1": 0, "Z2": 1, "Z3": 0, "Z4": 0, "Z5": 0}
    # Z2 band is 60-70% of reserve above resting: 135-149 bpm.
    z2 = next(z for z in zones if z.label == "Z2")
    assert (z2.low_bpm, z2.high_bpm) == (135, 149)


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


def _run(when: datetime, **fields: float | None) -> metrics.Run:
    moving = fields.get("moving_s")
    return metrics.Run(
        activity_id=ActivityId(1),
        source="strava",
        start=when,
        distance_m=fields.get("distance_m", 5000.0),
        moving_s=int(moving) if moving is not None else None,
        avg_pace_s_per_km=fields.get("avg_pace_s_per_km"),
        avg_hr=fields.get("avg_hr"),
        max_hr=None,
        avg_power_w=fields.get("avg_power_w"),
        grade_adj_distance_m=fields.get("grade_adj_distance_m"),
        relative_effort=fields.get("relative_effort"),
    )


def test_run_trend_keeps_only_runs_carrying_the_field() -> None:
    runs = [
        _run(datetime(2026, 6, 1, tzinfo=UTC), avg_power_w=245.0),
        _run(datetime(2026, 6, 2, tzinfo=UTC)),  # no power -> excluded
        _run(datetime(2026, 6, 3, tzinfo=UTC), avg_power_w=260.0),
    ]
    assert metrics.run_trend(runs, lambda r: r.avg_power_w) == [
        (date(2026, 6, 1), 245.0),
        (date(2026, 6, 3), 260.0),
    ]


def test_running_economy_is_speed_per_watt() -> None:
    # 5000 m in 1500 s -> 3.333 m/s; over 250 W -> 0.0133 m/s per watt.
    run = _run(datetime(2026, 6, 1, tzinfo=UTC), moving_s=1500.0, avg_power_w=250.0)
    assert metrics.running_economy(run) == round((5000 / 1500) / 250, 4)


def test_running_economy_none_without_power() -> None:
    run = _run(datetime(2026, 6, 1, tzinfo=UTC), moving_s=1500.0)
    assert metrics.running_economy(run) is None


def test_grade_adjusted_pace_uses_grade_adjusted_distance() -> None:
    # 1500 s over a grade-adjusted 5.0 km -> 300 s/km.
    runs = [
        _run(
            datetime(2026, 6, 1, tzinfo=UTC),
            moving_s=1500.0,
            grade_adj_distance_m=5000.0,
            avg_hr=150.0,
        ),
        _run(datetime(2026, 6, 2, tzinfo=UTC)),  # no grade-adjusted distance
    ]
    points = metrics.grade_adjusted_pace_points(runs)
    assert [(p.pace_s_per_km, p.distance_km, p.avg_hr) for p in points] == [
        (300.0, 5.0, 150.0)
    ]


def test_canonical_run_reads_enriched_columns(conn: sqlite3.Connection) -> None:
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source="strava",
                source_id=SourceId("strava:enriched"),
                sport_type="Run",
                start_time_utc=datetime(2026, 6, 1, 7, tzinfo=UTC),
                distance_m=5000.0,
                moving_s=1500,
                relative_effort=56.0,
                grade_adj_distance_m=5100.0,
                avg_power_w=245.0,
            ),
        ),
    )
    run = metrics.canonical_run_activities(conn)[0]
    assert (run.relative_effort, run.grade_adj_distance_m, run.avg_power_w) == (
        56.0,
        5100.0,
        245.0,
    )
