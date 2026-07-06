"""Matplotlib chart rendering for the analysis report.

Each function consumes the plain dataclasses produced by :mod:`runlog.analyze.
metrics` and writes a single PNG, returning its path. The Agg backend is
selected up front so charts render headless (and under pytest). matplotlib is
untyped, so this module is the one place we tolerate loose plotting calls;
all computation lives in ``metrics``.
"""

from __future__ import annotations

import statistics
from typing import TYPE_CHECKING, Any

import matplotlib

matplotlib.use("Agg")
from matplotlib.figure import Figure  # noqa: E402  (must follow use("Agg"))
from matplotlib.ticker import FuncFormatter  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime
    from pathlib import Path

    from runlog.analyze.analytics import BestEffortProgression, PmcPoint, Trend
    from runlog.analyze.anomaly import AnomalyReport
    from runlog.analyze.metrics import (
        BucketPace,
        Heatmap,
        HrPoint,
        HrZone,
        PacePoint,
        RacePrediction,
        WeeklyLoad,
        WeeklyVolume,
    )

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
_ROLLING_WINDOW = 5


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


def _figure(title: str, xlabel: str, ylabel: str) -> tuple[Any, Any]:
    fig = Figure(figsize=(9, 5), dpi=110)
    ax = fig.add_subplot(111)
    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.grid(True, linestyle=":", alpha=0.4)
    return fig, ax


def _save(fig: Any, out_dir: Path, name: str) -> Path:
    path = out_dir / name
    fig.tight_layout()
    fig.savefig(path)
    return path


def _pace_axis(ax: Any) -> None:
    """Format a pace axis as M:SS and invert it so faster is up."""
    ax.yaxis.set_major_formatter(FuncFormatter(_format_pace))
    ax.invert_yaxis()


def weekly_volume_chart(weekly: Sequence[WeeklyVolume], out_dir: Path) -> Path:
    fig, ax = _figure("Weekly volume", "Week", "Distance (km)")
    weeks = [w.week_start for w in weekly]
    ax.bar(weeks, [w.distance_km for w in weekly], width=6, color="#5891f5", alpha=0.8)
    ax.plot(
        weeks,
        [w.rolling_km for w in weekly],
        color="#e0566f",
        linewidth=2,
        label="4-week rolling mean",
    )
    ax.legend()
    fig.autofmt_xdate()
    return _save(fig, out_dir, "weekly_volume.png")


def monthly_by_year_chart(by_year: dict[int, list[float]], out_dir: Path) -> Path:
    fig, ax = _figure("Monthly distance by year", "Month", "Distance (km)")
    years = sorted(by_year)
    width = 0.8 / max(len(years), 1)
    for i, year in enumerate(years):
        offset = (i - (len(years) - 1) / 2) * width
        positions = [m + offset for m in range(1, 13)]
        ax.bar(positions, by_year[year], width=width, label=str(year))
    ax.set_xticks(range(1, 13))
    ax.set_xticklabels(["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"])
    if years:
        ax.legend()
    return _save(fig, out_dir, "monthly_by_year.png")


def distance_histogram(distances: Sequence[float], out_dir: Path) -> Path:
    fig, ax = _figure("Run distance distribution", "Distance (km)", "Runs")
    if distances:
        ax.hist(list(distances), bins=20, color="#5891f5", alpha=0.85)
    return _save(fig, out_dir, "distance_histogram.png")


def training_heatmap_chart(heatmap: Heatmap, out_dir: Path) -> Path:
    fig, ax = _figure("Training heatmap", "Week", "Weekday")
    if heatmap.week_starts:
        mesh = ax.pcolormesh(heatmap.matrix, cmap="YlGnBu")
        fig.colorbar(mesh, ax=ax, label="Distance (km)")
    ax.set_yticks([i + 0.5 for i in range(7)])
    ax.set_yticklabels(_WEEKDAYS)
    ax.invert_yaxis()
    return _save(fig, out_dir, "training_heatmap.png")


def _rolling_median(values: Sequence[float]) -> list[float]:
    return [
        statistics.median(values[max(0, i - _ROLLING_WINDOW + 1) : i + 1])
        for i in range(len(values))
    ]


def pace_over_time_chart(points: Sequence[PacePoint], out_dir: Path) -> Path:
    fig, ax = _figure("Pace over time", "Date", "Pace (min/km)")
    ordered = sorted(points, key=lambda p: p.start)
    if ordered:
        starts = [p.start for p in ordered]
        paces = [p.pace_s_per_km for p in ordered]
        ax.scatter(starts, paces, s=14, color="#5891f5", alpha=0.5, label="Runs")
        ax.plot(
            starts,
            _rolling_median(paces),
            color="#e0566f",
            linewidth=2,
            label="Rolling median",
        )
        ax.legend()
    _pace_axis(ax)
    fig.autofmt_xdate()
    return _save(fig, out_dir, "pace_over_time.png")


