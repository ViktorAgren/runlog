"""Tests for the inline-SVG line chart used by the per-workout pages."""

from __future__ import annotations

from runlog.analyze.svg import _float_decimals, _y_range, line_chart


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
    # y maps bottom (min) to top (max), with a margin so the extremes sit inside
    # the frame; x maps left pad to right pad.
    assert 'd="M52.0 200.0 L399.0 117.0 L746.0 34.0"' in svg


def test_line_chart_breaks_the_path_on_gaps() -> None:
    svg = line_chart(
        [(0.0, 100.0), (1.0, None), (2.0, 120.0)],
        title="HR",
        unit="",
        color="#000",
        y_format="int",
    )
    # The None sample lifts the pen: two move commands, no line segment across it.
    assert 'd="M52.0 200.0 M746.0 34.0"' in svg


def test_line_chart_without_two_points_is_marked_empty() -> None:
    assert (
        line_chart([(0.0, None)], title="Cadence", unit="spm", color="#000")
        == '<div class="chart-empty">Cadence: no stream</div>'
    )


def test_y_range_spans_full_data_with_margin_and_floor() -> None:
    assert [
        _y_range([100.0] * 50 + [1000.0], min_span=0.0),  # full range, nothing clipped
        _y_range([360.0] * 100, min_span=60.0),  # flat series held to the floor
        _y_range([100.0, 150.0, 200.0], min_span=0.0),  # spread + 6% margin
    ] == [(46.0, 1054.0), (330.0, 390.0), (94.0, 206.0)]


def test_line_chart_axis_spans_a_steady_series_and_its_spike() -> None:
    # 100 samples at 360 s/km then one 900 s/km stop: the axis spans the full
    # range, so the steady line sits at the bottom and the spike is drawn near
    # the top (not clipped), with the top tick above the 900 peak.
    points: list[tuple[float, float | None]] = [(float(i), 360.0) for i in range(100)]
    points.append((100.0, 900.0))
    svg = line_chart(
        points, title="Pace", unit="min/km", color="#2563eb", min_span=60.0
    )
    assert 'd="M52.0 200.0' in svg  # steady 360 near the bottom
    assert 'L746.0 34.0" fill="none"' in svg  # the 900 spike is shown, not clipped
    labels = [s.split("</text>")[0] for s in svg.split('class="ylab">')[1:]]
    assert labels == ["328", "630", "932"]  # top tick clears the 900 peak


def test_y_range_shows_variation_above_a_small_floor() -> None:
    assert _y_range([300.0, 320.0, 340.0, 360.0, 380.0], 12.0) == (295.2, 384.8)


def test_y_range_floors_a_near_constant_series() -> None:
    low, high = _y_range([300.0, 300.0, 300.0, 301.0], 12.0)
    assert round(high - low, 1) == 12.0


def test_float_decimals_separates_narrow_labels() -> None:
    assert [
        _float_decimals([0.90, 0.965, 1.03]),  # one decimal collides on 1.0
        _float_decimals([100.0, 150.0, 200.0]),
    ] == [2, 1]


def test_line_chart_float_axis_labels_are_distinct() -> None:
    points: list[tuple[float, float | None]] = [
        (float(i), 0.9 + 0.003 * i) for i in range(50)
    ]
    svg = line_chart(
        points, title="Stride", unit="m", color="#0d9488", y_format="float"
    )
    labels = [seg.split("</text>")[0] for seg in svg.split('class="ylab">')[1:]]
    assert len(set(labels)) == 3  # no repeated '1.0' ticks
