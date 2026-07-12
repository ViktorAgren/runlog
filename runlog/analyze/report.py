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
    cs,
    html_report,
    lifestyle,
    metrics,
    physiology,
    readiness,
    report_model,
    response,
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
    "walking_hr_avg": ("Walking heart rate (passive cardio)", "bpm"),
    "respiratory_rate": ("Respiratory rate (nightly mean)", "breaths/min"),
}
# Markers tracked for the summary + anomaly detection but too flat to chart.
_UNCHARTED_MARKERS = frozenset({"spo2", "walking_asymmetry"})

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
        "Running economy over time",
        "m/s per watt",
        "running_economy.png",
        metrics.running_economy,
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
            "elevation_by_month",
        ),
    ),
    (
        "All-sport training mix",
        "Every recorded workout — not just running. "
        "Running-only analysis is unaffected.",
        ("sport_hours",),
    ),
    (
        "Pace & racing",
        "Pace trends, elevation-adjusted pace, best efforts, and race projections.",
        (
            "pace_over_time",
            "grade_adjusted_pace",
            "fastest_by_bucket",
            "best_effort_progression",
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
            "training_load",
            "intensity_distribution",
            "cadence",
            "running_power",
            "stride_length",
            "vertical_oscillation",
            "ground_contact",
            "running_economy",
        ),
    ),
    (
        "Fitness & form",
        "Fitness/fatigue balance, workload ratio, aerobic efficiency, and "
        "the critical-speed model.",
        (
            "pmc_fitness_form",
            "acwr",
            "efficiency_factor",
            "aerobic_decoupling",
            "critical_speed",
        ),
    ),
    (
        "Anomalies & readiness",
        "Days and runs that deviate from your own rolling baseline, and how "
        "training load moves next-day recovery.",
        ("anomaly_timeline", "readiness", "load_response"),
    ),
    (
        "Lifestyle & daily patterns",
        "Passive daily-life signals — steps, energy, and sleep rhythm — "
        "separate from training analysis.",
        ("steps", "exercise_minutes", "active_energy", "weekday_profile"),
    ),
)