def grade_adjusted_pace_chart(points: Sequence[PacePoint], out_dir: Path) -> Path:
    """Grade-adjusted pace over time (flattens hilly runs for a fair trend)."""
    fig, ax = _figure("Grade-adjusted pace over time", "Date", "Pace (min/km)")
    ordered = sorted(points, key=lambda p: p.start)
    if ordered:
        starts = [p.start for p in ordered]
        paces = [p.pace_s_per_km for p in ordered]
        ax.scatter(starts, paces, s=14, color="#5891f5", alpha=0.5, label="Runs")
        ax.plot(
            starts,
            _rolling_median(paces),
            color="#e0566f",
            linewidth=2,
            label="Rolling median",
        )
        ax.legend()
    _pace_axis(ax)
    fig.autofmt_xdate()
    return _save(fig, out_dir, "grade_adjusted_pace.png")


def fastest_by_bucket_chart(buckets: Sequence[BucketPace], out_dir: Path) -> Path:
    fig, ax = _figure("Fastest pace by distance", "Distance bucket", "Pace (min/km)")
    labels = [b.label for b in buckets]
    values = [b.fastest_pace_s_per_km or 0.0 for b in buckets]
    ax.bar(labels, values, color="#5891f5", alpha=0.85)
    _pace_axis(ax)
    return _save(fig, out_dir, "fastest_by_bucket.png")


def pace_by_weekday_chart(
    weekday_paces: Sequence[Sequence[float]], out_dir: Path
) -> Path:
    fig, ax = _figure("Pace by day of week", "Weekday", "Pace (min/km)")
    data = [list(paces) if paces else [float("nan")] for paces in weekday_paces]
    ax.boxplot(data, tick_labels=list(_WEEKDAYS))
    _pace_axis(ax)
    return _save(fig, out_dir, "pace_by_weekday.png")


def hr_over_time_chart(points: Sequence[HrPoint], out_dir: Path) -> Path:
    fig, ax = _figure("Heart rate over time", "Date", "Heart rate (bpm)")
    ordered = sorted(points, key=lambda p: p.start)
    if ordered:
        starts = [p.start for p in ordered]
        ax.plot(starts, [p.avg_hr for p in ordered], color="#5891f5", label="Average")
        maxes = [(p.start, p.max_hr) for p in ordered if p.max_hr is not None]
        if maxes:
            ax.plot(
                [s for s, _ in maxes],
                [m for _, m in maxes],
                color="#e0566f",
                alpha=0.6,
                label="Max",
            )
        ax.legend()
    fig.autofmt_xdate()
    return _save(fig, out_dir, "hr_over_time.png")


def hr_histogram(
    samples: Sequence[float], out_dir: Path, zones: Sequence[HrZone] = ()
) -> Path:
    fig, ax = _figure("Heart-rate distribution", "Heart rate (bpm)", "Samples")
    if samples:
        ax.hist(list(samples), bins=30, color="#5891f5", alpha=0.7)
    top = ax.get_ylim()[1]
    for zone in zones:
        ax.axvline(zone.low_bpm, color="#8a909b", linestyle="--", linewidth=0.8)
        ax.text(
            zone.low_bpm, top, f" {zone.label}", color="#8a909b", va="top", fontsize=8
        )
    return _save(fig, out_dir, "hr_histogram.png")


def efficiency_chart(points: Sequence[PacePoint], out_dir: Path) -> Path:
    fig, ax = _figure(
        "Aerobic efficiency (pace vs HR)", "Pace (min/km)", "Avg HR (bpm)"
    )
    paired = [(p.pace_s_per_km, p.avg_hr) for p in points if p.avg_hr is not None]
    if paired:
        ax.scatter(
            [p for p, _ in paired],
            [h for _, h in paired],
            s=16,
            color="#5891f5",
            alpha=0.5,
        )
    ax.xaxis.set_major_formatter(FuncFormatter(_format_pace))
    return _save(fig, out_dir, "efficiency.png")


def _rolling_mean(values: Sequence[float], window: int) -> list[float]:
    return [
        statistics.fmean(values[max(0, i - window + 1) : i + 1])
        for i in range(len(values))
    ]


def race_prediction_chart(predictions: Sequence[RacePrediction], out_dir: Path) -> Path:
    fig, ax = _figure("Predicted race times (Riegel)", "Race", "Time (minutes)")
    if predictions:
        labels = [p.label for p in predictions]
        minutes = [p.seconds / 60 for p in predictions]
        bars = ax.bar(labels, minutes, color="#5891f5", alpha=0.85)
        for prediction, bar in zip(predictions, bars, strict=False):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height(),
                _format_clock(prediction.seconds),
                ha="center",
                va="bottom",
                fontsize=9,
            )
    return _save(fig, out_dir, "race_predictions.png")


