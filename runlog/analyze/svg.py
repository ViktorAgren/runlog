"""Inline SVG line charts for the per-workout pages.

Pure string building: each chart renders every point of a run's stream as one
SVG polyline with light gridlines and axis labels. No plotting dependency, crisp
at any width, and far lighter than a raster image per metric per run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

_W = 760.0
_H = 240.0
_PAD_L = 52.0
_PAD_R = 14.0
_PAD_T = 24.0
_PAD_B = 30.0


def _format(value: float, kind: str, decimals: int = 1) -> str:
    """Axis label: pace as m:ss, floats to ``decimals`` places, else integer."""
    if kind == "pace":
        total = int(round(value))
        return f"{total // 60}:{total % 60:02d}"
    if kind == "float":
        return f"{value:.{decimals}f}"
    return f"{int(round(value))}"


def _float_decimals(values: Sequence[float], limit: int = 3) -> int:
    """Fewest decimals (>=1) that render every tick label distinctly.

    A narrow range (e.g. stride 0.90-1.05) collapses to repeated '1.0' labels at
    one decimal; widen the precision until the labels separate.
    """
    for decimals in range(1, limit + 1):
        labels = [f"{v:.{decimals}f}" for v in values]
        if len(set(labels)) == len(labels):
            return decimals
    return limit


def _bounds(values: Sequence[float]) -> tuple[float, float]:
    low, high = min(values), max(values)
    if low == high:  # a flat series still needs a non-zero span to map
        return low - 1.0, high + 1.0
    return low, high


def _y_range(values: Sequence[float], min_span: float) -> tuple[float, float]:
    """Robust y-axis range: the 2nd-98th percentile band, never below ``min_span``.

    Raw min/max let a handful of stops/GPS spikes set the scale, squashing the
    real signal into a sliver; a near-constant series would likewise amplify
    quantization jitter to fill the plot. Scaling to the central band (every
    point is still drawn, clamped to the axis) and holding a metric-appropriate
    floor keeps a steady run flat and a varied run legible.
    """
    ys = sorted(values)
    last = len(ys) - 1
    low = ys[round(0.02 * last)]
    high = ys[round(0.98 * last)]
    if high - low < min_span:
        mid = (high + low) / 2
        low, high = mid - min_span / 2, mid + min_span / 2
    if low == high:
        return low - 1.0, high + 1.0
    return low, high


def line_chart(
    points: Sequence[tuple[float, float | None]],
    *,
    title: str,
    unit: str,
    color: str,
    y_format: str = "int",
    min_span: float = 0.0,
) -> str:
    """Render ``(x, y)`` points (y may be None for gaps) to an inline ``<svg>``."""
    valid = [(x, y) for x, y in points if y is not None]
    if len(valid) < 2:
        return f'<div class="chart-empty">{title}: no stream</div>'
    xmin, xmax = _bounds([x for x, _ in valid])
    ymin, ymax = _y_range([y for _, y in valid if y is not None], min_span)
    _y_top, _y_bot = _PAD_T, _H - _PAD_B

    def sx(x: float) -> float:
        return _PAD_L + (x - xmin) / (xmax - xmin) * (_W - _PAD_L - _PAD_R)

    def sy(y: float) -> float:
        v = _y_bot - (y - ymin) / (ymax - ymin) * (_y_bot - _y_top)
        return min(max(v, _y_top), _y_bot)

    segments: list[str] = []
    pen_down = False
    for x, y in points:
        if y is None:
            pen_down = False
            continue
        segments.append(f"{'L' if pen_down else 'M'}{sx(x):.1f} {sy(y):.1f}")
        pen_down = True
    path = " ".join(segments)

    tick_values = [ymin + frac * (ymax - ymin) for frac in (0.0, 0.5, 1.0)]
    decimals = _float_decimals(tick_values) if y_format == "float" else 1
    gridlines: list[str] = []
    for value in tick_values:
        gy = sy(value)
        gridlines.append(
            f'<line x1="{_PAD_L:.0f}" y1="{gy:.1f}" x2="{_W - _PAD_R:.0f}" '
            f'y2="{gy:.1f}" class="grid"/>'
            f'<text x="{_PAD_L - 6:.0f}" y="{gy + 3:.1f}" class="ylab">'
            f"{_format(value, y_format, decimals)}</text>"
        )
    xlabels = (
        f'<text x="{sx(xmin):.0f}" y="{_H - 10:.0f}" class="xlab">'
        f"{_format(xmin, 'int')}</text>"
        f'<text x="{sx(xmax):.0f}" y="{_H - 10:.0f}" class="xlab" '
        f'text-anchor="end">{_format(xmax, "int")} min</text>'
    )
    return (
        f'<svg class="chart" viewBox="0 0 {_W:.0f} {_H:.0f}" '
        f'role="img" aria-label="{title}">'
        f'<text x="{_PAD_L:.0f}" y="15" class="ctitle">{title}'
        f'{f" ({unit})" if unit else ""}</text>'
        f"{''.join(gridlines)}{xlabels}"
        f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.0"/>'
        f"</svg>"
    )
