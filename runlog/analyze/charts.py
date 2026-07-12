"""Matplotlib chart rendering for the analysis report.

Each function consumes the plain dataclasses produced by :mod:`runlog.analyze`
and writes one publication-quality PNG, returning its path. The shared look and
annotation primitives live in :mod:`runlog.analyze.style`; this module only
composes them per chart. matplotlib is loosely typed, so the plotting calls use
``Any`` axes — all real computation lives upstream in the metric modules.
"""

from __future__ import annotations

import math
import statistics
from typing import TYPE_CHECKING, Any

from matplotlib.ticker import FuncFormatter

from runlog.analyze import style
from runlog.analyze.style import (
    ACCENT,
    BAD,
    GOOD,
    MUTED,
    PRIMARY,
    SUBTLE,
    WARN,
    ZONE_COLORS,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime
    from pathlib import Path

    from runlog.analyze.analytics import BestEffortProgression, PmcPoint, Trend
    from runlog.analyze.anomaly import AnomalyReport
    from runlog.analyze.cs import CsModel
    from runlog.analyze.metrics import (
        BucketPace,
        Heatmap,
        HrPoint,
        HrZone,
        PacePoint,
        RacePrediction,
        WeeklyLoad,
        WeeklySportHours,
        WeeklyVolume,
    )
    from runlog.analyze.physiology import IntensityDistribution
    from runlog.analyze.readiness import ReadinessDay

# Y-axis rows (bottom to top) for the anomaly timeline, with display labels.
_ANOMALY_ROWS = (
    ("efficiency_factor", "Efficiency"),
    ("hr_recovery_1min", "HR recovery"),
    ("sleep_hours", "Sleep"),
    ("spo2", "SpO2"),
    ("hrv_sdnn", "HRV"),
    ("resting_hr", "Resting HR"),
)

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D")
_ROLLING_WINDOW = 5
# Break rolling-median lines across stretches with no runs, instead of drawing
# a flat segment that implies data where there is none.
_MAX_LINE_GAP_DAYS = 30


def _format_pace(seconds: float, _pos: Any = None) -> str:
    """Render a pace in seconds/km as ``M:SS``."""
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}"


def _format_clock(seconds: float) -> str:
    """Render a duration as ``H:MM:SS`` (or ``M:SS`` under an hour)."""
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _pace_axis(ax: Any) -> None:
    """Format a pace axis as M:SS and invert it so faster is up."""
    ax.yaxis.set_major_formatter(FuncFormatter(_format_pace))
    ax.invert_yaxis()


def _rolling_median(values: Sequence[float]) -> list[float]:
    return [
        statistics.median(values[max(0, i - _ROLLING_WINDOW + 1) : i + 1])
        for i in range(len(values))
    ]


def _gap_broken(
    days: Sequence[Any], values: Sequence[float], max_gap_days: int = _MAX_LINE_GAP_DAYS
) -> tuple[list[Any], list[float]]:
    """(x, y) with a NaN inserted wherever consecutive points span a long gap.

    matplotlib breaks a line at NaN, so a rolling median or weekly line drawn
    over irregular dates stops bridging months without data with a flat,
    misleading segment. All real points are kept.
    """
    xs: list[Any] = []
    ys: list[float] = []
    for i, (day, value) in enumerate(zip(days, values, strict=True)):
        gap = day - days[i - 1] if i else None
        if gap is not None and getattr(gap, "days", 0) > max_gap_days:
            xs.append(day)
            ys.append(math.nan)
        xs.append(day)
        ys.append(value)
    return xs, ys


def _rolling_mean(values: Sequence[float], window: int) -> list[float]:
    return [
        statistics.fmean(values[max(0, i - window + 1) : i + 1])
        for i in range(len(values))
    ]


def _count_note(n: int, unit: str = "runs") -> str:
    return f"n = {n} {unit}"


# --- Volume & consistency ---------------------------------------------------


