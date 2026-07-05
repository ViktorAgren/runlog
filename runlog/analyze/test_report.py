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
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source=source,
                source_id=SourceId(f"{source}:{when.isoformat()}"),
                sport_type="Run" if source == "strava" else "Running",
                start_time_utc=when,
                distance_m=distance_m,
                moving_s=1500,
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


def test_report_run_writes_charts_and_summary(tmp_path: Path) -> None:
    db_path = tmp_path / "runlog.db"
    conn = store.connect(db_path)
    store.init_db(conn)
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC), hr_stream=[140.0, 160.0])
    _add_run(conn, datetime(2026, 6, 8, 7, tzinfo=UTC), distance_m=12000.0)
    store.insert_health_metrics(
        conn, [HealthMetric("vo2max", datetime(2026, 6, 1, tzinfo=UTC), 52.0)]
    )
    conn.close()

    result = report.run(db_path, tmp_path / "out")

    names = {p.name for p in result.charts}
    assert {"weekly_volume.png", "vo2max.png", "hr_histogram.png"} <= names
    assert all(p.exists() for p in result.charts)
    assert "Running summary" in result.summary_text
