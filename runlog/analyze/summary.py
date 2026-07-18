"""Plain-text terminal summary of the analysis (stdlib only)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from runlog.analyze import stats

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from runlog.analyze.analytics import (
        BestEffortProgression,
        EffortRecord,
        PmcPoint,
        Trend,
    )
    from runlog.analyze.anomaly import AnomalyReport
    from runlog.analyze.cs import CsModel
    from runlog.analyze.energy import EnergySummary
    from runlog.analyze.forecast import RaceForecast
    from runlog.analyze.importance import SignificanceTable
    from runlog.analyze.lifestyle import LifestyleSummary
    from runlog.analyze.metrics import (
        ConsistencySummary,
        RacePrediction,
        SportMix,
        Summary,
        WeeklyVolume,
    )
    from runlog.analyze.physiology import IntensityDistribution
    from runlog.analyze.readiness import ReadinessDay
    from runlog.analyze.records import RecordEvent
    from runlog.analyze.response import MarkerResponse
    from runlog.analyze.stats import CorrTest

_MARKER_LABELS = {
    "vo2max": "VO2max",
    "resting_hr": "Resting HR",
    "hrv_sdnn": "HRV (SDNN)",
    "spo2": "SpO2",
    "sleep_hours": "Sleep (h)",
    "hr_recovery_1min": "HR rec 1min",
    "body_mass": "Body mass",
    "walking_asymmetry": "Walk asym",
    "walking_hr_avg": "Walking HR",
    "respiratory_rate": "Resp rate",
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
    efforts: Sequence[EffortRecord],
    latest_markers: dict[str, tuple[date, float] | None],
    streak: tuple[int, int],
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

    if efforts:
        lines += ["", "Fastest pace by distance (continuous best)", "-" * 40]
        for effort in efforts:
            lines.append(
                f"{effort.label:<6} {_pace(effort.pace_s_per_km):<9} "
                f"({_clock(effort.seconds)}, {effort.when})"
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


_RECORD_ORDER = ("1k", "5k", "10k", "longest_run", "biggest_week")
_RECORD_LABELS = {
    "1k": "1k",
    "5k": "5k",
    "10k": "10k",
    "longest_run": "Longest run",
    "biggest_week": "Biggest week",
}


def records_section(
    events: Sequence[RecordEvent], forecast: RaceForecast | None
) -> str:
    """Standing records, recent PRs, and the race forecast line."""
    from runlog.analyze import records as records_mod
    from runlog.analyze.forecast import format_race_time

    if not events:
        return ""
    lines: list[str] = ["", "Records & racing", "=" * 40]
    current = records_mod.current_records(events, "all_time")
    for kind in _RECORD_ORDER:
        event = current.get(kind)
        if event is not None:
            value = (
                event.label.split(" ", 1)[-1]
                if kind in ("1k", "5k", "10k")
                else f"{event.value:.1f} km"
            )
            lines.append(f"{_RECORD_LABELS[kind]:<13} {value}  ({event.day})")
    if forecast is not None:
        km = forecast.distance_m / 1000
        if forecast.ci_low_s is not None and forecast.ci_high_s is not None:
            band = (
                f"  (95% CI {format_race_time(forecast.ci_low_s)}-"
                f"{format_race_time(forecast.ci_high_s)}, {forecast.method} "
                f"n={forecast.n_weeks})"
            )
        else:
            band = f"  ({forecast.method})"
        lines.append(
            f"\nRace forecast  {km:.0f} km on {forecast.race_day}: "
            f"{format_race_time(forecast.predicted_s)}{band}"
        )
    return "\n".join(lines)


def importance_section(table: SignificanceTable, top: int = 12) -> str:
    """Render the ranked what-matters findings; empty when nothing scored."""
    if not table.findings:
        return ""
    lines: list[str] = [
        "",
        "What matters (FDR-corrected)",
        "=" * 40,
        f"{table.n_tests} tests, BH-FDR at q<{table.alpha:g}",
        "-" * 40,
    ]
    for finding in table.findings[:top]:
        star = "*" if finding.significant else " "
        lines.append(
            f"{star} {finding.label:<24} {finding.lane:<9} "
            f"{finding.effect} {finding.ci}  "
            f"{stats.format_p(finding.p)} q={finding.q:.3f} n={finding.n}"
        )
    remaining = len(table.findings) - top
    if remaining > 0:
        lines.append(f"  ... {remaining} more below threshold")
    return "\n".join(lines)


def training_mix_section(
    mix: Sequence[SportMix], strength_weeks: tuple[int, int], recent_weeks: int = 12
) -> str:
    """Render hours/sessions per sport plus strength consistency and balance."""
    if not mix:
        return ""
    lines: list[str] = ["", "Training mix (all sports)", "=" * 40]
    for sport in mix:
        lines.append(
            f"{sport.label:<9} {sport.total_hours:6.1f} h all-time / "
            f"{sport.recent_hours:5.1f} h last {recent_weeks}w  "
            f"({sport.sessions} sessions)"
        )
    active, considered = strength_weeks
    if considered:
        lines.append(f"Strength weeks {active} of last {considered}")
    hours = {sport.label: sport.recent_hours for sport in mix}
    run_h, strength_h = hours.get("Run", 0.0), hours.get("Strength", 0.0)
    ratio = f"{run_h / strength_h:.1f} : 1" if strength_h > 0 else "-"
    lines.append(f"Run:strength   {ratio} (hours, last {recent_weeks}w)")
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


_RESPONSE_LABELS = {
    "hrv_sdnn": "HRV",
    "resting_hr": "Resting HR",
    "sleep_hours": "Sleep",
    "hr_recovery_1min": "HR recovery",
}


def _bucket_z(response: MarkerResponse, label: str) -> str:
    stat = next(b for b in response.buckets if b.label == label)
    return f"{stat.mean_z:+.2f}" if stat.mean_z is not None else "-"


def response_section(responses: Sequence[MarkerResponse]) -> str:
    """Render next-day recovery response to training load; empty if unscored."""
    if not responses:
        return ""
    lines: list[str] = ["", "Load -> recovery (next-day, all-sport TRIMP)", "=" * 40]
    for response in responses:
        r = f"{response.pearson_r:+.2f}" if response.pearson_r is not None else "-"
        inference = ""
        if response.rest_vs_hard is not None:
            test = response.rest_vs_hard
            inference = (
                f"  g={test.hedges_g:+.2f} [{test.ci_low:+.2f},{test.ci_high:+.2f}] "
                f"{stats.format_p(test.p)}"
            )
        lines.append(
            f"{_RESPONSE_LABELS.get(response.metric, response.metric):<12} "
            f"hard {_bucket_z(response, 'hard')} vs rest "
            f"{_bucket_z(response, 'rest')} sigma"
            f"{inference}  (r={r}, n={response.n_pairs})"
        )
    return "\n".join(lines)


def lifestyle_section(lifestyle: LifestyleSummary) -> str:
    """Render passive daily patterns; empty when nothing is populated."""
    fields = (
        lifestyle.steps_30d,
        lifestyle.sleep_30d,
        lifestyle.weekend_sleep_shift_h,
        lifestyle.steps_contrast,
    )
    if all(field is None for field in fields):
        return ""
    lines: list[str] = ["", "Lifestyle (passive daily patterns)", "=" * 40]
    if lifestyle.steps_30d is not None:
        lines.append(f"Steps (30d)    {lifestyle.steps_30d:,.0f} /day")
    if lifestyle.sleep_30d is not None:
        sd = (
            f" (±{lifestyle.sleep_sd_30d:.1f} h night-to-night)"
            if lifestyle.sleep_sd_30d is not None
            else ""
        )
        lines.append(f"Sleep (30d)    {lifestyle.sleep_30d:.1f} h{sd}")
    if lifestyle.weekend_sleep_shift_h is not None:
        lines.append(
            f"Weekend sleep  {lifestyle.weekend_sleep_shift_h:+.1f} h vs weekdays"
        )
    if lifestyle.steps_contrast is not None:
        contrast = lifestyle.steps_contrast
        inference = (
            f", g={contrast.test.hedges_g:+.2f} {stats.format_p(contrast.test.p)}"
            if contrast.test is not None
            else ""
        )
        lines.append(
            f"Steps          {contrast.training_mean:,.0f} training days vs "
            f"{contrast.rest_mean:,.0f} rest "
            f"(n={contrast.training_n}/{contrast.rest_n}{inference})"
        )
    return "\n".join(lines)


_METHOD_LABELS = {
    "mifflin": "Mifflin-St Jeor estimate",
    "measured-basal": "measured basal energy",
}


def energy_section(
    energy: EnergySummary | None, cost_trend: stats.TrendTest | None = None
) -> str:
    """Render daily energy expenditure; empty when it can't be estimated."""
    if energy is None:
        return ""
    method = _METHOD_LABELS.get(energy.method, energy.method)
    lines: list[str] = ["", "Energy expenditure", "=" * 40]
    lines.append(f"Resting (BMR)  {energy.bmr_latest:,.0f} kcal/day ({method})")
    if energy.active_30d is not None:
        lines.append(f"Active (30d)   {energy.active_30d:,.0f} kcal/day")
    if energy.tdee_30d is not None:
        lines.append(f"Total (30d)    {energy.tdee_30d:,.0f} kcal/day")
    if energy.weight_latest is not None:
        lines.append(f"Body mass      {energy.weight_latest:.1f} kg")
    if energy.tdee_trend is not None:
        trend = energy.tdee_trend
        lines.append(
            f"Total trend    {trend.per_30_days:+,.0f} kcal/30d "
            f"({stats.format_p(trend.p)}, n={trend.n})"
        )
    if energy.tdee_contrast is not None:
        contrast = energy.tdee_contrast
        inference = (
            f", g={contrast.test.hedges_g:+.2f} {stats.format_p(contrast.test.p)}"
            if contrast.test is not None
            else ""
        )
        lines.append(
            f"Total          {contrast.training_mean:,.0f} training days vs "
            f"{contrast.rest_mean:,.0f} rest "
            f"(n={contrast.training_n}/{contrast.rest_n}{inference})"
        )
    if cost_trend is not None:
        lines.append(
            f"Energy cost    {cost_trend.per_30_days:+.1f} kcal/km per 30d "
            f"({stats.format_p(cost_trend.p)}, n={cost_trend.n})"
        )
    return "\n".join(lines)