def weekly_volume_chart(weekly: Sequence[WeeklyVolume], out_dir: Path) -> Path:
    fig, ax = style.figure(
        "Weekly volume",
        "Weekly kilometres with a 4-week rolling mean",
        "Week",
        "Distance (km)",
    )
    weeks = [w.week_start for w in weekly]
    ax.bar(weeks, [w.distance_km for w in weekly], width=6, color=PRIMARY, alpha=0.55)
    if weekly:
        ax.plot(
            weeks,
            [w.rolling_km for w in weekly],
            color=ACCENT,
            linewidth=2.2,
            label="4-week rolling mean",
        )
        latest = weekly[-1]
        style.latest_callout(
            ax, latest.week_start, latest.distance_km, f"{latest.distance_km:.0f} km"
        )
        ax.legend()
        style.footnote(fig, _count_note(sum(w.run_count for w in weekly)))
    style.date_axis(ax)
    return style.save(fig, out_dir, "weekly_volume.png")


def monthly_by_year_chart(by_year: dict[int, list[float]], out_dir: Path) -> Path:
    fig, ax = style.figure(
        "Monthly distance by year",
        "Kilometres per calendar month, grouped by year",
        "Month",
        "Distance (km)",
    )
    years = sorted(by_year)
    width = 0.8 / max(len(years), 1)
    for i, year in enumerate(years):
        offset = (i - (len(years) - 1) / 2) * width
        ax.bar(
            [m + offset for m in range(1, 13)],
            by_year[year],
            width=width,
            label=str(year),
        )
    ax.set_xticks(range(1, 13))
    ax.set_xticklabels(_MONTHS)
    if years:
        ax.legend(title="Year")
    return style.save(fig, out_dir, "monthly_by_year.png")


def distance_histogram(distances: Sequence[float], out_dir: Path) -> Path:
    fig, ax = style.figure(
        "Run distance distribution",
        "How far a typical run goes",
        "Distance (km)",
        "Runs",
    )
    if distances:
        ax.hist(list(distances), bins=20, color=PRIMARY, alpha=0.75)
        median = statistics.median(distances)
        ax.axvline(median, color=ACCENT, lw=2, label=f"median {median:.1f} km")
        ax.legend()
        style.footnote(fig, _count_note(len(distances)))
    return style.save(fig, out_dir, "distance_histogram.png")


def training_heatmap_chart(heatmap: Heatmap, out_dir: Path) -> Path:
    fig, ax = style.figure(
        "Training heatmap", "Distance by weekday over the weeks", "Week", "Weekday"
    )
    if heatmap.week_starts:
        mesh = ax.pcolormesh(heatmap.matrix, cmap="YlGnBu")
        fig.colorbar(mesh, ax=ax, label="Distance (km)")
    ax.set_yticks([i + 0.5 for i in range(7)])
    ax.set_yticklabels(_WEEKDAYS)
    ax.invert_yaxis()
    ax.grid(visible=False)
    return style.save(fig, out_dir, "training_heatmap.png")


def runs_per_week_chart(weekly: Sequence[WeeklyVolume], out_dir: Path) -> Path:
    fig, ax = style.figure(
        "Runs per week", "Session count per week (consistency)", "Week", "Runs"
    )
    ax.bar(
        [w.week_start for w in weekly],
        [w.run_count for w in weekly],
        width=6,
        color=PRIMARY,
        alpha=0.75,
    )
    style.date_axis(ax)
    return style.save(fig, out_dir, "runs_per_week.png")


def cumulative_ytd_chart(
    by_year: dict[int, list[tuple[int, float]]], out_dir: Path
) -> Path:
    fig, ax = style.figure(
        "Cumulative distance (year to date)",
        "Kilometres accumulated through each year",
        "Day of year",
        "Distance (km)",
    )
    for year in sorted(by_year):
        curve = by_year[year]
        (line,) = ax.plot(
            [day for day, _ in curve],
            [km for _, km in curve],
            linewidth=2,
            label=str(year),
        )
        if curve:
            style.latest_callout(
                ax, curve[-1][0], curve[-1][1], f"{curve[-1][1]:.0f}", line.get_color()
            )
    if by_year:
        ax.legend(title="Year")
    return style.save(fig, out_dir, "cumulative_distance.png")


# --- Pace -------------------------------------------------------------------


