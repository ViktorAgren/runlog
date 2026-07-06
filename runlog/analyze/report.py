"""Orchestrate the analysis: read the DB, compute metrics, write charts + text.

Read-only. Training analysis covers running workouts only (walking, strength,
cycling, etc. are excluded by the sport filter). Passive off-workout health
metrics (VO2max, resting HR, HRV) are rendered separately under ``recovery/``
and never mixed into the training charts or totals.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from runlog.analyze import analytics, anomaly, charts, metrics, summary
from runlog.db import store

_DEFAULT_HR_REST = 50.0

if TYPE_CHECKING:
    import sqlite3
    from datetime import date
    from pathlib import Path

# Passive, off-workout health metrics kept strictly separate from training data
# (title, y-axis label) so they are never mixed into the running analysis.
_RECOVERY_MARKERS = {
    "vo2max": ("VO2max (Apple estimate)", "ml/kg/min"),
    "resting_hr": ("Resting heart rate (passive)", "bpm"),
    "hrv_sdnn": ("HRV SDNN (passive)", "ms"),
    "spo2": ("Blood oxygen (SpO2)", "fraction"),
    "sleep_hours": ("Sleep duration", "hours"),
    "hr_recovery_1min": ("1-minute HR recovery", "bpm drop"),
    "body_mass": ("Body mass", "kg"),
    "walking_asymmetry": ("Walking asymmetry", "fraction"),
}

# Enriched per-run form/effort fields plotted as daily-marker trend charts,
# each gated on having data so sources lacking the field emit no empty figure.
_FormAccessor = Callable[[metrics.Run], "float | None"]
_FORM_TRENDS: tuple[tuple[str, str, str, _FormAccessor], ...] = (
    ("Running power over time", "Watts", "running_power.png", lambda r: r.avg_power_w),
    (
        "Stride length over time",
        "Metres",
        "stride_length.png",
        lambda r: r.avg_stride_length_m,
    ),
    (
        "Vertical oscillation over time",
        "Centimetres",
        "vertical_oscillation.png",
        lambda r: r.avg_vertical_oscillation_cm,
    ),
    (
        "Ground contact time over time",
        "Milliseconds",
        "ground_contact.png",
        lambda r: r.avg_ground_contact_ms,
    ),
    (
        "Relative effort over time",
        "Effort score",
        "relative_effort.png",
        lambda r: r.relative_effort,
    ),
)


@dataclass(frozen=True)
class ReportResult:
    charts: list[Path]
    summary_text: str


def run(
    db_path: Path,
    out_dir: Path,
    sports: tuple[str, ...] = ("Run", "Running"),
    recent_weeks: int = 6,
    since: date | None = None,
    min_distance_km: float = metrics.MIN_DISTANCE_KM,
    hr_max: float | None = None,
) -> ReportResult:
    """Compute all metrics, render charts into ``out_dir``, return the summary."""
    conn = store.connect(db_path)
    runs = metrics.canonical_run_activities(
        conn, sports, since=since, min_distance_km=min_distance_km
    )
    weekly = metrics.weekly_volume(runs)
    buckets = metrics.fastest_by_bucket(runs)
    pace = metrics.pace_points(runs)
    predictions = metrics.predict_races(runs)

    # Workout HR samples (from run streams only) drive the histogram, zones, and
    # the HR-max estimate — never passive/resting HR.
    hr = metrics.hr_samples(conn, [r.activity_id for r in runs])
    hr_max_value = hr_max if hr_max else metrics.estimated_hr_max(hr)
    zones = metrics.hr_zone_seconds(hr, hr_max_value)

    # Training charts (running workouts only) and passive recovery charts go in
    # separate folders so the two data kinds are never visually mixed.
    training_dir = out_dir / "training"
    recovery_dir = out_dir / "recovery"
    training_dir.mkdir(parents=True, exist_ok=True)
    recovery_dir.mkdir(parents=True, exist_ok=True)
    produced = [
        charts.weekly_volume_chart(weekly, training_dir),
        charts.runs_per_week_chart(weekly, training_dir),
        charts.monthly_by_year_chart(
            metrics.monthly_distance_by_year(runs), training_dir
        ),
        charts.cumulative_ytd_chart(
            metrics.cumulative_distance_by_year(runs), training_dir
        ),
        charts.distance_histogram(metrics.distance_distribution(runs), training_dir),
        charts.training_heatmap_chart(metrics.training_heatmap(runs), training_dir),
        charts.rest_gap_histogram(metrics.run_gap_days(runs), training_dir),
        charts.pace_over_time_chart(pace, training_dir),
        charts.fastest_by_bucket_chart(buckets, training_dir),
        charts.pace_by_weekday_chart(metrics.pace_by_weekday(runs), training_dir),
        charts.race_prediction_chart(predictions, training_dir),
        charts.hr_over_time_chart(metrics.hr_over_time(runs), training_dir),
        charts.hr_histogram(hr, training_dir, zones),
        charts.hr_zones_chart(zones, hr_max_value, training_dir),
        charts.efficiency_chart(pace, training_dir),
        charts.training_load_chart(
            metrics.weekly_training_load(runs, hr_max_value), training_dir
        ),
        charts.start_hour_chart(metrics.start_hour_distribution(runs), training_dir),
    ]

    # Data-dependent charts: only emit them when there is something to plot, so
    # sources that lack a field (e.g. cadence in a Strava bulk export) don't
    # produce an empty, misleading figure.
    cadence = metrics.cadence_points(runs)
    if cadence:
        produced.append(charts.cadence_chart(cadence, training_dir))
    elevation = metrics.monthly_elevation_by_year(runs)
    if elevation:
        produced.append(charts.elevation_by_month_chart(elevation, training_dir))
    grade_pace = metrics.grade_adjusted_pace_points(runs)
    if grade_pace:
        produced.append(charts.grade_adjusted_pace_chart(grade_pace, training_dir))
    for title, ylabel, filename, accessor in _FORM_TRENDS:
        trend = metrics.run_trend(runs, accessor)
        if trend:
            produced.append(
                charts.marker_chart(trend, title, ylabel, filename, training_dir)
            )

    latest_markers: dict[str, tuple[date, float] | None] = {}
    for metric, (title, ylabel) in _RECOVERY_MARKERS.items():
        series = metrics.metric_series(conn, metric, since=since)
        latest_markers[metric] = (
            (series[-1][0].date(), series[-1][1]) if series else None
        )
        daily = metrics.daily_means(series)
        if daily:
            produced.append(
                charts.marker_chart(daily, title, ylabel, f"{metric}.png", recovery_dir)
            )

    # High-level analytics: TRIMP-based Fitness/Fatigue/Form, ACWR, efficiency
    # trend, decoupling, and true best efforts from the streams. Resting HR is
    # used only as a scalar constant in the TRIMP formula (not plotted here).
    analytics_dir = out_dir / "analytics"
    analytics_dir.mkdir(parents=True, exist_ok=True)
    hr_rest = _resolve_hr_rest(conn)
    daily = analytics.daily_trimp(runs, hr_max_value, hr_rest)
    pmc = analytics.performance_management(daily)
    acwr = analytics.acwr_series(daily)
    efficiency = analytics.efficiency_factor(runs)
    ef_trend = analytics.linear_trend(efficiency)
    best_efforts = analytics.best_effort_progressions(conn, runs)
    anomalies = anomaly.analyze(conn, runs, since=since)
    produced += [
        charts.pmc_chart(pmc, analytics_dir),
        charts.acwr_chart(acwr, analytics_dir),
        charts.efficiency_trend_chart(efficiency, ef_trend, analytics_dir),
        charts.decoupling_chart(
            analytics.aerobic_decoupling(conn, runs), analytics_dir
        ),
        charts.best_effort_progression_chart(best_efforts, analytics_dir),
        charts.anomaly_timeline_chart(anomalies, analytics_dir),
    ]

    text = summary.build_summary_text(
        summary=metrics.overall_summary(runs),
        weekly=weekly,
        buckets=buckets,
        latest_markers=latest_markers,
        streak=metrics.active_week_streak(weekly),
        records=metrics.best_efforts(runs),
        predictions=predictions,
        consistency=metrics.consistency_summary(runs),
        recent_weeks=recent_weeks,
    )
    text += "\n" + summary.analytics_section(pmc, acwr, ef_trend, best_efforts)
    text += "\n" + summary.anomaly_section(anomalies)
    return ReportResult(charts=produced, summary_text=text)


def _resolve_hr_rest(conn: sqlite3.Connection) -> float:
    """Median resting HR (a scalar constant for TRIMP), else a default."""
    daily = metrics.daily_means(metrics.metric_series(conn, "resting_hr"))
    if not daily:
        return _DEFAULT_HR_REST
    values = sorted(v for _, v in daily)
    return values[len(values) // 2]