def runs_per_week_chart(weekly: Sequence[WeeklyVolume], out_dir: Path) -> Path:
    fig, ax = _figure("Runs per week", "Week", "Runs")
    ax.bar(
        [w.week_start for w in weekly],
        [w.run_count for w in weekly],
        width=6,
        color="#5891f5",
        alpha=0.85,
    )
    fig.autofmt_xdate()
    return _save(fig, out_dir, "runs_per_week.png")


def rest_gap_histogram(gaps: Sequence[int], out_dir: Path) -> Path:
    fig, ax = _figure("Days between runs", "Gap (days)", "Occurrences")
    if gaps:
        ax.hist(list(gaps), bins=range(0, max(gaps) + 2), color="#5891f5", alpha=0.85)
    return _save(fig, out_dir, "rest_gaps.png")


def hr_zones_chart(zones: Sequence[HrZone], hr_max: float, out_dir: Path) -> Path:
    fig, ax = _figure(
        f"Time in heart-rate zones (HRmax ≈ {hr_max:.0f} bpm)",
        "Zone",
        "Time (minutes)",
    )
    ax.bar(
        [f"{z.label}\n{z.bpm_range}" for z in zones],
        [z.minutes for z in zones],
        color=["#8fb8f6", "#5891f5", "#f2b544", "#ee8f4e", "#e0566f"],
    )
    return _save(fig, out_dir, "hr_zones.png")


def training_load_chart(load: Sequence[WeeklyLoad], out_dir: Path) -> Path:
    fig, ax = _figure("Weekly training load", "Week", "HR-weighted load")
    if load:
        ax.plot(
            [w.week_start for w in load],
            [w.load for w in load],
            color="#e0566f",
            linewidth=2,
        )
    fig.autofmt_xdate()
    return _save(fig, out_dir, "training_load.png")


def cadence_chart(points: Sequence[tuple[datetime, float]], out_dir: Path) -> Path:
    fig, ax = _figure("Cadence over time", "Date", "Avg cadence (steps/min)")
    ordered = sorted(points, key=lambda p: p[0])
    if ordered:
        days = [d for d, _ in ordered]
        values = [v for _, v in ordered]
        ax.scatter(days, values, s=12, color="#5891f5", alpha=0.4, label="Runs")
        ax.plot(
            days, _rolling_mean(values, 10), color="#e0566f", linewidth=2, label="Trend"
        )
        ax.legend()
    fig.autofmt_xdate()
    return _save(fig, out_dir, "cadence.png")


def elevation_by_month_chart(by_year: dict[int, list[float]], out_dir: Path) -> Path:
    fig, ax = _figure("Elevation gain by month", "Month", "Elevation gain (m)")
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
    ax.set_xticklabels(["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"])
    if years:
        ax.legend()
    return _save(fig, out_dir, "elevation_by_month.png")


def start_hour_chart(hours: Sequence[int], out_dir: Path) -> Path:
    fig, ax = _figure("Run start time of day", "Hour (local)", "Runs")
    if hours:
        ax.hist(list(hours), bins=range(0, 25), color="#5891f5", alpha=0.85)
    ax.set_xticks(range(0, 25, 3))
    return _save(fig, out_dir, "start_hours.png")


def cumulative_ytd_chart(
    by_year: dict[int, list[tuple[int, float]]], out_dir: Path
) -> Path:
    fig, ax = _figure("Cumulative distance (year to date)", "Day of year", "km")
    for year in sorted(by_year):
        curve = by_year[year]
        ax.plot(
            [day for day, _ in curve],
            [km for _, km in curve],
            linewidth=2,
            label=str(year),
        )
    if by_year:
        ax.legend()
    return _save(fig, out_dir, "cumulative_distance.png")


def marker_chart(
    daily: Sequence[tuple[date, float]],
    title: str,
    ylabel: str,
    filename: str,
    out_dir: Path,
    trend_window: int = 14,
) -> Path:
    """Plot faint daily values with a prominent rolling-mean trend line."""
    fig, ax = _figure(title, "Date", ylabel)
    if daily:
        days = [d for d, _ in daily]
        values = [v for _, v in daily]
        ax.scatter(days, values, s=10, color="#5891f5", alpha=0.3, label="Daily")
        ax.plot(
            days,
            _rolling_mean(values, trend_window),
            color="#e0566f",
            linewidth=2,
            label=f"{trend_window}-day mean",
        )
        ax.legend()
    fig.autofmt_xdate()
    return _save(fig, out_dir, filename)


# --- High-level analytics charts --------------------------------------------