def _pace_scatter(
    ax: Any, days: Sequence[Any], paces: Sequence[float], label: str = "Runs"
) -> None:
    ax.scatter(days, paces, s=16, color=MUTED, alpha=0.6, label=label)
    ax.plot(
        *_gap_broken(days, _rolling_median(paces)),
        color=PRIMARY,
        linewidth=2.2,
        label="Rolling median",
    )


def pace_over_time_chart(points: Sequence[PacePoint], out_dir: Path) -> Path:
    fig, ax = style.figure(
        "Pace over time",
        "Average pace per run with a rolling median (lower is faster)",
        "Date",
        "Pace (min/km)",
    )
    ordered = sorted(points, key=lambda p: p.start)
    if ordered:
        starts = [p.start for p in ordered]
        paces = [p.pace_s_per_km for p in ordered]
        _pace_scatter(ax, starts, paces)
        ax.legend()
        style.footnote(fig, _count_note(len(ordered)))
    _pace_axis(ax)
    style.date_axis(ax)
    return style.save(fig, out_dir, "pace_over_time.png")


def grade_adjusted_pace_chart(points: Sequence[PacePoint], out_dir: Path) -> Path:
    """Grade-adjusted pace over time (flattens hilly runs for a fair trend)."""
    fig, ax = style.figure(
        "Grade-adjusted pace over time",
        "Pace normalized for elevation (Strava GAP) — a fairer fitness signal",
        "Date",
        "Pace (min/km)",
    )
    ordered = sorted(points, key=lambda p: p.start)
    if ordered:
        _pace_scatter(
            ax, [p.start for p in ordered], [p.pace_s_per_km for p in ordered]
        )
        ax.legend()
    _pace_axis(ax)
    style.date_axis(ax)
    return style.save(fig, out_dir, "grade_adjusted_pace.png")


def fastest_by_bucket_chart(buckets: Sequence[BucketPace], out_dir: Path) -> Path:
    fig, ax = style.figure(
        "Fastest pace by distance",
        "Best average pace achieved in each distance band",
        "Distance bucket",
        "Pace (min/km)",
    )
    values = [b.fastest_pace_s_per_km or 0.0 for b in buckets]
    bars = ax.bar([b.label for b in buckets], values, color=PRIMARY, alpha=0.8)
    for bucket, bar in zip(buckets, bars, strict=False):
        if bucket.fastest_pace_s_per_km:
            ax.annotate(
                _format_pace(bucket.fastest_pace_s_per_km),
                xy=(bar.get_x() + bar.get_width() / 2, bucket.fastest_pace_s_per_km),
                xytext=(0, 3),
                textcoords="offset points",
                ha="center",
                fontsize=9,
                fontweight="bold",
                color=SUBTLE,
            )
    # Format as M:SS but do NOT invert: on an inverted axis the bars hang from
    # the top and the fastest bucket reads as the longest bar.
    ax.yaxis.set_major_formatter(FuncFormatter(_format_pace))
    positive = [v for v in values if v]
    if positive:
        ax.set_ylim(min(positive) * 0.85, max(positive) * 1.08)
    return style.save(fig, out_dir, "fastest_by_bucket.png")


def race_prediction_chart(predictions: Sequence[RacePrediction], out_dir: Path) -> Path:
    fig, ax = style.figure(
        "Predicted race times (Riegel)",
        "Current-fitness projections from your best efforts",
        "Race",
        "Time (minutes)",
    )
    if predictions:
        bars = ax.bar(
            [p.label for p in predictions],
            [p.seconds / 60 for p in predictions],
            color=PRIMARY,
            alpha=0.8,
        )
        for prediction, bar in zip(predictions, bars, strict=False):
            ax.annotate(
                _format_clock(prediction.seconds),
                xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
                xytext=(0, 3),
                textcoords="offset points",
                ha="center",
                fontsize=9,
                fontweight="bold",
                color=SUBTLE,
            )
    return style.save(fig, out_dir, "race_predictions.png")


# --- Heart rate & effort ----------------------------------------------------


