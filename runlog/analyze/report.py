"""Orchestrate the analysis: read the DB, compute metrics, write charts + text.

Read-only. Training analysis covers running workouts only (walking, strength,
cycling, etc. are excluded by the sport filter). Passive off-workout health
metrics (VO2max, resting HR, HRV) are rendered separately under ``recovery/``
and never mixed into the training charts or totals.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from runlog.analyze import (
    activities_view,
    analytics,
    anomaly,
    charts,
    cs,
    curves,
    durability,
    energy,
    forecast,
    html_report,
    importance,
    lifestyle,
    load,
    metrics,
    persist,
    physiology,
    race,
    readiness,
    records,
    report_model,
    response,
    rolling,
    stats,
    streams,
    summary,
)
from runlog.analyze.report_model import FigureRef, Kpi, ReportModel, Section
from runlog.analyze.series import Scalar, from_trends
from runlog.db import queries, store

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import date
    from pathlib import Path

    from runlog.config import Athlete, Sex


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
    ("Running power over time", "Watts", "running_power.svg", lambda r: r.avg_power_w),
    (
        "Stride length over time",
        "Metres",
        "stride_length.svg",
        lambda r: r.avg_stride_length_m,
    ),
    (
        "Vertical oscillation over time",
        "Centimetres",
        "vertical_oscillation.svg",
        lambda r: r.avg_vertical_oscillation_cm,
    ),
    (
        "Ground contact time over time",
        "Milliseconds",
        "ground_contact.svg",
        lambda r: r.avg_ground_contact_ms,
    ),
    (
        "Running economy over time",
        "m/s per watt",
        "running_economy.svg",
        metrics.running_economy,
    ),
)


@dataclass(frozen=True)
class ReportResult:
    charts: list[Path]
    summary_text: str
    report_html: Path | None = None
    activity_pages: list[Path] = field(default_factory=list)
    data_exports: list[Path] = field(default_factory=list)


# Report sections for the HTML dashboard: (title, narrative, figure stems).
_SECTION_SPEC: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "Volume & consistency",
        "Training volume and its week-to-week regularity over the reporting period.",
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
        "All recorded workouts across sports; the running-specific analyses "
        "elsewhere are unaffected.",
        ("sport_hours",),
    ),
    (
        "Pace & racing",
        "Pace trends, elevation-adjusted pace, maximal efforts and race-time "
        "projections.",
        (
            "pace_over_time",
            "pace_by_session",
            "grade_adjusted_pace",
            "best_effort_pace",
            "best_effort_progression",
            "race_predictions",
            "records",
        ),
    ),
    (
        "Heart rate & physiology",
        "Cardiovascular effort, heart-rate zone distribution and running-form "
        "kinematics.",
        (
            "hr_over_time",
            "pace_hr_density",
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
        "Fitness-fatigue balance, workload ratio, aerobic efficiency and the "
        "critical-speed model.",
        (
            "pmc_fitness_form",
            "acwr",
            "efficiency_factor",
            "aerobic_decoupling",
            "critical_speed",
            "lactate_thresholds",
        ),
    ),
    (
        "Performance models",
        "Mean-maximal speed and the critical-speed model, modelled race fitness "
        "(VDOT), training intensity structure (Polarization Index), long-run "
        "durability, and training-load structure.",
        (
            "mean_maximal_curve",
            "vdot",
            "polarization_index",
            "durability",
            "training_monotony",
            "training_strain",
            "correlation_matrix",
        ),
    ),
    (
        "Anomalies & readiness",
        "Signals deviating from their rolling robust baseline, and the next-day "
        "recovery response to training load.",
        ("anomaly_timeline", "readiness", "load_response"),
    ),
    (
        "Energy & expenditure",
        "Estimated energy expenditure: resting metabolism plus active energy, "
        "and the energy cost per kilometre.",
        ("energy_breakdown", "tdee", "active_energy", "energy_cost"),
    ),
    (
        "Lifestyle & daily patterns",
        "Passive daily-life signals (steps, movement, sleep rhythm), tracked "
        "separately from the training analysis.",
        (
            "steps",
            "exercise_minutes",
            "physical_effort",
            "walking_speed",
            "flights_climbed",
            "weekday_profile",
        ),
    ),
)

# One source of truth for each figure's caption: (title, description). The title
# and description render only in the HTML <figcaption> (the figures carry no
# embedded title), and the descriptions state what is measured, not coaching
# slogans.
_FIGURES: dict[str, tuple[str, str]] = {
    # Volume & consistency
    "weekly_volume": (
        "Weekly volume",
        "Distance per calendar week with a 4-week rolling mean.",
    ),
    "runs_per_week": ("Runs per week", "Number of sessions per calendar week."),
    "monthly_by_year": (
        "Monthly distance",
        "Distance per calendar month, grouped by year.",
    ),
    "cumulative_distance": (
        "Cumulative distance",
        "Distance accumulated through each year to date.",
    ),
    "training_heatmap": (
        "Training calendar",
        "Distance by weekday and week; darker denotes a longer run.",
    ),
    "distance_histogram": (
        "Run-distance distribution",
        "Distribution of per-run distance, with the median marked.",
    ),
    "elevation_by_month": (
        "Elevation gain",
        "Metres ascended per calendar month, grouped by year.",
    ),
    # All-sport mix
    "sport_hours": (
        "Weekly hours by sport",
        "Training duration per week, stacked by sport type.",
    ),
    # Pace & racing
    "pace_over_time": (
        "Pace over time",
        "Mean pace per run with a rolling median (lower is faster).",
    ),
    "pace_by_session": (
        "Pace by session type",
        "Distribution of mean pace per run, by detected session type.",
    ),
    "grade_adjusted_pace": (
        "Grade-adjusted pace",
        "Pace corrected for elevation (Strava GAP).",
    ),
    "best_effort_pace": (
        "Fastest pace by distance",
        "Fastest sustained pace at each distance, from the streams.",
    ),
    "best_effort_progression": (
        "Best-effort progression",
        "All-time-best pace at each distance over time.",
    ),
    "race_predictions": (
        "Predicted race times",
        "Riegel projections from current best efforts.",
    ),
    "records": (
        "Personal records",
        "All-time (filled) and seasonal (open) bests over time.",
    ),
    # Heart rate & physiology
    "hr_over_time": (
        "Heart rate over time",
        "Mean heart rate per run with a rolling median.",
    ),
    "pace_hr_density": (
        "Heart rate vs speed",
        "Joint density of heart rate and speed across every run's stream.",
    ),
    "hr_zones": (
        "Time in heart-rate zones",
        "Minutes accumulated in each Karvonen heart-rate-reserve zone.",
    ),
    "hr_histogram": (
        "Heart-rate distribution",
        "Time spent at each heart rate, with the zone thresholds.",
    ),
    "training_load": (
        "Weekly training load",
        "Heart-rate-weighted load (duration × intensity) per week.",
    ),
    "intensity_distribution": (
        "Intensity distribution",
        "Share of time in the easy, moderate and hard heart-rate zones.",
    ),
    "cadence": ("Cadence over time", "Mean step rate per run with a rolling mean."),
    "running_power": (
        "Running power",
        "Mean mechanical power per run (device estimate).",
    ),
    "stride_length": ("Stride length", "Mean stride length per run."),
    "vertical_oscillation": (
        "Vertical oscillation",
        "Mean vertical oscillation per run.",
    ),
    "ground_contact": ("Ground-contact time", "Mean ground-contact time per run."),
    "running_economy": (
        "Running economy",
        "Speed per unit mechanical power (m/s per watt).",
    ),
    # Fitness & form
    "pmc_fitness_form": (
        "Fitness, fatigue and form",
        "Banister model: fitness (CTL), fatigue (ATL) and form (TSB).",
    ),
    "acwr": (
        "Acute:chronic workload ratio",
        "Seven-day load relative to the 28-day chronic load.",
    ),
    "efficiency_factor": (
        "Aerobic efficiency",
        "Speed per heartbeat over time (efficiency factor).",
    ),
    "aerobic_decoupling": (
        "Aerobic decoupling",
        "Pace-to-HR drift between the first and second half of long runs.",
    ),
    "critical_speed": (
        "Critical speed",
        "Speed-duration best efforts fitted to the critical-speed model.",
    ),
    "lactate_thresholds": (
        "Lactate thresholds",
        "Aerobic (LT1) and lactate (LT2) threshold speed from the CS model.",
    ),
    # Performance models
    "mean_maximal_curve": (
        "Mean-maximal speed curve",
        "Fastest mean speed sustained at each duration, with the fitted CS.",
    ),
    "vdot": (
        "VDOT",
        "VO₂max-equivalent race fitness (Daniels-Gilbert), with CI and PI bands.",
    ),
    "polarization_index": (
        "Polarization index",
        "Treff index of the intensity distribution over time (above 2 is polarized).",
    ),
    "durability": (
        "Durability",
        "Within-long-run efficiency decline from the fresh to the fatigued third.",
    ),
    "training_monotony": (
        "Training monotony",
        "Weekly mean daily load divided by its standard deviation (Foster).",
    ),
    "training_strain": (
        "Training strain",
        "Weekly load multiplied by monotony (Foster).",
    ),
    "correlation_matrix": (
        "Correlation matrix",
        "Pearson correlation between daily training and recovery signals.",
    ),
    # Anomalies & readiness
    "anomaly_timeline": (
        "Anomaly timeline",
        "Days flagged against each metric's rolling robust baseline.",
    ),
    "readiness": (
        "Daily readiness",
        "Composite recovery score from resting HR, HRV, sleep and HR recovery.",
    ),
    "load_response": (
        "Load-response",
        "Next-day deviation of each recovery marker by prior-day load.",
    ),
    # Energy & expenditure
    "energy_breakdown": (
        "Daily energy expenditure",
        "Resting metabolism (Mifflin-St Jeor) plus active energy.",
    ),
    "tdee": (
        "Total daily energy expenditure",
        "Estimated total daily energy expenditure over time.",
    ),
    "active_energy": ("Active energy", "Active energy expenditure over time."),
    "energy_cost": ("Energy cost per km", "Energy expended per kilometre over time."),
    # Lifestyle
    "steps": ("Daily steps", "Daily step count over time."),
    "exercise_minutes": ("Exercise minutes", "Daily exercise minutes over time."),
    "physical_effort": (
        "Physical effort",
        "Daily physical-effort (MET) index over time.",
    ),
    "walking_speed": ("Walking speed", "Average walking speed over time."),
    "flights_climbed": ("Flights climbed", "Flights of stairs ascended per day."),
    "weekday_profile": (
        "Weekday rhythm",
        "Mean steps (bars) and sleep (line) by weekday.",
    ),
    # Recovery markers
    "vo2max": ("VO₂max", "Apple VO₂max estimate over time."),
    "resting_hr": ("Resting heart rate", "Resting heart rate over time."),
    "hrv_sdnn": ("Heart-rate variability", "Overnight HRV (SDNN) over time."),
    "sleep_hours": ("Sleep duration", "Nightly sleep duration over time."),
    "hr_recovery_1min": (
        "Heart-rate recovery",
        "One-minute post-exercise heart-rate recovery over time.",
    ),
    "body_mass": ("Body mass", "Body mass over time."),
    "walking_hr_avg": ("Walking heart rate", "Average walking heart rate over time."),
    "respiratory_rate": ("Respiratory rate", "Overnight respiratory rate over time."),
}


def _figure_meta(stem: str) -> tuple[str, str]:
    """(title, description) for a figure stem, falling back to a prettified stem."""
    return _FIGURES.get(stem, (stem.replace("_", " ").capitalize(), ""))


def _figure_ref(stem: str, path: str) -> FigureRef:
    title, description = _figure_meta(stem)
    return FigureRef(title, path, description)


def run(
    db_path: Path,
    out_dir: Path,
    sports: tuple[str, ...] = ("Run", "Running"),
    recent_weeks: int = 6,
    since: date | None = None,
    min_distance_km: float = metrics.MIN_DISTANCE_KM,
    hr_max: float | None = None,
    hr_rest: float | None = None,
    sex: Sex | None = None,
    athlete: Athlete | None = None,
    save: bool = True,
) -> ReportResult:
    """Compute all metrics, render charts into ``out_dir``, return the summary."""
    conn = store.connect(db_path)
    store.init_db(conn)  # ensure the derived-metrics tables exist before persisting
    # One shared stream load per run for the whole read-only pass (the report
    # reads each run's stream many times).
    with streams.stream_cache(conn):
        return _run(
            conn,
            out_dir,
            db_path=db_path,
            sports=sports,
            recent_weeks=recent_weeks,
            since=since,
            min_distance_km=min_distance_km,
            hr_max=hr_max,
            hr_rest=hr_rest,
            sex=sex,
            athlete=athlete,
            save=save,
        )


def _run(
    conn: sqlite3.Connection,
    out_dir: Path,
    *,
    db_path: Path,
    sports: tuple[str, ...],
    recent_weeks: int,
    since: date | None,
    min_distance_km: float,
    hr_max: float | None,
    hr_rest: float | None,
    sex: Sex | None,
    athlete: Athlete | None,
    save: bool,
) -> ReportResult:
    runs = metrics.canonical_run_activities(
        conn, sports, since=since, min_distance_km=min_distance_km
    )
    weekly = metrics.weekly_volume(runs)
    efforts = analytics.best_effort_records(conn, runs)
    pace = metrics.pace_points(runs)
    predictions = metrics.predict_races(runs)

    # Workout HR samples (from run streams only) drive the histogram, zones, and
    # the HR-max estimate — never passive/resting HR.
    hr = queries.hr_samples(conn, [r.activity_id for r in runs])
    hr_max_value = hr_max if hr_max else metrics.estimated_hr_max(hr)
    hr_rest_value = hr_rest if hr_rest is not None else metrics.resting_hr_median(conn)
    zones = metrics.hr_zone_seconds(hr, hr_max_value, hr_rest_value)

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
        charts.best_effort_pace_chart(efforts, training_dir),
        charts.race_prediction_chart(predictions, training_dir),
        charts.hr_over_time_chart(metrics.hr_over_time(runs), training_dir),
        charts.pace_hr_density_chart(streams.speed_hr_pairs(conn, runs), training_dir),
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
    # Trend candidates for the what-matters panel: (label, lane, unit, series).
    trend_series: list[tuple[str, str, str, list[tuple[date, float]]]] = []
    for title, ylabel, filename, accessor in _FORM_TRENDS:
        trend = metrics.run_trend(runs, accessor)
        if trend:
            produced.append(
                charts.marker_chart(trend, title, ylabel, filename, training_dir)
            )
            trend_series.append(
                (
                    f"{_figure_meta(filename[:-4])[0]} trend",
                    "training",
                    f"{ylabel}/30d",
                    trend,
                )
            )

    latest_markers: dict[str, tuple[date, float] | None] = {}
    for metric, (title, ylabel) in _RECOVERY_MARKERS.items():
        series = metrics.metric_series(conn, metric, since=since)
        latest_markers[metric] = (
            (series[-1][0].date(), series[-1][1]) if series else None
        )
        daily = metrics.daily_means(series)
        if daily:
            trend_series.append(
                (f"{_figure_meta(metric)[0]} trend", "recovery", f"{ylabel}/30d", daily)
            )
        if daily and metric not in _UNCHARTED_MARKERS:
            produced.append(
                charts.marker_chart(daily, title, ylabel, f"{metric}.svg", recovery_dir)
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
        ("physical_effort", "Daily physical effort", "MET (daily mean)"),
        ("walking_speed", "Walking speed", "m/s (daily mean)"),
        ("flights_climbed", "Flights climbed", "flights/day"),
    ):
        daily_series[metric] = metrics.daily_means(
            metrics.metric_series(conn, metric, since=since)
        )
        if daily_series[metric]:
            produced.append(
                charts.marker_chart(
                    daily_series[metric], title, ylabel, f"{metric}.svg", lifestyle_dir
                )
            )
            trend_series.append(
                (
                    f"{_figure_meta(metric)[0]} trend",
                    "lifestyle",
                    f"{ylabel}/30d",
                    daily_series[metric],
                )
            )
    sleep_daily = metrics.daily_means(
        metrics.metric_series(conn, "sleep_hours", since=since)
    )
    profile = lifestyle.weekday_profile(daily_series["steps"], sleep_daily)
    if profile is not None:
        produced.append(charts.weekday_profile_chart(profile, lifestyle_dir))

    # Energy: resting (BMR) + active = total daily expenditure, plus the
    # per-run energy cost. Needs athlete demographics; omitted without them.
    energy_dir = out_dir / "energy"
    energy_dir.mkdir(parents=True, exist_ok=True)
    energy_days = energy.energy_series(conn, athlete, since=since)
    energy_summary = energy.build_energy(conn, athlete, training_days, since=since)
    if energy_days:
        produced.append(charts.energy_breakdown_chart(energy_days, energy_dir))
        tdee_daily = [(d.day, d.tdee) for d in energy_days]
        produced.append(
            charts.marker_chart(
                tdee_daily,
                "Total daily energy expenditure",
                "kcal",
                "tdee.svg",
                energy_dir,
            )
        )
        trend_series.append(("TDEE trend", "energy", "kcal/30d", tdee_daily))
    active_daily = metrics.daily_means(
        metrics.metric_series(conn, "active_energy", since=since)
    )
    if active_daily:
        produced.append(
            charts.marker_chart(
                active_daily,
                "Daily active energy",
                "kcal",
                "active_energy.svg",
                energy_dir,
            )
        )
        trend_series.append(("Active energy trend", "energy", "kcal/30d", active_daily))
    # Calories are provider-specific, so pin the cost series to one source.
    cost_source = energy.dominant_calorie_source(runs)
    cost_daily = energy.energy_cost_series(runs, source=cost_source)
    cost_trend = stats.trend_test(cost_daily) if cost_daily else None
    if cost_daily:
        produced.append(
            charts.marker_chart(
                cost_daily,
                f"Energy cost per km ({cost_source} calories)",
                "kcal/km",
                "energy_cost.svg",
                energy_dir,
            )
        )
        trend_series.append(("Energy cost trend", "energy", "kcal/km/30d", cost_daily))

    # High-level analytics: TRIMP-based Fitness/Fatigue/Form, ACWR, efficiency
    # trend, decoupling, and true best efforts from the streams. Resting HR is
    # used only as a scalar constant in the TRIMP formula (not plotted here).
    analytics_dir = out_dir / "analytics"
    analytics_dir.mkdir(parents=True, exist_ok=True)
    daily = analytics.daily_trimp(runs, hr_max_value, hr_rest_value, sex)
    pmc = analytics.performance_management(daily)
    acwr = analytics.acwr_series(daily)
    efficiency = analytics.efficiency_factor(runs)
    ef_trend = analytics.linear_trend(efficiency)
    ef_test = stats.trend_test(efficiency)
    best_efforts = analytics.best_effort_progressions(conn, runs)
    anomalies = anomaly.analyze(conn, runs, since=since)
    produced += [
        charts.pmc_chart(pmc, analytics_dir),
        charts.acwr_chart(acwr, analytics_dir),
        charts.efficiency_trend_chart(efficiency, ef_trend, analytics_dir, ef_test),
        charts.decoupling_chart(
            analytics.aerobic_decoupling(conn, runs), analytics_dir
        ),
        charts.best_effort_progression_chart(best_efforts, analytics_dir),
        charts.anomaly_timeline_chart(anomalies, analytics_dir),
    ]

    # Personal-record timeline and an updating race forecast (race from the
    # active plan when one is present next to the DB).
    record_events = records.records_timeline(conn, runs)
    if record_events:
        produced.append(charts.records_chart(record_events, analytics_dir))
    race_forecast = _race_forecast(conn, runs, db_path)

    # Advanced models: Critical Speed from best efforts, and a daily readiness
    # score from the recovery markers (with its link to performance). Skip-empty
    # gated — sparse streams or health data simply omit the figure.
    cs_model = cs.critical_speed(conn, runs)
    if cs_model is not None:
        produced.append(charts.critical_speed_chart(cs_model, analytics_dir))
    # Mean-maximal speed curve (power-duration analogue) and its CS/D' fit.
    mm_curve = curves.mean_maximal_curve(conn, runs)
    mm_model = curves.fit(mm_curve)
    if mm_curve:
        produced.append(
            charts.mean_maximal_curve_chart(mm_curve, mm_model, analytics_dir)
        )
    # Estimated LT1/LT2 threshold pace over time: critical speed is the LT2/MLSS
    # velocity (rolling re-fit per window), LT1 a fixed fraction of it.
    cs_series = cs.critical_speed_series(conn, runs, datetime.now().date())
    lt_series = cs.lactate_threshold_speeds(cs_series)
    if lt_series:
        produced.append(charts.lactate_threshold_chart(lt_series, analytics_dir))
        trend_series.append(
            (
                "LT2 threshold speed trend",
                "training",
                "km/h/30d",
                [(day, lt2) for day, _lt1, lt2 in lt_series],
            )
        )
    vdot_points = race.vdot_series(cs_series)
    if vdot_points:
        trend_series.append(
            ("VDOT (from critical speed) trend", "training", "/30d", vdot_points)
        )
        produced.append(
            charts.regression_trend_chart(
                vdot_points,
                "VDOT (modelled race fitness)",
                "Rolling critical-speed fits, with 95% CI + prediction bands",
                "VDOT",
                "vdot.svg",
                analytics_dir,
            )
        )
    readiness_days = readiness.readiness_series(conn, since=since)
    readiness_r = readiness.performance_correlation(readiness_days, runs)
    if readiness_days:
        produced.append(charts.readiness_chart(readiness_days, analytics_dir))

    # Dose-response: how all-sport training load moves next-day recovery.
    responses = response.load_response(
        conn, hr_max_value, hr_rest_value, since=since, sex=sex
    )
    if responses:
        produced.append(charts.load_response_chart(responses, analytics_dir))

    # Stream-based physiology: intensity distribution (polarization) and
    # regression cardiac drift, both from each run's own HR/velocity stream.
    intensity = physiology.training_intensity_distribution(
        conn, runs, hr_max_value, hr_rest_value
    )
    if intensity is not None:
        produced.append(charts.intensity_distribution_chart(intensity, analytics_dir))
    # Cardiac drift feeds a summary line only; as a per-run scatter it was noise.
    drift = streams.run_stream_series(conn, runs, physiology.cardiac_drift_pct)
    median_drift = statistics.median(v for _, v in drift) if drift else None

    # Pace-based intensity split (session intent) alongside the HR-based one,
    # using the athlete's own VDOT pace bands derived from their best 5k.
    pace_intensity = _pace_intensity(conn, runs, best_efforts)

    # The full test family, BH-FDR-corrected once: every trend the report
    # draws, plus the cross-lane correlations and contrasts.
    trend_series.append(("Efficiency factor trend", "training", "EF/30d", efficiency))
    trend_series.append(
        (
            "Weekly running volume trend",
            "training",
            "km-wk/30d",
            [(w.week_start, w.distance_km) for w in weekly],
        )
    )
    # Load structure (Foster): monotony, strain, and week-over-week ramp rate.
    monotony, strain, ramp = (
        load.monotony_series(daily),
        load.strain_series(daily),
        load.ramp_series(daily),
    )
    for label, unit, points in (
        ("Training monotony trend", "ratio/30d", monotony),
        ("Training strain trend", "load/30d", strain),
        ("Weekly load ramp trend", "%/30d", ramp),
    ):
        if points:
            trend_series.append((label, "training", unit, points))
    if monotony:
        produced.append(
            charts.marker_chart(
                monotony,
                "Training monotony",
                "mean/SD",
                "training_monotony.svg",
                analytics_dir,
            )
        )
    if strain:
        produced.append(
            charts.marker_chart(
                strain, "Training strain", "load", "training_strain.svg", analytics_dir
            )
        )
    # Polarization Index over time (Treff): training intensity structure.
    polarization = physiology.polarization_series(
        physiology.run_intensity_bands(conn, runs, hr_max_value, hr_rest_value)
    )
    if polarization:
        trend_series.append(
            ("Polarization index trend", "training", "a.u./30d", polarization)
        )
        produced.append(
            charts.marker_chart(
                polarization,
                "Polarization Index",
                "a.u.",
                "polarization_index.svg",
                analytics_dir,
            )
        )
    # Durability: within-long-run efficiency fade (fatigue resistance).
    fade = durability.durability_series(conn, runs)
    if fade:
        trend_series.append(
            ("Durability (efficiency fade) trend", "training", "%/30d", fade)
        )
        produced.append(
            charts.marker_chart(
                fade,
                "Durability (long-run efficiency fade)",
                "% fade",
                "durability.svg",
                analytics_dir,
            )
        )
    # Rolling r(t): how next-day HRV's coupling to training load drifts over time.
    coupling = rolling.rolling_correlation(
        daily, metrics.daily_means(metrics.metric_series(conn, "hrv_sdnn"))
    )
    if coupling:
        trend_series.append(("Load-HRV coupling trend", "cross", "r/30d", coupling))
    # Correlation matrix across the key daily signals (common-date Pearson r).
    corr_labels, corr_matrix = analytics.correlation_matrix(
        [
            ("Load", daily),
            ("HRV", metrics.daily_means(metrics.metric_series(conn, "hrv_sdnn"))),
            ("Rest HR", metrics.daily_means(metrics.metric_series(conn, "resting_hr"))),
            ("Sleep", metrics.daily_means(metrics.metric_series(conn, "sleep_hours"))),
            (
                "HR rec",
                metrics.daily_means(metrics.metric_series(conn, "hr_recovery_1min")),
            ),
            ("VO2max", metrics.daily_means(metrics.metric_series(conn, "vo2max"))),
            (
                "Resp",
                metrics.daily_means(metrics.metric_series(conn, "respiratory_rate")),
            ),
        ]
    )
    if any(
        value is not None
        for i, row in enumerate(corr_matrix)
        for j, value in enumerate(row)
        if i != j
    ):
        produced.append(
            charts.correlation_heatmap_chart(corr_labels, corr_matrix, analytics_dir)
        )
    table = importance.build_table(
        _collect_candidates(trend_series, responses, readiness_r, life)
    )

    overall = metrics.overall_summary(runs)
    text = summary.build_summary_text(
        summary=overall,
        weekly=weekly,
        efforts=efforts,
        latest_markers=latest_markers,
        streak=metrics.active_week_streak(weekly),
        predictions=predictions,
        consistency=metrics.consistency_summary(runs),
        recent_weeks=recent_weeks,
    )
    importance_text = summary.importance_section(table)
    if importance_text:
        text += "\n" + importance_text
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
    energy_text = summary.energy_section(energy_summary, cost_trend)
    if energy_text:
        text += "\n" + energy_text
    elif athlete is None:
        # Say why the section is absent; a silent omission looks like a bug.
        text += (
            "\n\nEnergy expenditure not estimated. Set RUNLOG_ATHLETE_SEX, "
            "RUNLOG_ATHLETE_HEIGHT_CM and RUNLOG_ATHLETE_BIRTH_DATE in .env "
            "(see .env.example)."
        )
    text += "\n" + summary.physiology_section(intensity, median_drift, pace_intensity)
    records_text = summary.records_section(record_events, race_forecast)
    if records_text:
        text += "\n" + records_text
    readiness_latest = readiness_days[-1] if readiness_days else None
    advanced = summary.advanced_section(cs_model, readiness_latest, readiness_r)
    if advanced:
        text += "\n" + advanced
    quarantined = metrics.quarantined_count(conn)
    if quarantined:
        text += f"\n\n{quarantined} run(s) quarantined (data errors, excluded)."

    # Activities tab: a row per run, plus a full-resolution detail page each.
    activity_rows, activity_details = activities_view.build_activities(
        conn, runs, hr_max_value, hr_rest_value, sex
    )
    activity_pages = html_report.render_activity_pages(activity_details, out_dir)
    produced.append(charts.pace_by_session_chart(activity_rows, training_dir))

    # Persist every computed series to the derived store and export tidy CSVs,
    # so the timeseries are queryable/exportable without a recompute.
    data_exports: list[Path] = []
    if save:
        scalars: list[Scalar] = []
        if mm_model is not None:
            scalars.append(
                Scalar("critical_speed_kmh", round(mm_model.cs_mps * 3.6, 2), "km/h")
            )
            scalars.append(Scalar("d_prime_m", round(mm_model.d_prime_m), "m"))
        persist.persist(
            conn, from_trends(trend_series), scalars, datetime.now().isoformat()
        )
        data_exports = persist.export(conn, out_dir)

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
            ef_test,
            energy_summary,
        ),
        sections=_build_sections(produced, out_dir),
        stats=_stats_table(table),
        activities=activity_rows,
        summary=summary.abstract(overall, pmc, table),
    )
    report_html = html_report.render(model, out_dir)
    return ReportResult(
        charts=produced,
        summary_text=text,
        report_html=report_html,
        activity_pages=activity_pages,
        data_exports=data_exports,
    )


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
    ef_test: stats.TrendTest | None = None,
    energy_summary: energy.EnergySummary | None = None,
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
        if ef_test is not None:
            low, high = ef_test.ci_30d
            context = f"[{low:+.3f}, {high:+.3f}]/mo, {stats.format_p(ef_test.p)}"
        else:
            context = f"r={ef_trend.r:.2f}"
        kpis.append(
            Kpi(
                "Efficiency",
                f"{ef_trend.per_30_days:+.3f}",
                "/mo",
                context,
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
    if energy_summary is not None:
        method = (
            "measured basal"
            if energy_summary.method == "measured-basal"
            else "Mifflin-St Jeor"
        )
        kpis.append(
            Kpi(
                "Resting (BMR)", f"{energy_summary.bmr_latest:,.0f}", "kcal/day", method
            )
        )
        if energy_summary.tdee_30d is not None:
            kpis.append(
                Kpi(
                    "Expenditure",
                    f"{energy_summary.tdee_30d:,.0f}",
                    "kcal/day",
                    "30-day mean total",
                )
            )
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
        figures = [_figure_ref(s, by_stem[s]) for s in stems if s in by_stem]
        if figures:
            sections.append(Section(title, narrative, figures))
    if recovery:
        sections.append(
            Section(
                "Recovery & fitness markers",
                "Passive off-workout recovery markers, tracked separately from "
                "training load.",
                [_figure_ref(stem, rel) for stem, rel in recovery],
            )
        )
    return sections


def _race_forecast(
    conn: sqlite3.Connection, runs: Sequence[metrics.Run], db_path: Path
) -> forecast.RaceForecast | None:
    """Race projection from the active plan's race row, if a plan is present."""
    from datetime import date

    from runlog.plan import schedule

    plans_dir = db_path.parent / "plans"
    if not plans_dir.exists():
        return None
    active = schedule.find_active_plan(plans_dir)
    if active is None:
        return None
    sched = schedule.load_schedule(active)
    if sched.race is None or sched.race_distance_m is None:
        return None
    return forecast.race_forecast(
        conn, runs, sched.race.day, sched.race_distance_m, date.today()
    )


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


