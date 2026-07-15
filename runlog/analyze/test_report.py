"""Tests for the summary formatter and end-to-end report generation."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from runlog.analyze import metrics, report, summary
from runlog.db import store
from runlog.domain import (
    Activity,
    ActivityRecord,
    HealthMetric,
    Source,
    SourceId,
    StreamPoint,
)


def _add_run(
    conn: sqlite3.Connection,
    when: datetime,
    *,
    source: Source = "strava",
    distance_m: float = 5000.0,
    hr_stream: list[float] | None = None,
) -> None:
    stream = tuple(
        StreamPoint(offset_s=i, hr=hr) for i, hr in enumerate(hr_stream or [])
    )
    # Moving time follows distance at 5:00/km so fixtures stay plausible and
    # never trip the ingest-time pace quarantine.
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source=source,
                source_id=SourceId(f"{source}:{when.isoformat()}"),
                sport_type="Run" if source == "strava" else "Running",
                start_time_utc=when,
                distance_m=distance_m,
                moving_s=int(distance_m / 1000 * 300),
                avg_pace_s_per_km=300.0,
                avg_hr=150.0,
            ),
            stream=stream,
        ),
    )


def test_build_summary_text_includes_key_sections() -> None:
    text = summary.build_summary_text(
        summary=metrics.Summary(
            run_count=3,
            total_km=17.0,
            first_run=date(2026, 6, 1),
            last_run=date(2026, 6, 20),
            longest_km=12.0,
            this_week_km=5.0,
            this_month_km=17.0,
        ),
        weekly=[metrics.WeeklyVolume(date(2026, 6, 1), 5.0, 1, 5.0)],
        buckets=[metrics.BucketPace("3-5k", 290.0, 2)],
        latest_markers={"vo2max": (date(2026, 6, 1), 52.0), "resting_hr": None},
        streak=(2, 3),
    )
    assert "Total distance 17.0 km" in text
    assert "2 current / 3 longest" in text
    assert "4:50/km" in text  # 290s formatted
    assert "VO2max       52.0  (2026-06-01)" in text


def test_advanced_section_renders_cs_and_readiness() -> None:
    from runlog.analyze.cs import CsModel, CsPoint
    from runlog.analyze.readiness import ReadinessDay
    from runlog.analyze.stats import CorrTest

    model = CsModel(
        cs_mps=5.0, d_prime_m=200.0, r=1.0, points=[CsPoint(5000.0, 1160.0)]
    )
    latest = ReadinessDay(date(2026, 6, 20), 72.0, {})
    corr = CorrTest(r=0.3, ci_low=0.04, ci_high=0.52, p=0.024, n=52)
    text = summary.advanced_section(model, latest, corr)

    # CS 3k time = (3000 - 200) / 5 = 560 s = 9:20; r=0.3 -> ~9% of variance.
    assert "Critical speed 5.00 m/s  (D' 200 m, r=1.00)" in text
    assert "CS 3k   9:20" in text
    assert "Readiness      72/100  (2026-06-20, 40-60 normal)" in text
    assert (
        "r=+0.30 [+0.04, +0.52] p=.024, n=52 "
        "(explains ~9% of off-day variance)" in text
    )


def test_advanced_section_empty_without_models() -> None:
    assert summary.advanced_section(None, None, None) == ""


def test_report_run_writes_charts_and_summary(tmp_path: Path) -> None:
    db_path = tmp_path / "runlog.db"
    conn = store.connect(db_path)
    store.init_db(conn)
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC), hr_stream=[140.0, 160.0])
    _add_run(conn, datetime(2026, 6, 8, 7, tzinfo=UTC), distance_m=12000.0)
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
    store.insert_health_metrics(
        conn,
        [
            HealthMetric("vo2max", datetime(2026, 6, 1, tzinfo=UTC), 52.0),
            HealthMetric("walking_hr_avg", datetime(2026, 6, 1, tzinfo=UTC), 82.0),
            HealthMetric("respiratory_rate", datetime(2026, 6, 1, tzinfo=UTC), 14.5),
            HealthMetric("steps", datetime(2026, 6, 1, tzinfo=UTC), 9500.0),
            HealthMetric("steps", datetime(2026, 6, 2, tzinfo=UTC), 7000.0),
            # A rest day, so the training-vs-rest steps contrast has both sides.
            HealthMetric("steps", datetime(2026, 6, 3, tzinfo=UTC), 5000.0),
        ],
    )
    conn.close()

    result = report.run(db_path, tmp_path / "out")

    names = {p.name for p in result.charts}
    assert {
        "weekly_volume.png",
        "vo2max.png",
        "hr_histogram.png",
        "walking_hr_avg.png",
        "respiratory_rate.png",
    } <= names
    assert all(p.exists() for p in result.charts)
    assert "Running summary" in result.summary_text
    assert "Walking HR" in result.summary_text
    assert "Resp rate" in result.summary_text
    # All-sport mix appears, and the strength session must NOT leak into the
    # running totals (5 km + 12 km runs only).
    assert "sport_hours.png" in names
    assert "Training mix (all sports)" in result.summary_text
    assert "Strength" in result.summary_text
    assert "Total distance 17.0 km" in result.summary_text
    # Lifestyle: steps chart lands in its own folder + summary block appears.
    assert any(
        p.name == "steps.png" and p.parent.name == "lifestyle" for p in result.charts
    )
    assert "Lifestyle (passive daily patterns)" in result.summary_text
