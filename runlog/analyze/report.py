"""Orchestrate the analysis: read the DB, compute metrics, write charts + text.

Read-only. Training analysis covers running workouts only (walking, strength,
cycling, etc. are excluded by the sport filter). Passive off-workout health
metrics (VO2max, resting HR, HRV) are rendered separately under ``recovery/``
and never mixed into the training charts or totals.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
from runlog.analyze.report_model import (
    FigureRef,
    Kpi,
    ReportModel,
    Section,
    TodayPlan,
)
from runlog.analyze.series import Scalar, from_trends
from runlog.db import queries, store

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import date
    from pathlib import Path

    from runlog.analyze.forecast import RaceForecast
    from runlog.config import Athlete, Sex
    from runlog.plan.schedule import PlanSchedule


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
        "Pace distribution by detected session type; structured sessions "
        "(intervals, threshold, tempo) use their work-rep pace, not the whole run.",
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
        "Ensemble projections (Riegel, fitted fatigue exponent, critical speed) "
        "from current best efforts.",
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
    schedule: PlanSchedule | None = None,
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
            schedule=schedule,
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
    schedule: PlanSchedule | None,
    save: bool,
) -> ReportResult:
    runs = metrics.canonical_run_activities(
        conn, sports, since=since, min_distance_km=min_distance_km
    )
    weekly = metrics.weekly_volume(runs)
    efforts = analytics.best_effort_records(conn, runs)
    pace = metrics.pace_points(runs)
    predictions = _ensemble_race_predictions(conn, runs)

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
    marker_levels: dict[str, tuple[date, float] | None] = {}
    marker_deltas: dict[str, float | None] = {}
    for metric, (title, ylabel) in _RECOVERY_MARKERS.items():
        series = metrics.metric_series(conn, metric, since=since)
        latest_markers[metric] = (
            (series[-1][0].date(), series[-1][1]) if series else None
        )
        daily = metrics.daily_means(series)
        marker_levels[metric] = _marker_level(daily)
        marker_deltas[metric] = _marker_delta(daily)
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
            marker_levels,
            cs_model,
            readiness_latest,
            life,
            ef_test,
            energy_summary,
            marker_deltas,
            vdot_points,
            cs_series[-1][1] if cs_series else None,
        ),
        sections=_build_sections(produced, out_dir),
        stats=_stats_table(table),
        activities=activity_rows,
        summary=summary.training_status(
            overall,
            pmc,
            weekly,
            intensity,
            acwr,
            readiness_latest,
            anomalies,
            table,
        ),
        today=_today_plan(
            conn,
            schedule,
            datetime.now().date(),
            hr_max=hr_max_value,
            hr_rest=hr_rest_value,
            sex=sex,
        ),
        provenance=_hr_provenance(hr_max, hr_rest, hr_max_value, hr_rest_value),
        coverage=_coverage(conn, runs, quarantined, cost_source),
    )
    report_html = html_report.render(model, out_dir)
    return ReportResult(
        charts=produced,
        summary_text=text,
        report_html=report_html,
        activity_pages=activity_pages,
        data_exports=data_exports,
    )


def _hr_provenance(
    hr_max: float | None,
    hr_rest: float | None,
    hr_max_value: float,
    hr_rest_value: float,
) -> str:
    """State the HR anchors the analytics ran on and where each came from."""
    max_src = "configured" if hr_max is not None else "estimated from runs"
    rest_src = "athlete.toml" if hr_rest is not None else "measured median"
    return (
        f"HR model: max {hr_max_value:.0f} bpm ({max_src}), "
        f"rest {hr_rest_value:.0f} bpm ({rest_src}) — the Karvonen reserve behind "
        f"TRIMP load, HR zones and intensity. The Resting HR card below is passive "
        f"Apple Watch data, tracked separately."
    )


def _ensemble_race_predictions(
    conn: sqlite3.Connection, runs: Sequence[metrics.Run]
) -> list[metrics.RacePrediction]:
    """Race-time projections from the individualized ensemble (Riegel + the
    athlete's fitted fatigue exponent + critical speed), over best efforts, rather
    than a single-effort Riegel scaling."""
    efforts = [
        (e.distance_m, e.seconds) for e in analytics.best_effort_records(conn, runs)
    ]
    if not efforts:
        return []
    model = cs.critical_speed(conn, runs)
    predictions: list[metrics.RacePrediction] = []
    for label, dist_km in (
        ("5k", 5.0),
        ("10k", 10.0),
        ("Half", 21.0975),
        ("Marathon", 42.195),
    ):
        pred = race.predict(efforts, dist_km * 1000, model)
        if pred is not None:
            predictions.append(
                metrics.RacePrediction(
                    label=label, distance_km=dist_km, seconds=pred.seconds
                )
            )
    return predictions


def _race_outlook(fc: RaceForecast | None, today: date) -> str:
    """One line: distance, race day, days out, projected time and CI band."""
    if fc is None:
        return ""
    km = fc.distance_m / 1000
    days = (fc.race_day - today).days
    band = ""
    if fc.ci_low_s is not None and fc.ci_high_s is not None:
        band = (
            f" (95% CI {forecast.format_race_time(fc.ci_low_s)}"
            f"–{forecast.format_race_time(fc.ci_high_s)})"
        )
    return (
        f"{km:.0f} km on {fc.race_day} ({days} d): "
        f"{forecast.format_race_time(fc.predicted_s)}{band}"
    )


def _today_plan(
    conn: sqlite3.Connection,
    schedule: PlanSchedule | None,
    today: date,
    *,
    hr_max: float,
    hr_rest: float,
    sex: Sex | None,
) -> TodayPlan:
    """Surface the coach's deterministic go/easy/rest call, next session, race."""
    from runlog.coach import daily

    card = daily.build_today(
        conn, schedule, today, hr_max=hr_max, hr_rest=hr_rest, sex=sex
    )
    session = card.session
    session_text = ""
    if session is not None and session.is_rest:
        session_text = "rest day"
    elif session is not None:
        session_text = f"{session.kind} — {session.description}".strip(" —")
    return TodayPlan(
        action=card.guidance.action,
        reason=card.guidance.reasons[0],
        session=session_text,
        race=_race_outlook(card.forecast, today),
    )


def _coverage(
    conn: sqlite3.Connection,
    runs: Sequence[metrics.Run],
    quarantined: int,
    calorie_source: str | None,
) -> list[tuple[str, str]]:
    """What the report actually ran on, so a reduced sample is never hidden."""
    total = len(runs)
    if not total:
        return []
    by_source = Counter(run.source for run in runs)
    with_stream = sum(
        1
        for run in runs
        if conn.execute(
            "SELECT 1 FROM stream_points WHERE activity_id = ? LIMIT 1",
            (int(queries.stream_bearing_id(conn, run.activity_id)),),
        ).fetchone()
    )
    runs_label = f"{total}" + (f" · {quarantined} quarantined" if quarantined else "")
    rows = [
        ("Runs analysed", runs_label),
        ("Sources", " · ".join(f"{src} {n}" for src, n in sorted(by_source.items()))),
        ("Streams", f"{with_stream}/{total} with a usable trace"),
        ("Heart rate", f"{sum(1 for r in runs if r.avg_hr)}/{total}"),
        ("Cadence", f"{sum(1 for r in runs if r.avg_cadence)}/{total}"),
    ]
    if calorie_source:
        rows.append(("Calories", f"pinned to {calorie_source}"))
    return rows


def _series_delta(dated: Sequence[tuple[date, float]], days: int = 28) -> float | None:
    """Change from ~``days`` ago to the latest point of a date-keyed series."""
    if len(dated) < 2:
        return None
    latest_day, latest_val = dated[-1]
    cutoff = latest_day - timedelta(days=days)
    past = [v for d, v in dated if d <= cutoff]
    return latest_val - (past[-1] if past else dated[0][1])


def _marker_level(
    daily: Sequence[tuple[date, float]], window: int = 7
) -> tuple[date, float] | None:
    """Robust recent level: the latest date with the median of its last ``window`` days.

    A single day's resting HR or VO2max swings with sensor noise, so the card
    shows a short-window median rather than one possibly-outlying reading.
    """
    if not daily:
        return None
    return daily[-1][0], statistics.median([v for _, v in daily[-window:]])


def _marker_delta(
    daily: Sequence[tuple[date, float]], window: int = 7, lag: int = 28
) -> float | None:
    """Change in the ``window``-day median level versus ~``lag`` days earlier."""
    if len(daily) < 2:
        return None
    recent = statistics.median([v for _, v in daily[-window:]])
    cutoff = daily[-1][0] - timedelta(days=lag)
    prior = [v for d, v in daily if d <= cutoff]
    if not prior:
        return None
    return recent - statistics.median(prior[-window:])


def _kpi_delta(
    current: float, reference: float | None, *, pct: bool, up_good: bool
) -> tuple[str, str]:
    """(display, colour) for a change vs a reference; colour is good/bad/flat.

    The arrow sits in the display text; ``delta_dir`` carries the colour so the
    template need not know whether up is good for each metric.
    """
    if reference is None or (pct and reference == 0):
        return "", ""
    change = current - reference
    magnitude = change / abs(reference) * 100 if pct else change
    if abs(magnitude) < (8 if pct else 1):
        return "steady", "flat"
    arrow = "▲" if change > 0 else "▼"
    text = (
        f"{arrow} {abs(magnitude):.0f}% vs 4-wk avg"
        if pct
        else f"{arrow} {abs(change):.0f} in 4 wk"
    )
    return text, ("good" if (change > 0) == up_good else "bad")


def _signed(value: float, places: int = 3) -> str:
    """Signed fixed-point without a '-0.000' artefact from rounding."""
    rounded = round(value, places) or 0.0
    return f"{rounded:+.{places}f}"


def _efficiency_tone(per_30_days: float, test: stats.TrendTest | None) -> str:
    """Tone the efficiency trend only when it is significant; else neutral.

    A positive slope whose CI straddles zero is noise, not improvement, so it
    must not read as green.
    """
    if test is None or test.p >= 0.05:
        return "neutral"
    return "good" if per_30_days >= 0 else "warn"


def _vdot_race_context(model: cs.CsModel | None) -> str:
    """5k/10k times from the same CS model the VDOT is derived from.

    The headline VDOT is ``vdot(5k from critical speed)``, so the predicted
    times must come from that model too; mixing in a Riegel estimate would print
    a VDOT and times that describe different fitness.
    """
    if model is None:
        return "Daniels VDOT from critical speed"
    parts = [
        f"{label} {forecast.format_race_time(secs)}"
        for label, dist in (("5k", 5000.0), ("10k", 10000.0))
        if (secs := model.predict_seconds(dist)) is not None
    ]
    return " · ".join(parts) or "Daniels VDOT from critical speed"


def _build_kpis(
    overall: metrics.Summary,
    pmc: Sequence[analytics.PmcPoint],
    acwr: Sequence[tuple[date, float]],
    ef_trend: analytics.Trend | None,
    intensity: physiology.IntensityDistribution | None,
    marker_levels: dict[str, tuple[date, float] | None],
    cs_model: cs.CsModel | None,
    readiness_latest: readiness.ReadinessDay | None,
    life: lifestyle.LifestyleSummary | None = None,
    ef_test: stats.TrendTest | None = None,
    energy_summary: energy.EnergySummary | None = None,
    marker_deltas: dict[str, float | None] | None = None,
    vdot_points: Sequence[tuple[date, float]] = (),
    vdot_model: cs.CsModel | None = None,
) -> list[Kpi]:
    deltas = marker_deltas or {}
    kpis = [
        Kpi(
            "Total distance",
            f"{overall.total_km:.0f}",
            "km",
            f"{overall.run_count} runs",
        ),
        # No delta on the partial current week (it always reads "down" mid-week);
        # its week-over-week trend is narrated in the training-status summary. A
        # bare 0 reads as broken, so name the gap instead.
        Kpi(
            "This week",
            f"{overall.this_week_km:.0f}",
            "km",
            (
                f"no runs since {overall.last_run}"
                if overall.this_week_km == 0
                else "current training week"
            ),
        ),
    ]
    if pmc:
        last = pmc[-1]
        ctl_ref = pmc[-28].fitness if len(pmc) > 28 else None
        ctl_text, ctl_dir = _kpi_delta(last.fitness, ctl_ref, pct=False, up_good=True)
        kpis.append(
            Kpi(
                "Fitness (CTL)",
                f"{last.fitness:.0f}",
                context=f"form {last.form:+.0f} TSB",
                tone=report_model.form_tone(last.form),
                delta=ctl_text,
                delta_dir=ctl_dir,
            )
        )
    if vdot_points:
        latest_vdot = vdot_points[-1][1]
        vd = _series_delta(vdot_points)
        v_text, v_dir = _kpi_delta(
            latest_vdot,
            latest_vdot - vd if vd is not None else None,
            pct=False,
            up_good=True,
        )
        kpis.append(
            Kpi(
                "Race fitness",
                f"{latest_vdot:.1f}",
                "VDOT",
                _vdot_race_context(vdot_model),
                delta=v_text,
                delta_dir=v_dir,
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
                f"{intensity.moderate_pct:.0f}% moderate; ~80% easy ideal",
                tone,
            )
        )
    if ef_trend is not None:
        if ef_test is not None:
            low, high = ef_test.ci_30d
            context = (
                f"[{_signed(low)}, {_signed(high)}]/mo, {stats.format_p(ef_test.p)}"
            )
        else:
            context = f"r={ef_trend.r:.2f}"
        kpis.append(
            Kpi(
                "Efficiency",
                _signed(ef_trend.per_30_days),
                "/mo",
                context,
                _efficiency_tone(ef_trend.per_30_days, ef_test),
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
    markers = (
        ("vo2max", "VO2max", "ml/kg/min", True),
        ("resting_hr", "Resting HR", "bpm", False),
    )
    for key, label, unit, up_good in markers:
        marker = marker_levels.get(key)
        if marker is not None:
            md = deltas.get(key)
            text, color = _kpi_delta(
                marker[1],
                marker[1] - md if md is not None else None,
                pct=False,
                up_good=up_good,
            )
            marker_tone = color if color in ("good", "bad") else "neutral"
            kpis.append(
                Kpi(
                    label,
                    f"{marker[1]:.0f}",
                    unit,
                    f"7-day median to {marker[0]}",
                    tone=marker_tone,
                    delta=text,
                    delta_dir=color,
                )
            )
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
            Kpi("Resting (BMR)", f"{energy_summary.bmr:,.0f}", "kcal/day", method)
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