_CS_PREDICTIONS: tuple[tuple[str, float], ...] = (
    ("3k", 3000.0),
    ("5k", 5000.0),
    ("10k", 10000.0),
)


def advanced_section(
    cs_model: CsModel | None,
    readiness_latest: ReadinessDay | None,
    readiness_corr: CorrTest | None,
) -> str:
    """Render the Critical Speed model and readiness score (empty if neither)."""
    if cs_model is None and readiness_latest is None:
        return ""
    lines: list[str] = ["", "Advanced models", "=" * 40]
    if cs_model is not None:
        lines.append(
            f"Critical speed {cs_model.cs_mps:.2f} m/s  "
            f"(D' {cs_model.d_prime_m:.0f} m, r={cs_model.r:.2f})"
        )
        for label, meters in _CS_PREDICTIONS:
            seconds = cs_model.predict_seconds(meters)
            if seconds is not None:
                lines.append(f"  CS {label:<4} {_clock(seconds)}")
    if readiness_latest is not None:
        lines.append(
            f"Readiness      {readiness_latest.score:.0f}/100  "
            f"({readiness_latest.day}, 40-60 normal)"
        )
    if readiness_corr is not None:
        lines.append(
            f"Readiness vs performance  r={readiness_corr.r:+.2f} "
            f"[{readiness_corr.ci_low:+.2f}, {readiness_corr.ci_high:+.2f}] "
            f"{stats.format_p(readiness_corr.p)}, n={readiness_corr.n} "
            f"(explains ~{round(readiness_corr.r**2 * 100)}% of off-day variance)"
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