def pmc_chart(points: Sequence[PmcPoint], out_dir: Path) -> Path:
    fig, ax = _figure(
        "Fitness / Fatigue / Form (Performance Management Chart)",
        "Date",
        "Training load (CTL / ATL)",
    )
    if points:
        days = [p.day for p in points]
        ax.plot(
            days,
            [p.fitness for p in points],
            color="#1f6feb",
            lw=2,
            label="Fitness (CTL)",
        )
        ax.plot(
            days,
            [p.fatigue for p in points],
            color="#e0566f",
            lw=1.2,
            label="Fatigue (ATL)",
        )
        forms = [p.form for p in points]
        ax2 = ax.twinx()
        ax2.fill_between(
            days, forms, 0, where=[f >= 0 for f in forms], color="#3fb950", alpha=0.25
        )
        ax2.fill_between(
            days, forms, 0, where=[f < 0 for f in forms], color="#e0566f", alpha=0.20
        )
        ax2.set_ylabel("Form (TSB)")
        ax.legend(loc="upper left")
    fig.autofmt_xdate()
    return _save(fig, out_dir, "pmc_fitness_form.png")


def acwr_chart(series: Sequence[tuple[date, float]], out_dir: Path) -> Path:
    fig, ax = _figure("Acute:chronic workload ratio", "Date", "ACWR")
    ax.axhspan(0.8, 1.3, color="#3fb950", alpha=0.15, label="Sweet spot (0.8-1.3)")
    ax.axhline(1.5, color="#e0566f", linestyle="--", lw=0.8, label="Injury-risk (>1.5)")
    if series:
        ax.plot([d for d, _ in series], [v for _, v in series], color="#1f6feb", lw=1.5)
    ax.legend()
    fig.autofmt_xdate()
    return _save(fig, out_dir, "acwr.png")


def efficiency_trend_chart(
    points: Sequence[tuple[date, float]], trend: Trend | None, out_dir: Path
) -> Path:
    fig, ax = _figure(
        "Aerobic efficiency factor (speed per heartbeat)",
        "Date",
        "EF (m/min per bpm)",
    )
    if points:
        ax.scatter(
            [d for d, _ in points],
            [v for _, v in points],
            s=14,
            color="#5891f5",
            alpha=0.5,
        )
    if trend is not None:
        ax.plot(
            [d for d, _ in trend.fitted],
            [v for _, v in trend.fitted],
            color="#e0566f",
            lw=2,
        )
        ax.text(
            0.02,
            0.96,
            f"trend {trend.per_30_days:+.3f} / month   r={trend.r:.2f}",
            transform=ax.transAxes,
            va="top",
            fontsize=9,
        )
    fig.autofmt_xdate()
    return _save(fig, out_dir, "efficiency_factor.png")


def decoupling_chart(series: Sequence[tuple[date, float]], out_dir: Path) -> Path:
    fig, ax = _figure("Aerobic decoupling on long runs", "Date", "Decoupling (%)")
    ax.axhline(5, color="#e0566f", linestyle="--", lw=0.8, label="5% (well-trained)")
    if series:
        ax.bar([d for d, _ in series], [v for _, v in series], width=4, color="#5891f5")
    ax.legend()
    fig.autofmt_xdate()
    return _save(fig, out_dir, "aerobic_decoupling.png")


def best_effort_progression_chart(
    progressions: Sequence[BestEffortProgression], out_dir: Path
) -> Path:
    fig, ax = _figure(
        "Best-effort pace progression (from streams)", "Date", "Pace (min/km)"
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
        ax.legend()
    _pace_axis(ax)
    fig.autofmt_xdate()
    return _save(fig, out_dir, "best_effort_progression.png")


def anomaly_timeline_chart(report: AnomalyReport, out_dir: Path) -> Path:
    """One row per metric, a dot per flagged day; red-flag days shaded."""
    fig, ax = _figure("Anomaly timeline", "Date", "")
    rows = {metric: i for i, (metric, _) in enumerate(_ANOMALY_ROWS)}
    ax.set_yticks(range(len(_ANOMALY_ROWS)))
    ax.set_yticklabels([label for _, label in _ANOMALY_ROWS])
    ax.set_ylim(-0.5, len(_ANOMALY_ROWS) - 0.5)

    for flag in report.red_flag_days:
        ax.axvline(flag.day, color="#e0566f", linestyle="--", lw=0.8, alpha=0.6)
    for anomaly in (*report.health, *report.performance):
        row = rows.get(anomaly.metric)
        if row is not None:
            ax.scatter(anomaly.day, row, s=40, color="#e0566f", alpha=0.7, zorder=3)
    fig.autofmt_xdate()
    return _save(fig, out_dir, "anomaly_timeline.png")
