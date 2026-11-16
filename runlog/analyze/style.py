"""Shared visual theme and figure toolkit for the analysis charts.

A modern analytical-dashboard aesthetic — de-spined axes, a light y-grid, a
left-aligned title with a grey context subtitle, a colorblind-safe palette, and
reusable annotation primitives (trend line + uncertainty ribbon, reference
bands, latest-value callouts, bar value labels, footnotes). Centralizing it here
keeps every chart in :mod:`runlog.analyze.charts` short and visually consistent.

matplotlib stays the renderer; seaborn is used only as an explicit-``ax`` helper
for statistical layers (KDE/regression), never for global theming.
"""

from __future__ import annotations

import math
import statistics
from typing import TYPE_CHECKING, Any

import matplotlib
import numpy as np

matplotlib.use("Agg")
from matplotlib import dates as mdates  # noqa: E402  (must follow use("Agg"))
from matplotlib.figure import Figure  # noqa: E402

from runlog.analyze import stats  # noqa: E402
from runlog.analyze.analytics import linear_trend  # noqa: E402

# matplotlib ships partial type hints; reach its (stub-less) ``cycler`` and the
# dates locator/formatter through Any handles so strict mypy stays happy.
_MPL: Any = matplotlib
_MDATES: Any = mdates

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date
    from pathlib import Path

    from runlog.analyze.analytics import Trend

# --- Palette (colorblind-safe) ----------------------------------------------
INK = "#0f172a"  # primary text
SUBTLE = "#64748b"  # subtitles / secondary text
MUTED = "#94a3b8"  # de-emphasized marks (raw scatter)
GRID = "#e2e8f0"
PRIMARY = "#2563eb"  # main series
ACCENT = "#f97316"  # trend / secondary series
FORM = "#7c3aed"  # violet, for the PMC Form (TSB) line
GOOD = "#16a34a"
WARN = "#d97706"
BAD = "#dc2626"
# Heart-rate zones Z1..Z5, cool -> warm.
ZONE_COLORS = ("#3b82f6", "#22c55e", "#eab308", "#f97316", "#ef4444")
# Categorical cycle (Okabe-Ito), for multi-series charts.
PALETTE = (
    "#0072B2",
    "#E69F00",
    "#009E73",
    "#CC79A7",
    "#56B4E9",
    "#D55E00",
    "#7c3aed",
)

_FONT_STACK = ["DejaVu Sans", "Helvetica Neue", "Arial", "sans-serif"]


def apply_theme() -> None:
    """Install the dashboard look into matplotlib rcParams (idempotent)."""
    matplotlib.rcParams.update(
        {
            "figure.dpi": 150,
            "figure.facecolor": "white",
            "savefig.facecolor": "white",
            "font.family": "sans-serif",
            "font.sans-serif": _FONT_STACK,
            "text.color": INK,
            "axes.edgecolor": MUTED,
            "axes.labelcolor": SUBTLE,
            "axes.titlecolor": INK,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "axes.axisbelow": True,
            "grid.color": GRID,
            "grid.linewidth": 1.1,
            "lines.linewidth": 2.0,
            "xtick.color": SUBTLE,
            "ytick.color": SUBTLE,
            "axes.labelsize": 13,
            "xtick.labelsize": 12,
            "ytick.labelsize": 12,
            "legend.fontsize": 11,
            "legend.frameon": False,
            "axes.prop_cycle": _MPL.cycler(color=list(PALETTE)),
        }
    )


def figure(
    title: str = "",
    subtitle: str | None = None,
    xlabel: str = "",
    ylabel: str = "",
    figsize: tuple[float, float] = (9.5, 5.4),
) -> tuple[Any, Any]:
    """A themed figure/axes with only axes and data.

    The title and description are rendered as the HTML ``<figcaption>`` (journal
    style), not inside the figure, so they never appear twice; ``title`` /
    ``subtitle`` are accepted for call-site compatibility but not drawn.
    """
    _ = (title, subtitle)
    fig = Figure(figsize=figsize)
    ax = fig.add_subplot(111)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.grid(axis="x", visible=False)
    return fig, ax


def sig_value(value: float, sig_digits: int = 3) -> str:
    """Format a value to ~``sig_digits`` significant digits, no trailing zeros.

    Fixed-decimal formats break on fraction-scale metrics (running economy at
    0.013 m/s per W renders as "0.0" under ``{:.1f}``); scaling the decimals to
    the magnitude keeps callouts and trend labels meaningful at any scale.
    """
    if value == 0:
        return "0"
    decimals = max(0, sig_digits - 1 - math.floor(math.log10(abs(value))))
    if decimals == 0:
        return f"{value:.0f}"
    return f"{value:.{decimals}f}".rstrip("0").rstrip(".")


def footnote(fig: Any, text: str) -> None:
    """A small grey caption at the bottom-left (data source / n / date)."""
    fig.text(0.01, 0.005, text, fontsize=9.5, color=MUTED, va="bottom")


