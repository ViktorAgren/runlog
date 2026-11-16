"""Tests for the inline-SVG line chart used by the per-workout pages."""

from __future__ import annotations

from runlog.analyze.svg import line_chart


def test_line_chart_maps_points_to_a_polyline() -> None:
    svg = line_chart(
        [(0.0, 100.0), (1.0, 150.0), (2.0, 200.0)],
        title="Heart rate",
        unit="bpm",
        color="#dc2626",
        y_format="int",
    )
    assert svg.startswith('<svg class="chart" viewBox="0 0 760 240"')
    assert "Heart rate (bpm)" in svg
    assert 'stroke="#dc2626"' in svg
    # y maps bottom (min) to top (max); x maps left pad to right pad.
    assert 'd="M52.0 210.0 L399.0 117.0 L746.0 24.0"' in svg


def test_line_chart_breaks_the_path_on_gaps() -> None:
    svg = line_chart(
        [(0.0, 100.0), (1.0, None), (2.0, 120.0)],
        title="HR",
        unit="",
        color="#000",
        y_format="int",
    )
    # The None sample lifts the pen: two move commands, no line segment across it.
    assert 'd="M52.0 210.0 M746.0 24.0"' in svg


def test_line_chart_without_two_points_is_marked_empty() -> None:
    assert (
        line_chart([(0.0, None)], title="Cadence", unit="spm", color="#000")
        == '<div class="chart-empty">Cadence: no stream</div>'
    )