def hr_over_time_chart(points: Sequence[HrPoint], out_dir: Path) -> Path:
    """Per-run average HR as a scatter with a rolling median.

    A scatter (not connected lines): consecutive runs mix easy and hard
    sessions, so lines between them read as noise, and lines across sparse
    months imply data that is not there.
    """
    fig, ax = style.figure(
        "Heart rate over time",
        "Average HR per run (dots) with a rolling median",
        "Date",
        "Heart rate (bpm)",
    )
    ordered = sorted(points, key=lambda p: p.start)
    if ordered:
        starts = [p.start for p in ordered]
        averages = [p.avg_hr for p in ordered]
        ax.scatter(starts, averages, s=14, color=MUTED, alpha=0.45, label="Per run")
        ax.plot(
            *_gap_broken(starts, _rolling_median(averages)),
            color=PRIMARY,
            lw=2.2,
            label="Rolling median",
        )
        ax.legend()
    style.date_axis(ax)
    return style.save(fig, out_dir, "hr_over_time.png")


def hr_histogram(
    samples: Sequence[float], out_dir: Path, zones: Sequence[HrZone] = ()
) -> Path:
    fig, ax = style.figure(
        "Heart-rate distribution",
        "Time spent at each heart rate, with zone thresholds",
        "Heart rate (bpm)",
        "Samples (≈ seconds)",
    )
    if samples:
        ax.hist(list(samples), bins=30, color=PRIMARY, alpha=0.65)
    top = ax.get_ylim()[1]
    for zone in zones:
        ax.axvline(zone.low_bpm, color=SUBTLE, linestyle="--", linewidth=0.8)
        ax.text(zone.low_bpm, top, f" {zone.label}", color=SUBTLE, va="top", fontsize=8)
    return style.save(fig, out_dir, "hr_histogram.png")


def hr_zones_chart(zones: Sequence[HrZone], hr_max: float, out_dir: Path) -> Path:
    fig, ax = style.figure(
        "Time in heart-rate zones",
        f"Minutes per zone as a fraction of HRmax ≈ {hr_max:.0f} bpm",
        "Zone",
        "Time (minutes)",
    )
    bars = ax.bar(
        [f"{z.label}\n{z.bpm_range}" for z in zones],
        [z.minutes for z in zones],
        color=list(ZONE_COLORS[: len(zones)]),
    )
    style.bar_value_labels(ax, bars, "{:.0f}")
    return style.save(fig, out_dir, "hr_zones.png")


def training_load_chart(load: Sequence[WeeklyLoad], out_dir: Path) -> Path:
    fig, ax = style.figure(
        "Weekly training load",
        "HR-weighted load (minutes × intensity) per week",
        "Week",
        "Load (a.u.)",
    )
    if load:
        weeks = [w.week_start for w in load]
        ax.plot(
            *_gap_broken(weeks, [w.load for w in load]),
            color=PRIMARY,
            linewidth=2.2,
            marker="o",
            markersize=3,
        )
    style.date_axis(ax)
    return style.save(fig, out_dir, "training_load.png")


# --- Cadence, elevation, dynamics -------------------------------------------


def cadence_chart(points: Sequence[tuple[datetime, float]], out_dir: Path) -> Path:
    fig, ax = style.figure(
        "Cadence over time",
        "Average step rate per run with a rolling mean",
        "Date",
        "Cadence (steps/min)",
    )
    ordered = sorted(points, key=lambda p: p[0])
    if ordered:
        days = [d for d, _ in ordered]
        values = [v for _, v in ordered]
        ax.scatter(days, values, s=14, color=MUTED, alpha=0.5, label="Runs")
        ax.plot(
            *_gap_broken(days, _rolling_mean(values, 10)),
            color=PRIMARY,
            linewidth=2.2,
            label="Rolling mean",
        )
        ax.legend()
    style.date_axis(ax)
    return style.save(fig, out_dir, "cadence.png")