# Short display labels for the cross-lane response candidates.
_RESPONSE_LABELS = {
    "hrv_sdnn": "HRV",
    "resting_hr": "resting HR",
    "sleep_hours": "sleep",
    "hr_recovery_1min": "HR recovery",
}


def _collect_candidates(
    trend_series: Sequence[tuple[str, str, str, list[tuple[date, float]]]],
    responses: Sequence[response.MarkerResponse],
    readiness_corr: stats.CorrTest | None,
    life: lifestyle.LifestyleSummary,
) -> list[importance.Candidate]:
    """Assemble the full what-matters test family from computed results."""
    candidates: list[importance.Candidate] = []
    for label, lane, unit, series in trend_series:
        values = [v for _, v in series]
        candidates.append(
            importance.Candidate(
                kind="trend",
                label=label,
                lane=lane,
                unit=unit,
                trend=stats.trend_test(series),
                sd=statistics.pstdev(values) if len(values) > 1 else None,
            )
        )
    for resp in responses:
        name = _RESPONSE_LABELS.get(resp.metric, resp.metric)
        candidates.append(
            importance.Candidate(
                kind="correlation",
                label=f"Load -> next-day {name}",
                lane="cross",
                unit="",
                corr=resp.load_corr,
            )
        )
        candidates.append(
            importance.Candidate(
                kind="contrast",
                label=f"Hard vs rest: next-day {name}",
                lane="cross",
                unit="",
                group=resp.rest_vs_hard,
            )
        )
    candidates.append(
        importance.Candidate(
            kind="correlation",
            label="Readiness <-> run efficiency",
            lane="cross",
            unit="",
            corr=readiness_corr,
        )
    )
    for name, contrast in (
        ("Steps", life.steps_contrast),
        ("Sleep", life.sleep_contrast),
    ):
        if contrast is not None:
            candidates.append(
                importance.Candidate(
                    kind="contrast",
                    label=f"{name}: training vs rest days",
                    lane="cross",
                    unit="",
                    group=contrast.test,
                )
            )
    return candidates


def _stats_table(table: importance.SignificanceTable) -> report_model.StatsTable | None:
    """Preformatted view-model rows for the HTML what-matters table."""
    if not table.findings:
        return None
    return report_model.StatsTable(
        caption=f"{table.n_tests} tests, Benjamini-Hochberg FDR at q<{table.alpha:g}",
        rows=[
            report_model.StatsRow(
                label=finding.label,
                lane=finding.lane,
                effect=finding.effect,
                ci=finding.ci,
                p=stats.format_p(finding.p),
                q=f"{finding.q:.3f}",
                n=str(finding.n),
                significant=finding.significant,
            )
            for finding in table.findings
        ],
    )
