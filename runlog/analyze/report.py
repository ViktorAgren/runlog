"""Orchestrate the analysis: read the DB, compute metrics, write charts + text.

Read-only. Training analysis covers running workouts only (walking, strength,
cycling, etc. are excluded by the sport filter). Passive off-workout health
metrics (VO2max, resting HR, HRV) are rendered separately under ``recovery/``
and never mixed into the training charts or totals.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from runlog.analyze import (
    analytics,
    anomaly,
    charts,
    html_report,
    metrics,
    physiology,
    report_model,
    streams,
    summary,
)
from runlog.analyze.report_model import FigureRef, Kpi, ReportModel, Section
from runlog.db import store

_DEFAULT_HR_REST = 50.0

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import date
    from pathlib import Path

    from runlog.analyze.streams import StreamSample


def _ascent_m(stream: list[StreamSample]) -> float | None:
    stats = streams.climb_stats(stream)
    return stats.ascent_m if stats is not None else None


def _negative_split_pct(stream: list[StreamSample]) -> float | None:
    stats = streams.pacing_stats(stream)
    return stats.negative_split_pct if stats is not None else None


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
    report_html: Path | None = None


# Report sections for the HTML dashboard: (title, narrative, figure stems).
_SECTION_SPEC: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "Volume & consistency",
        "How much and how regularly you run.",
        (
            "weekly_volume",
            "runs_per_week",
            "monthly_by_year",
            "cumulative_distance",
            "training_heatmap",
            "distance_histogram",
            "rest_gaps",
            "start_hours",
            "elevation_by_month",
        ),
    ),
    (
        "Pace & racing",
        "Pace trends, elevation-adjusted pace, best efforts, and race projections.",
        (
            "pace_over_time",
            "grade_adjusted_pace",
            "elevation_gap",
            "pace_by_weekday",
            "fastest_by_bucket",
            "best_effort_progression",
            "route_pace",
            "race_predictions",
        ),
    ),
    (
        "Heart rate & physiology",
        "Effort, running form, and how your body responds to load.",
        (
            "hr_over_time",
            "hr_zones",
            "hr_histogram",
            "efficiency",
            "training_load",
            "intensity_distribution",
            "cardiac_drift",
            "cadence",
            "running_power",
            "stride_length",
            "vertical_oscillation",
            "ground_contact",
            "relative_effort",
            "climb_ascent",
            "pacing_negative_split",
        ),
    ),
    (
        "Fitness & form",
        "Fitness/fatigue balance, workload ratio, and aerobic-efficiency trend.",
        ("pmc_fitness_form", "acwr", "efficiency_factor", "aerobic_decoupling"),
    ),
    (
        "Anomalies",
        "Days and runs that deviate from your own rolling baseline.",
        ("anomaly_timeline",),
    ),
)

_TITLE_OVERRIDES = {
    "pmc_fitness_form": "Fitness · Fatigue · Form",
    "acwr": "Acute:chronic workload ratio",
    "hr_over_time": "Heart rate over time",
    "hr_zones": "Time in HR zones",
    "hr_histogram": "HR distribution",
    "grade_adjusted_pace": "Grade-adjusted pace",
    "elevation_gap": "Elevation-based GAP",
    "efficiency": "Aerobic efficiency (pace vs HR)",
    "efficiency_factor": "Aerobic efficiency factor",
    "pacing_negative_split": "Negative-split %",
    "cardiac_drift": "Cardiac drift %",
    "vo2max": "VO2max",
    "resting_hr": "Resting heart rate",
    "hrv_sdnn": "HRV (SDNN)",
    "spo2": "Blood oxygen (SpO2)",
    "sleep_hours": "Sleep hours",
    "hr_recovery_1min": "HR recovery (1 min)",
    "body_mass": "Body mass",
    "walking_asymmetry": "Walking asymmetry",
}


def _pretty(stem: str) -> str:
    return _TITLE_OVERRIDES.get(stem, stem.replace("_", " ").capitalize())


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

    # Per-second stream analyses (GPS + elevation + velocity): elevation-based
    # grade-adjusted pace, climb load, and pacing discipline, plus same-route
    # pace tracking. All skip-empty gated (runs without streams contribute none).
    gap = streams.run_stream_series(conn, runs, streams.grade_adjusted_pace_s_per_km)
    if gap:
        produced.append(
            charts.pace_trend_chart(
                gap,
                "Grade-adjusted pace (elevation-based)",
                "elevation_gap.png",
                training_dir,
            )
        )
    ascent = streams.run_stream_series(conn, runs, _ascent_m)
    if ascent:
        produced.append(
            charts.marker_chart(
                ascent, "Ascent per run", "metres", "climb_ascent.png", training_dir
            )
        )
    pacing = streams.run_stream_series(conn, runs, _negative_split_pct)
    if pacing:
        produced.append(
            charts.marker_chart(
                pacing,
                "Negative-split % per run",
                "%",
                "pacing_negative_split.png",
                training_dir,
            )
        )
    route_groups = streams.route_pace_series(conn, runs)
    if route_groups:
        produced.append(charts.route_pace_chart(route_groups, training_dir))

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

    # Stream-based physiology: intensity distribution (polarization) and
    # regression cardiac drift, both from each run's own HR/velocity stream.
    intensity = physiology.training_intensity_distribution(conn, runs, hr_max_value)
    if intensity is not None:
        produced.append(charts.intensity_distribution_chart(intensity, analytics_dir))
    drift = streams.run_stream_series(conn, runs, physiology.cardiac_drift_pct)
    if drift:
        produced.append(
            charts.marker_chart(
                drift, "Cardiac drift %", "%", "cardiac_drift.png", analytics_dir
            )
        )
    median_drift = statistics.median(v for _, v in drift) if drift else None

    overall = metrics.overall_summary(runs)
    text = summary.build_summary_text(
        summary=overall,
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
    text += "\n" + summary.physiology_section(intensity, median_drift)

    span = f"{overall.first_run} to {overall.last_run}"
    model = ReportModel(
        title="Running analytics report",
        subtitle=f"{span} · {overall.run_count} runs",
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        kpis=_build_kpis(overall, pmc, acwr, ef_trend, intensity, latest_markers),
        sections=_build_sections(produced, out_dir),
    )
    report_html = html_report.render(model, out_dir)
    return ReportResult(charts=produced, summary_text=text, report_html=report_html)


def _build_kpis(
    overall: metrics.Summary,
    pmc: Sequence[analytics.PmcPoint],
    acwr: Sequence[tuple[date, float]],
    ef_trend: analytics.Trend | None,
    intensity: physiology.IntensityDistribution | None,
    latest_markers: dict[str, tuple[date, float] | None],
) -> list[Kpi]:
    kpis = [
        Kpi(
            "Total distance",
            f"{overall.total_km:.0f}",
            "km",
            f"{overall.run_count} runs",
        ),
        Kpi("This week", f"{overall.this_week_km:.0f}", "km", "current training week"),
    ]
    if pmc:
        last = pmc[-1]
        kpis.append(
            Kpi(
                "Fitness (CTL)",
                f"{last.fitness:.0f}",
                context=f"form {last.form:+.0f} TSB",
                tone=report_model.form_tone(last.form),
            )
        )
    if acwr:
        value = acwr[-1][1]
        kpis.append(
            Kpi(
                "ACWR",
                f"{value:.2f}",
                context="sweet spot 0.8-1.3",
                tone=report_model.acwr_tone(value),
            )
        )
    if intensity is not None:
        tone = (
            "good"
            if intensity.easy_pct >= 70
            else "warn" if intensity.easy_pct >= 50 else "bad"
        )
        kpis.append(
            Kpi(
                "Easy share",
                f"{intensity.easy_pct:.0f}",
                "%",
                f"easy:hard {intensity.easy_to_hard}; ~80% ideal",
                tone,
            )
        )
    if ef_trend is not None:
        tone = "good" if ef_trend.per_30_days >= 0 else "warn"
        kpis.append(
            Kpi(
                "Efficiency",
                f"{ef_trend.per_30_days:+.3f}",
                "/mo",
                f"r={ef_trend.r:.2f}",
                tone,
            )
        )
    markers = (("vo2max", "VO2max", "ml/kg/min"), ("resting_hr", "Resting HR", "bpm"))
    for key, label, unit in markers:
        marker = latest_markers.get(key)
        if marker is not None:
            kpis.append(Kpi(label, f"{marker[1]:.0f}", unit, f"latest {marker[0]}"))
    return kpis


def _build_sections(produced: Sequence[Path], out_dir: Path) -> list[Section]:
    by_stem = {p.stem: p.relative_to(out_dir).as_posix() for p in produced}
    recovery = [
        (p.stem, rel)
        for p in produced
        if (rel := p.relative_to(out_dir).as_posix()).startswith("recovery/")
    ]
    sections: list[Section] = []
    for title, narrative, stems in _SECTION_SPEC:
        figures = [FigureRef(_pretty(s), by_stem[s]) for s in stems if s in by_stem]
        if figures:
            sections.append(Section(title, narrative, figures))
    if recovery:
        sections.append(
            Section(
                "Recovery & fitness markers",
                "Passive off-workout signals, kept separate from training load.",
                [FigureRef(_pretty(stem), rel) for stem, rel in recovery],
            )
        )
    return sections


def _resolve_hr_rest(conn: sqlite3.Connection) -> float:
    """Median resting HR (a scalar constant for TRIMP), else a default."""
    daily = metrics.daily_means(metrics.metric_series(conn, "resting_hr"))
    if not daily:
        return _DEFAULT_HR_REST
    values = sorted(v for _, v in daily)
    return values[len(values) // 2]