def elevation_by_month_chart(by_year: dict[int, list[float]], out_dir: Path) -> Path:
    fig, ax = style.figure(
        "Elevation gain by month",
        "Metres climbed per calendar month",
        "Month",
        "Elevation gain (m)",
    )
    years = sorted(by_year)
    width = 0.8 / max(len(years), 1)
    for i, year in enumerate(years):
        offset = (i - (len(years) - 1) / 2) * width
        ax.bar(
            [m + offset for m in range(1, 13)],
            by_year[year],
            width=width,
            label=str(year),
        )
    ax.set_xticks(range(1, 13))
    ax.set_xticklabels(_MONTHS)
    if years:
        ax.legend(title="Year")
    return style.save(fig, out_dir, "elevation_by_month.png")


def marker_chart(
    daily: Sequence[tuple[date, float]],
    title: str,
    ylabel: str,
    filename: str,
    out_dir: Path,
    trend_window: int = 14,
) -> Path:
    """Faint daily values with an OLS trend line, ±1σ ribbon, and a current call-out."""
    fig, ax = style.figure(title, "Daily values with a linear trend", "Date", ylabel)
    if daily:
        days = [d for d, _ in daily]
        values = [v for _, v in daily]
        ax.scatter(days, values, s=14, color=MUTED, alpha=0.4, label="Daily")
        style.trend_annotation(ax, list(daily))
        style.latest_callout(ax, days[-1], values[-1], style.sig_value(values[-1]))
        ax.legend(loc="lower left")
    style.date_axis(ax)
    return style.save(fig, out_dir, filename)


def sport_hours_chart(weekly: Sequence[WeeklySportHours], out_dir: Path) -> Path:
    """Weekly training hours stacked by sport (the all-sport view)."""
    fig, ax = style.figure(
        "Weekly training hours by sport",
        "All recorded workouts — running, strength, walking, cycling, rowing",
        "Week",
        "Hours",
    )
    totals: dict[str, float] = {}
    for week in weekly:
        for label, hours in week.hours_by_sport.items():
            totals[label] = totals.get(label, 0.0) + hours
    labels = sorted(totals, key=lambda lbl: totals[lbl], reverse=True)
    weeks = [w.week_start for w in weekly]
    bottoms = [0.0] * len(weekly)
    for i, label in enumerate(labels):
        heights = [w.hours_by_sport.get(label, 0.0) for w in weekly]
        ax.bar(
            weeks,
            heights,
            width=6,
            bottom=bottoms,
            color=style.PALETTE[i % len(style.PALETTE)],
            label=label,
        )
        bottoms = [b + h for b, h in zip(bottoms, heights, strict=True)]
    if labels:
        ax.legend(title="Sport")
    style.date_axis(ax)
    return style.save(fig, out_dir, "sport_hours.png")


# --- High-level analytics ---------------------------------------------------


def pmc_chart(points: Sequence[PmcPoint], out_dir: Path) -> Path:
    fig, ax = style.figure(
        "Fitness · Fatigue · Form",
        "Performance-management chart: Fitness (CTL), Fatigue (ATL), Form (TSB)",
        "Date",
        "Training load (CTL / ATL)",
    )
    if points:
        days = [p.day for p in points]
        ax.plot(
            days,
            [p.fitness for p in points],
            color=PRIMARY,
            lw=2.2,
            label="Fitness (CTL)",
        )
        ax.plot(
            days,
            [p.fatigue for p in points],
            color=ACCENT,
            lw=1.4,
            label="Fatigue (ATL)",
        )
        forms = [p.form for p in points]
        ax2 = ax.twinx()
        ax2.grid(visible=False)
        ax2.fill_between(
            days, forms, 0, where=[f >= 0 for f in forms], color=GOOD, alpha=0.20
        )
        ax2.fill_between(
            days, forms, 0, where=[f < 0 for f in forms], color=BAD, alpha=0.18
        )
        ax2.set_ylabel("Form (TSB)")
        style.latest_callout(
            ax, days[-1], points[-1].fitness, f"CTL {points[-1].fitness:.0f}"
        )
        ax.legend(loc="upper left")
    style.date_axis(ax)
    return style.save(fig, out_dir, "pmc_fitness_form.png")


