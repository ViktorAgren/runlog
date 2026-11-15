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
_H = 220.0
_PAD_L = 52.0
_PAD_R = 14.0
_PAD_T = 24.0
_PAD_B = 30.0


def _format(value: float, kind: str) -> str:
    """Axis label for a value: pace as m:ss, floats to one decimal, else integer."""
    if kind == "pace":
        total = int(round(value))
        return f"{total // 60}:{total % 60:02d}"
    if kind == "float":
        return f"{value:.1f}"
    return f"{int(round(value))}"


def _bounds(values: Sequence[float]) -> tuple[float, float]:
    low, high = min(values), max(values)
    if low == high:  # a flat series still needs a non-zero span to map
        return low - 1.0, high + 1.0
    return low, high


def line_chart(
    points: Sequence[tuple[float, float | None]],
    *,
    title: str,
    unit: str,
    color: str,
    y_format: str = "int",
) -> str:
    """Render ``(x, y)`` points (y may be None for gaps) to an inline ``<svg>``."""
    valid = [(x, y) for x, y in points if y is not None]
    if len(valid) < 2:
        return f'<div class="chart-empty">{title}: no stream</div>'
    xmin, xmax = _bounds([x for x, _ in valid])
    ymin, ymax = _bounds([y for _, y in valid if y is not None])

    def sx(x: float) -> float:
        return _PAD_L + (x - xmin) / (xmax - xmin) * (_W - _PAD_L - _PAD_R)

    def sy(y: float) -> float:
        return _H - _PAD_B - (y - ymin) / (ymax - ymin) * (_H - _PAD_T - _PAD_B)

    segments: list[str] = []
    pen_down = False
    for x, y in points:
        if y is None:
            pen_down = False
            continue
        segments.append(f"{'L' if pen_down else 'M'}{sx(x):.1f} {sy(y):.1f}")
        pen_down = True
    path = " ".join(segments)

    gridlines: list[str] = []
    for frac in (0.0, 0.5, 1.0):
        value = ymin + frac * (ymax - ymin)
        gy = sy(value)
        gridlines.append(
            f'<line x1="{_PAD_L:.0f}" y1="{gy:.1f}" x2="{_W - _PAD_R:.0f}" '
            f'y2="{gy:.1f}" class="grid"/>'
            f'<text x="{_PAD_L - 6:.0f}" y="{gy + 3:.1f}" class="ylab">'
            f"{_format(value, y_format)}</text>"
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
        f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.5"/>'
        f"</svg>"
    )