def save(fig: Any, out_dir: Path, name: str) -> Path:
    """Write the figure as a vector SVG (``name`` with its suffix forced to .svg)."""
    path = (out_dir / name).with_suffix(".svg")
    fig.savefig(path, format="svg", bbox_inches="tight")
    return path


def trend_annotation(
    ax: Any, points: Sequence[tuple[date, float]], color: str = ACCENT
) -> Trend | None:
    """Draw an OLS trend line + ±1σ residual ribbon + a slope/r label.

    Returns the fitted :class:`~runlog.analyze.analytics.Trend` (or ``None`` when
    there are too few points), so callers/tests can reuse the numbers.
    """
    trend = linear_trend(points)
    if trend is None:
        return None
    xs = [day for day, _ in points]
    fit = [value for _, value in trend.fitted]
    residuals = [y - f for (_, y), f in zip(points, fit, strict=True)]
    sd = statistics.pstdev(residuals) if len(residuals) > 1 else 0.0
    if sd > 0:
        ax.fill_between(
            xs,
            [f - sd for f in fit],
            [f + sd for f in fit],
            color=color,
            alpha=0.12,
            zorder=1,
        )
    ax.plot(xs, fit, color=color, lw=2, zorder=5, label="Trend")
    sign = "+" if trend.per_30_days >= 0 else ""
    test = stats.trend_test(points)
    if test is not None:
        low, high = test.ci_30d
        label_text = (
            f"trend {sign}{sig_value(test.per_30_days)}/30d  "
            f"[{sig_value(low)}, {sig_value(high)}]\n"
            f"{stats.format_p(test.p)}  n={test.n}"
        )
    else:
        label_text = f"trend {sign}{sig_value(trend.per_30_days)}/30d   r={trend.r:.2f}"
    ax.text(
        0.02,
        0.96,
        label_text,
        transform=ax.transAxes,
        va="top",
        fontsize=11,
        color=SUBTLE,
        bbox={"boxstyle": "round,pad=0.3", "fc": "white", "ec": GRID, "alpha": 0.9},
    )
    return trend


def regression_band(
    ax: Any, points: Sequence[tuple[date, float]], color: str = ACCENT
) -> None:
    """OLS fit with a 95% confidence band (mean) and a 95% prediction band (new obs)."""
    if len(points) < 3:
        return
    days = [day for day, _ in points]
    x = np.array([_MDATES.date2num(day) for day in days], dtype=float)
    y = np.array([value for _, value in points], dtype=float)
    n = len(x)
    slope, intercept = (float(c) for c in np.polyfit(x, y, 1))
    fit = intercept + slope * x
    xbar = float(x.mean())
    sxx = float(np.sum((x - xbar) ** 2))
    if sxx == 0:
        return
    resid_se = math.sqrt(float(np.sum((y - fit) ** 2)) / (n - 2))
    tval = stats.students_t_ppf(0.975, n - 2)
    se_fit = resid_se * np.sqrt(1 / n + (x - xbar) ** 2 / sxx)
    se_pred = resid_se * np.sqrt(1 + 1 / n + (x - xbar) ** 2 / sxx)
    ax.fill_between(
        days,
        fit - tval * se_pred,
        fit + tval * se_pred,
        color=color,
        alpha=0.08,
        lw=0,
        label="95% prediction",
    )
    ax.fill_between(
        days,
        fit - tval * se_fit,
        fit + tval * se_fit,
        color=color,
        alpha=0.22,
        lw=0,
        label="95% CI",
    )
    ax.plot(days, fit, color=color, lw=2, zorder=5, label="OLS fit")


def reference_band(
    ax: Any, low: float, high: float, label: str, color: str = GOOD
) -> None:
    """Shade a horizontal reference range (e.g. an ACWR sweet spot)."""
    ax.axhspan(low, high, color=color, alpha=0.12, label=label, zorder=0)


def latest_callout(ax: Any, x: Any, y: float, text: str, color: str = PRIMARY) -> None:
    """Mark and label the most recent point so the current value stands out."""
    ax.scatter([x], [y], s=45, color=color, zorder=6, edgecolor="white", linewidth=1)
    ax.annotate(
        text,
        xy=(x, y),
        xytext=(6, 6),
        textcoords="offset points",
        fontsize=11,
        fontweight="bold",
        color=color,
    )


def bar_value_labels(ax: Any, bars: Any, fmt: str = "{:.0f}") -> None:
    """Print each bar's value just above it."""
    for bar in bars:
        height = bar.get_height()
        ax.annotate(
            fmt.format(height),
            xy=(bar.get_x() + bar.get_width() / 2, height),
            xytext=(0, 3),
            textcoords="offset points",
            ha="center",
            fontsize=10,
            color=SUBTLE,
        )


def date_axis(ax: Any) -> None:
    """Apply a compact, readable date formatter to the x-axis."""
    locator = _MDATES.AutoDateLocator()
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(_MDATES.ConciseDateFormatter(locator))


apply_theme()