def acwr_chart(series: Sequence[tuple[date, float]], out_dir: Path) -> Path:
    fig, ax = style.figure(
        "Acute:chronic workload ratio",
        "Recent vs. chronic load — stay in the safe band",
        "Date",
        "ACWR",
    )
    style.reference_band(ax, 0.8, 1.3, "Sweet spot (0.8–1.3)", GOOD)
    ax.axhline(1.5, color=BAD, linestyle="--", lw=1.0, label="Injury risk (>1.5)")
    if series:
        ax.plot([d for d, _ in series], [v for _, v in series], color=PRIMARY, lw=1.8)
        style.latest_callout(ax, series[-1][0], series[-1][1], f"{series[-1][1]:.2f}")
    ax.legend()
    style.date_axis(ax)
    return style.save(fig, out_dir, "acwr.png")


def efficiency_trend_chart(
    points: Sequence[tuple[date, float]], trend: Trend | None, out_dir: Path
) -> Path:
    fig, ax = style.figure(
        "Aerobic efficiency factor",
        "Speed per heartbeat over time — rising means improving aerobic fitness",
        "Date",
        "EF (m/min per bpm)",
    )
    if points:
        ax.scatter(
            [d for d, _ in points], [v for _, v in points], s=16, color=MUTED, alpha=0.5
        )
    if trend is not None:
        ax.plot(
            [d for d, _ in trend.fitted],
            [v for _, v in trend.fitted],
            color=ACCENT,
            lw=2.2,
        )
        ax.text(
            0.02,
            0.96,
            f"trend {trend.per_30_days:+.3f}/month   r={trend.r:.2f}",
            transform=ax.transAxes,
            va="top",
            fontsize=9,
            color=SUBTLE,
            bbox={
                "boxstyle": "round,pad=0.3",
                "fc": "white",
                "ec": MUTED,
                "alpha": 0.9,
            },
        )
    style.date_axis(ax)
    return style.save(fig, out_dir, "efficiency_factor.png")


def decoupling_chart(series: Sequence[tuple[date, float]], out_dir: Path) -> Path:
    fig, ax = style.figure(
        "Aerobic decoupling on long runs",
        "Pace-to-HR drift second half vs first — under 5% is well-trained",
        "Date",
        "Decoupling (%)",
    )
    ax.axhline(5, color=WARN, linestyle="--", lw=1.0, label="5% (well-trained)")
    if series:
        colors = [GOOD if v <= 5 else BAD for _, v in series]
        ax.bar([d for d, _ in series], [v for _, v in series], width=4, color=colors)
    ax.legend()
    style.date_axis(ax)
    return style.save(fig, out_dir, "aerobic_decoupling.png")


def best_effort_progression_chart(
    progressions: Sequence[BestEffortProgression], out_dir: Path
) -> Path:
    fig, ax = style.figure(
        "Best-effort pace progression",
        "All-time best pace at each distance, from the per-second streams",
        "Date",
        "Pace (min/km)",
    )
    for prog in progressions:
        km = prog.distance_m / 1000
        ax.step(
            [d for d, _ in prog.progression],
            [s / km for _, s in prog.progression],
            where="post",
            lw=2,
            label=prog.label,
        )
    if progressions:
        ax.legend(title="Distance")
    _pace_axis(ax)
    style.date_axis(ax)
    return style.save(fig, out_dir, "best_effort_progression.png")


def intensity_distribution_chart(dist: IntensityDistribution, out_dir: Path) -> Path:
    """Stacked easy/moderate/hard share of training time (polarization view)."""
    ratio = dist.easy_to_hard
    verdict = (
        "polarized" if ratio >= 3 else "pyramidal" if ratio >= 1 else "threshold-heavy"
    )
    fig, ax = style.figure(
        "Training intensity distribution",
        f"Share of time by zone — easy:hard {ratio} ({verdict}); 80/20 is the goal",
        "Share of time (%)",
        "",
    )
    segments = (
        ("Easy (Z1–2)", dist.easy_pct, GOOD),
        ("Moderate (Z3)", dist.moderate_pct, WARN),
        ("Hard (Z4–5)", dist.hard_pct, BAD),
    )
    left = 0.0
    for label, value, color in segments:
        ax.barh(0, value, left=left, color=color, label=f"{label} · {value:.0f}%")
        if value >= 6:
            ax.text(
                left + value / 2,
                0,
                f"{value:.0f}%",
                ha="center",
                va="center",
                color="white",
                fontweight="bold",
                fontsize=10,
            )
        left += value
    ax.set_yticks([])
    ax.set_xlim(0, 100)
    ax.grid(visible=False)
    ax.legend(ncol=3, loc="upper center", bbox_to_anchor=(0.5, 1.18))
    return style.save(fig, out_dir, "intensity_distribution.png")


