"""Plain-text terminal summary of the analysis (stdlib only)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from runlog.analyze.analytics import BestEffortProgression, PmcPoint, Trend
    from runlog.analyze.anomaly import AnomalyReport
    from runlog.analyze.metrics import (
        BestEffort,
        BucketPace,
        ConsistencySummary,
        RacePrediction,
        Summary,
        WeeklyVolume,
    )
    from runlog.analyze.physiology import IntensityDistribution

_MARKER_LABELS = {
    "vo2max": "VO2max",
    "resting_hr": "Resting HR",
    "hrv_sdnn": "HRV (SDNN)",
    "spo2": "SpO2",
    "sleep_hours": "Sleep (h)",
    "hr_recovery_1min": "HR rec 1min",
    "body_mass": "Body mass",
    "walking_asymmetry": "Walk asym",
}


def _pace(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}/km"


def _clock(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def build_summary_text(
    summary: Summary,
    weekly: Sequence[WeeklyVolume],
    buckets: Sequence[BucketPace],
    latest_markers: dict[str, tuple[date, float] | None],
    streak: tuple[int, int],
    records: Sequence[BestEffort] = (),
    predictions: Sequence[RacePrediction] = (),
    consistency: ConsistencySummary | None = None,
    recent_weeks: int = 6,
) -> str:
    """Render the overview, recent weeks, fastest efforts, and fitness markers."""
    lines: list[str] = ["Running summary", "=" * 40]
    span = (
        f"{summary.first_run} -> {summary.last_run}"
        if summary.first_run
        else "no dated runs"
    )
    lines += [
        f"Runs           {summary.run_count}",
        f"Total distance {summary.total_km:.1f} km",
        f"Date range     {span}",
        f"Longest run    {summary.longest_km:.2f} km",
        f"This week      {summary.this_week_km:.1f} km",
        f"This month     {summary.this_month_km:.1f} km",
        f"Week streak    {streak[0]} current / {streak[1]} longest",
    ]

    if consistency is not None:
        lines += [
            f"Runs / week    {consistency.runs_per_week}",
            f"Active days    {consistency.active_days} of {consistency.span_days}",
            f"Longest layoff {consistency.longest_layoff_days} days "
            f"(median gap {consistency.median_gap_days})",
        ]

    lines += ["", f"Last {recent_weeks} weeks", "-" * 40]
    for week in list(weekly)[-recent_weeks:]:
        bar = "#" * min(int(week.distance_km), 40)
        lines.append(
            f"{week.week_start}  {week.distance_km:5.1f} km  "
            f"({week.run_count} runs)  {bar}"
        )

    lines += ["", "Fastest pace by distance", "-" * 40]
    for bucket in buckets:
        lines.append(
            f"{bucket.label:<6} {_pace(bucket.fastest_pace_s_per_km):<9} "
            f"({bucket.count} runs)"
        )

    if records:
        lines += ["", "Records (best pace)", "-" * 40]
        for record in records:
            lines.append(
                f"{record.label:<6} {_pace(record.pace_s_per_km):<9} ({record.when})"
            )

    if predictions:
        lines += ["", "Predicted race times (Riegel)", "-" * 40]
        for prediction in predictions:
            lines.append(f"{prediction.label:<9} {_clock(prediction.seconds)}")

    lines += ["", "Recovery & fitness (passive, off-workout)", "-" * 40]
    for key, label in _MARKER_LABELS.items():
        latest = latest_markers.get(key)
        if latest is None:
            lines.append(f"{label:<12} -")
        else:
            when, value = latest
            lines.append(f"{label:<12} {value:.1f}  ({when})")

    return "\n".join(lines)


def analytics_section(
    pmc: Sequence[PmcPoint],
    acwr: Sequence[tuple[date, float]],
    ef_trend: Trend | None,
    best_efforts: Sequence[BestEffortProgression],
) -> str:
    """Render the high-level training-status block appended to the summary."""
    lines: list[str] = ["", "Training status (analytics)", "=" * 40]
    if pmc:
        last = pmc[-1]
        lines += [
            f"Fitness (CTL)  {last.fitness:.1f}",
            f"Fatigue (ATL)  {last.fatigue:.1f}",
            f"Form (TSB)     {last.form:+.1f}",
        ]
    if acwr:
        lines.append(f"ACWR           {acwr[-1][1]}  (sweet spot 0.8-1.3)")
    if ef_trend is not None:
        direction = "improving" if ef_trend.per_30_days >= 0 else "declining"
        lines.append(
            f"Efficiency     {ef_trend.per_30_days:+.3f}/month "
            f"({direction}, r={ef_trend.r:.2f})"
        )

    if best_efforts:
        lines += ["", "Best efforts (fastest continuous, from streams)", "-" * 40]
        for effort in best_efforts:
            best_seconds = min(seconds for _, seconds in effort.progression)
            lines.append(f"{effort.label:<4} {_clock(best_seconds)}")
    return "\n".join(lines)


_ANOMALY_LABELS = {
    "resting_hr": "Resting HR",
    "hrv_sdnn": "HRV",
    "spo2": "SpO2",
    "sleep_hours": "Sleep",
    "hr_recovery_1min": "HR recovery",
    "efficiency_factor": "Efficiency",
}


def anomaly_section(report: AnomalyReport, recent: int = 8) -> str:
    """Render the anomalies block: red-flag days plus recent off-readings."""
    lines: list[str] = ["", "Anomalies (rolling-baseline)", "=" * 40]
    if report.red_flag_days:
        lines += ["Readiness red-flag days (>=2 signals)", "-" * 40]
        for flag in report.red_flag_days[-recent:]:
            names = ", ".join(_ANOMALY_LABELS.get(m, m) for m in flag.metrics)
            lines.append(f"{flag.day}  {names}")
    else:
        lines.append("No readiness red-flag days.")

    if report.performance:
        lines += ["", "Off runs (slow for the effort)", "-" * 40]
        for anomaly in report.performance[-recent:]:
            lines.append(
                f"{anomaly.day}  efficiency {anomaly.value:.2f} "
                f"vs {anomaly.baseline:.2f} ({anomaly.deviation:+.1f} sigma)"
            )
    return "\n".join(lines)


def _intensity_line(label: str, dist: IntensityDistribution) -> str:
    return (
        f"{label:<14} easy {dist.easy_pct:.0f}% / mod {dist.moderate_pct:.0f}% / "
        f"hard {dist.hard_pct:.0f}%  (easy:hard {dist.easy_to_hard})"
    )


def physiology_section(
    intensity: IntensityDistribution | None,
    median_drift: float | None,
    pace_intensity: IntensityDistribution | None = None,
) -> str:
    """Render the stream-based physiology block (intensity splits + drift)."""
    lines: list[str] = ["", "Physiology (from HR/velocity streams)", "=" * 40]
    if intensity is not None:
        lines.append(_intensity_line("Intensity (HR)", intensity))
    if pace_intensity is not None:
        lines.append(_intensity_line("Intensity (pace)", pace_intensity))
    if median_drift is not None:
        lines.append(f"Cardiac drift  {median_drift:+.1f}% median (>0 = drift up)")
    return "\n".join(lines)
