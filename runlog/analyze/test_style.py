"""Unit tests for the chart theme + figure toolkit."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

from runlog.analyze import style


def test_palettes_are_valid_hex() -> None:
    hex_re = re.compile(r"^#[0-9a-fA-F]{6}$")
    swatches = [*style.ZONE_COLORS, *style.PALETTE, style.PRIMARY, style.ACCENT]
    assert (
        len(style.ZONE_COLORS),
        len(style.PALETTE),
        all(map(hex_re.match, swatches)),
    ) == (
        5,
        7,
        True,
    )


def test_figure_sets_left_aligned_title() -> None:
    _fig, ax = style.figure("Weekly volume", subtitle="km per week")
    assert ax.get_title(loc="left") == "Weekly volume"


def test_trend_annotation_returns_fitted_trend() -> None:
    # A perfect line rising 2 units/day -> +60/30d, r = 1.0.
    points = [(date(2026, 6, 1) + timedelta(days=i), 10.0 + 2 * i) for i in range(5)]
    _fig, ax = style.figure("t")
    trend = style.trend_annotation(ax, points)
    assert trend is not None and (round(trend.per_30_days, 1), round(trend.r, 3)) == (
        60.0,
        1.0,
    )


def test_trend_annotation_none_for_single_point() -> None:
    _fig, ax = style.figure("t")
    assert style.trend_annotation(ax, [(date(2026, 6, 1), 5.0)]) is None


def test_trend_annotation_box_shows_ci_and_p() -> None:
    # Reference dataset from test_stats: slope 0.6/day -> +18/30d, p=.124.
    ys = [2.0, 4.0, 5.0, 4.0, 5.0]
    points = [(date(2026, 1, 1 + i), y) for i, y in enumerate(ys)]
    _fig, ax = style.figure("t")
    style.trend_annotation(ax, points)
    box_text = next(t.get_text() for t in ax.texts if "trend" in t.get_text())
    assert "18" in box_text
    assert "p=.124" in box_text
    assert "n=5" in box_text


def test_trend_annotation_two_points_falls_back_to_slope_only() -> None:
    points = [(date(2026, 6, 1), 5.0), (date(2026, 6, 2), 7.0)]
    _fig, ax = style.figure("t")
    trend = style.trend_annotation(ax, points)
    assert trend is not None  # renders the old single-line format, no crash
    box_text = next(t.get_text() for t in ax.texts if "trend" in t.get_text())
    assert "r=" in box_text and "p=" not in box_text


def test_save_writes_png(tmp_path: Path) -> None:
    fig, _ax = style.figure("t")
    path = style.save(fig, tmp_path, "t.png")
    assert path.exists() and path.stat().st_size > 0


def test_sig_value_keeps_three_significant_digits() -> None:
    # Fraction-scale metrics (running economy 0.0133 m/s/W) must not collapse
    # to "0.0" the way a fixed {:.1f} did; large values need no decimals.
    assert [
        style.sig_value(v) for v in (0.013341, 0.964, 7.5, 52.34, 245.0, -0.05, 0.0)
    ] == ["0.0133", "0.964", "7.5", "52.3", "245", "-0.05", "0"]