_TITLE_OVERRIDES = {
    "pmc_fitness_form": "Fitness · Fatigue · Form",
    "acwr": "Acute:chronic workload ratio",
    "hr_over_time": "Heart rate over time",
    "hr_zones": "Time in HR zones",
    "hr_histogram": "HR distribution",
    "grade_adjusted_pace": "Grade-adjusted pace",
    "efficiency_factor": "Aerobic efficiency factor",
    "critical_speed": "Critical speed",
    "readiness": "Daily readiness",
    "running_economy": "Running economy (speed per watt)",
    "vo2max": "VO2max",
    "resting_hr": "Resting heart rate",
    "hrv_sdnn": "HRV (SDNN)",
    "sleep_hours": "Sleep hours",
    "hr_recovery_1min": "HR recovery (1 min)",
    "body_mass": "Body mass",
    "walking_hr_avg": "Walking HR (passive)",
    "respiratory_rate": "Respiratory rate",
    "sport_hours": "Weekly hours by sport",
    "load_response": "Load and next-day recovery",
    "steps": "Daily steps",
    "exercise_minutes": "Exercise minutes",
    "active_energy": "Active energy",
    "weekday_profile": "Weekday rhythm",
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
        charts.pace_over_time_chart(pace, training_dir),
        charts.fastest_by_bucket_chart(buckets, training_dir),
        charts.race_prediction_chart(predictions, training_dir),
        charts.hr_over_time_chart(metrics.hr_over_time(runs), training_dir),
        charts.hr_histogram(hr, training_dir, zones),
        charts.hr_zones_chart(zones, hr_max_value, training_dir),
        charts.training_load_chart(
            metrics.weekly_training_load(runs, hr_max_value), training_dir
        ),
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
        if daily and metric not in _UNCHARTED_MARKERS:
            produced.append(
                charts.marker_chart(daily, title, ylabel, f"{metric}.png", recovery_dir)
            )

    # All-sport training mix: every recorded workout (strength, walks, rides),
    # kept strictly out of the running-only charts and totals above.
    all_activities = metrics.canonical_run_activities(
        conn, metrics.ALL_SPORT_TYPES, since=since, min_distance_km=0.0
    )
    sport_weeks = metrics.weekly_sport_hours(all_activities)
    if sport_weeks:
        produced.append(charts.sport_hours_chart(sport_weeks, training_dir))

    # Lifestyle: passive daily-life signals in their own folder/section.
    lifestyle_dir = out_dir / "lifestyle"
    lifestyle_dir.mkdir(parents=True, exist_ok=True)
    training_days = frozenset(a.start.date() for a in all_activities)
    life = lifestyle.build_lifestyle(conn, training_days, since=since)
    daily_series: dict[str, list[tuple[date, float]]] = {}
    for metric, title, ylabel in (
        ("steps", "Daily steps", "steps"),
        ("exercise_minutes", "Daily exercise minutes", "minutes"),
        ("active_energy", "Daily active energy", "kcal"),
    ):
        daily_series[metric] = metrics.daily_means(
            metrics.metric_series(conn, metric, since=since)
        )
        if daily_series[metric]:
            produced.append(
                charts.marker_chart(
                    daily_series[metric], title, ylabel, f"{metric}.png", lifestyle_dir
                )
            )
    sleep_daily = metrics.daily_means(
        metrics.metric_series(conn, "sleep_hours", since=since)
    )
    profile = lifestyle.weekday_profile(daily_series["steps"], sleep_daily)
    if profile is not None:
        produced.append(charts.weekday_profile_chart(profile, lifestyle_dir))

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

    # Advanced models: Critical Speed from best efforts, and a daily readiness
    # score from the recovery markers (with its link to performance). Skip-empty
    # gated — sparse streams or health data simply omit the figure.
    cs_model = cs.critical_speed(conn, runs)
    if cs_model is not None:
        produced.append(charts.critical_speed_chart(cs_model, analytics_dir))
    readiness_days = readiness.readiness_series(conn, since=since)
    readiness_r = readiness.performance_correlation(readiness_days, runs)
    if readiness_days:
        produced.append(charts.readiness_chart(readiness_days, analytics_dir))

    # Dose-response: how all-sport training load moves next-day recovery.
    responses = response.load_response(conn, hr_max_value, hr_rest, since=since)
    if responses:
        produced.append(charts.load_response_chart(responses, analytics_dir))

    # Stream-based physiology: intensity distribution (polarization) and
    # regression cardiac drift, both from each run's own HR/velocity stream.
    intensity = physiology.training_intensity_distribution(conn, runs, hr_max_value)
    if intensity is not None:
        produced.append(charts.intensity_distribution_chart(intensity, analytics_dir))
    # Cardiac drift feeds a summary line only; as a per-run scatter it was noise.
    drift = streams.run_stream_series(conn, runs, physiology.cardiac_drift_pct)
    median_drift = statistics.median(v for _, v in drift) if drift else None

    # Pace-based intensity split (session intent) alongside the HR-based one,
    # using the athlete's own VDOT pace bands derived from their best 5k.
    pace_intensity = _pace_intensity(conn, runs, best_efforts)

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
    mix_text = summary.training_mix_section(
        metrics.sport_mix(all_activities), metrics.strength_week_count(sport_weeks)
    )
    if mix_text:
        text += "\n" + mix_text
    text += "\n" + summary.analytics_section(pmc, acwr, ef_trend, best_efforts)
    text += "\n" + summary.anomaly_section(anomalies)
    response_text = summary.response_section(responses)
    if response_text:
        text += "\n" + response_text
    lifestyle_text = summary.lifestyle_section(life)
    if lifestyle_text:
        text += "\n" + lifestyle_text
    text += "\n" + summary.physiology_section(intensity, median_drift, pace_intensity)
    readiness_latest = readiness_days[-1] if readiness_days else None
    advanced = summary.advanced_section(cs_model, readiness_latest, readiness_r)
    if advanced:
        text += "\n" + advanced
    quarantined = metrics.quarantined_count(conn)
    if quarantined:
        text += f"\n\n{quarantined} run(s) quarantined (data errors, excluded)."

    span = f"{overall.first_run} to {overall.last_run}"
    model = ReportModel(
        title="Running analytics report",
        subtitle=f"{span} · {overall.run_count} runs",
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        kpis=_build_kpis(
            overall,
            pmc,
            acwr,
            ef_trend,
            intensity,
            latest_markers,
            cs_model,
            readiness_latest,
            life,
        ),
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
    cs_model: cs.CsModel | None,
    readiness_latest: readiness.ReadinessDay | None,
    life: lifestyle.LifestyleSummary | None = None,
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
    if cs_model is not None:
        kpis.append(
            Kpi(
                "Critical speed",
                f"{cs_model.cs_mps:.2f}",
                "m/s",
                f"D' {cs_model.d_prime_m:.0f} m; r={cs_model.r:.2f}",
            )
        )
    if readiness_latest is not None:
        kpis.append(
            Kpi(
                "Readiness",
                f"{readiness_latest.score:.0f}",
                "/100",
                f"latest {readiness_latest.day}; 40-60 normal",
                _readiness_tone(readiness_latest.score),
            )
        )
    markers = (("vo2max", "VO2max", "ml/kg/min"), ("resting_hr", "Resting HR", "bpm"))
    for key, label, unit in markers:
        marker = latest_markers.get(key)
        if marker is not None:
            kpis.append(Kpi(label, f"{marker[1]:.0f}", unit, f"latest {marker[0]}"))
    if life is not None and life.steps_30d is not None:
        kpis.append(Kpi("Steps/day", f"{life.steps_30d:,.0f}", "", "30-day mean"))
    if life is not None and life.sleep_30d is not None:
        context = (
            f"±{life.sleep_sd_30d:.1f} h night-to-night"
            if life.sleep_sd_30d is not None
            else "30-day mean"
        )
        kpis.append(Kpi("Sleep", f"{life.sleep_30d:.1f}", "h", context))
    return kpis


def _readiness_tone(score: float) -> report_model.Tone:
    """Green when recovered (>=60), red when depressed (<40), else neutral."""
    if score >= 60:
        return "good"
    if score < 40:
        return "bad"
    return "neutral"


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


def _pace_intensity(
    conn: sqlite3.Connection,
    runs: Sequence[metrics.Run],
    best_efforts: Sequence[analytics.BestEffortProgression],
) -> physiology.IntensityDistribution | None:
    """Pace-based intensity split using VDOT bands derived from the best 5k."""
    from runlog.plan import targets

    best_5k = next(
        (min(s for _, s in e.progression) for e in best_efforts if e.label == "5k"),
        None,
    )
    if best_5k is None:
        return None
    paces = targets.training_paces(targets.vdot_from_effort(5000, best_5k))
    return physiology.pace_intensity_distribution(
        conn,
        runs,
        easy_ceiling_s=paces["Marathon"][1],
        hard_floor_s=paces["Threshold"][0],
    )


def _resolve_hr_rest(conn: sqlite3.Connection) -> float:
    """Median resting HR (a scalar constant for TRIMP), else a default."""
    daily = metrics.daily_means(metrics.metric_series(conn, "resting_hr"))
    if not daily:
        return _DEFAULT_HR_REST
    values = sorted(v for _, v in daily)
    return values[len(values) // 2]