def anomaly_timeline_chart(report: AnomalyReport, out_dir: Path) -> Path:
    """One row per metric, a dot per flagged day; red-flag days shaded."""
    fig, ax = style.figure(
        "Anomaly timeline",
        "Readiness & performance flags vs. each metric's rolling baseline",
        "Date",
        "",
    )
    rows = {metric: i for i, (metric, _) in enumerate(_ANOMALY_ROWS)}
    ax.set_yticks(range(len(_ANOMALY_ROWS)))
    ax.set_yticklabels([label for _, label in _ANOMALY_ROWS])
    ax.set_ylim(-0.5, len(_ANOMALY_ROWS) - 0.5)
    ax.grid(axis="y", visible=False)

    for flag in report.red_flag_days:
        ax.axvline(flag.day, color=BAD, linestyle="--", lw=0.9, alpha=0.5)
    for anomaly in (*report.health, *report.performance):
        row = rows.get(anomaly.metric)
        if row is not None:
            ax.scatter(anomaly.day, row, s=44, color=BAD, alpha=0.75, zorder=3)
    style.date_axis(ax)
    return style.save(fig, out_dir, "anomaly_timeline.png")


def critical_speed_chart(model: CsModel | None, out_dir: Path) -> Path:
    """Speed-duration curve: best efforts approaching the critical-speed line."""
    fig, ax = style.figure(
        "Critical speed",
        "Best efforts approach the sustainable-speed asymptote (v = CS + D'/t)",
        "Duration (s)",
        "Speed (m/s)",
    )
    if model is not None and model.points:
        durations = [p.seconds for p in model.points]
        ax.scatter(
            durations,
            [p.distance_m / p.seconds for p in model.points],
            s=52,
            color=PRIMARY,
            zorder=5,
            label="Best efforts",
        )
        t_max = max(durations)
        curve_t = [t_max * pct / 100 for pct in range(5, 101)]
        ax.plot(
            curve_t,
            [model.cs_mps + model.d_prime_m / t for t in curve_t],
            color=ACCENT,
            lw=2,
            label="Model",
        )
        ax.axhline(
            model.cs_mps,
            color=GOOD,
            linestyle="--",
            lw=1.2,
            label=f"CS {model.cs_mps:.2f} m/s",
        )
        ax.text(
            0.98,
            0.05,
            f"CS {model.cs_mps:.2f} m/s   D' {model.d_prime_m:.0f} m   r={model.r:.2f}",
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            fontsize=9,
            color=SUBTLE,
            bbox={
                "boxstyle": "round,pad=0.3",
                "fc": "white",
                "ec": MUTED,
                "alpha": 0.9,
            },
        )
        ax.legend(loc="upper right")
    return style.save(fig, out_dir, "critical_speed.png")


def readiness_chart(days: Sequence[ReadinessDay], out_dir: Path) -> Path:
    """Daily readiness score over time with the 40-60 normal band."""
    fig, ax = style.figure(
        "Daily readiness",
        "Composite recovery score from resting HR, HRV, sleep, and HR-recovery",
        "Date",
        "Readiness (0–100)",
    )
    style.reference_band(ax, 40, 60, "Normal (40–60)", GOOD)
    if days:
        xs = [d.day for d in days]
        ys = [d.score for d in days]
        ax.plot(xs, ys, color=PRIMARY, lw=1.6)
        style.latest_callout(ax, xs[-1], ys[-1], f"{ys[-1]:.0f}")
    ax.set_ylim(0, 100)
    ax.legend(loc="lower left")
    style.date_axis(ax)
    return style.save(fig, out_dir, "readiness.png")
