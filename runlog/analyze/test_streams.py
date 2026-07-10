"""Unit tests for per-run stream analysis (GAP, climb, pacing, routes)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from runlog.analyze import streams
from runlog.analyze.metrics import Run
from runlog.analyze.streams import ClimbStats, PacingStats, RouteGroup, StreamSample
from runlog.domain import ActivityId


def _sample(offset: int, dist: float, alt: float | None = None) -> StreamSample:
    return StreamSample(
        offset_s=offset,
        distance_m=dist,
        altitude_m=alt,
        lat=None,
        lng=None,
        hr=None,
        velocity_mps=None,
    )


@pytest.mark.parametrize(
    ("grade", "expected"),
    [(0.0, 1.0), (0.10, 1.658), (-0.10, 0.598)],
)
def test_grade_adjust_factor_matches_minetti(grade: float, expected: float) -> None:
    assert streams.grade_adjust_factor(grade) == pytest.approx(expected, abs=0.01)


def test_grade_adjusted_pace_equals_raw_pace_on_flat() -> None:
    # 100 m at 2 m/s (500 s/km), dead flat -> GAP == raw pace == 500 s/km.
    flat = [_sample(d // 2, float(d), alt=42.0) for d in range(0, 101, 10)]
    assert streams.grade_adjusted_pace_s_per_km(flat) == pytest.approx(500.0)


def test_grade_adjusted_pace_is_faster_than_raw_uphill() -> None:
    # 100 m at 2 m/s climbing 1 m per 10 m (10% grade): GAP credits the effort,
    # so flat-equivalent pace is faster (smaller s/km) than the raw 500 s/km.
    uphill = [_sample(d // 2, float(d), alt=float(d) / 10) for d in range(0, 101, 10)]
    gap = streams.grade_adjusted_pace_s_per_km(uphill)
    assert gap == pytest.approx(500.0 / streams.grade_adjust_factor(0.10), abs=1.0)


def test_grade_adjusted_pace_rejects_corrupted_stream() -> None:
    # 50 m of distance spread over 100000 s (a paused workout): the implied pace
    # is absurd, so it is dropped rather than skewing the trend.
    corrupted = [_sample(0, 0.0, alt=10.0), _sample(100_000, 50.0, alt=10.0)]
    assert streams.grade_adjusted_pace_s_per_km(corrupted) is None


def test_climb_stats_sums_ascent_and_rate() -> None:
    # 100 m climbing 10 m total in 50 s -> VAM 10 m / (50/3600 h) = 720 m/h.
    uphill = [_sample(d // 2, float(d), alt=float(d) / 10) for d in range(0, 101, 10)]
    assert streams.climb_stats(uphill) == ClimbStats(
        ascent_m=10.0, descent_m=0.0, vam_m_per_h=720.0, longest_climb_m=10.0
    )


def test_pacing_stats_detects_negative_split() -> None:
    # First km at 1 m/s (1000 s), second km at 2 m/s (500 s).
    first_km = [_sample(d, float(d)) for d in range(0, 1001, 100)]
    second_km = [_sample(1000 + i * 50, 1000.0 + i * 100) for i in range(1, 11)]
    assert streams.pacing_stats([*first_km, *second_km]) == PacingStats(
        even_cv=0.333, negative_split_pct=50.0
    )


def _geo(lat: float, lng: float, dist: float) -> StreamSample:
    return StreamSample(0, dist, None, lat, lng, None, None)


def test_route_signature_collides_for_same_start_and_distance() -> None:
    a = [_geo(59.3251, 18.0711, 0.0), _geo(59.3260, 18.0720, 5040.0)]
    b = [_geo(59.3253, 18.0709, 0.0), _geo(59.3299, 18.0777, 4980.0)]
    far = [_geo(59.4000, 18.2000, 0.0), _geo(59.4100, 18.2100, 5010.0)]
    assert streams.route_signature(a) == streams.route_signature(b)
    assert streams.route_signature(a) != streams.route_signature(far)


def _run(day: int, pace: float | None) -> Run:
    return Run(
        activity_id=ActivityId(day),
        source="strava",
        start=datetime(2026, 6, day, tzinfo=UTC),
        distance_m=5000.0,
        moving_s=1500,
        avg_pace_s_per_km=pace,
        avg_hr=None,
        max_hr=None,
    )


def test_rank_routes_keeps_frequent_routes_in_date_order() -> None:
    home: streams.RouteSig = (59.325, 18.071, 5)
    other: streams.RouteSig = (59.400, 18.200, 5)
    labelled = [
        (_run(1, 320.0), home),
        (_run(2, 315.0), home),
        (_run(3, 310.0), home),
        (_run(4, 300.0), other),  # only one run on `other`
        (_run(5, None), home),  # no pace -> excluded from the series
    ]
    assert streams._rank_routes(labelled, min_runs=3, top_n=3) == [
        RouteGroup(
            label="~5 km near (59.325, 18.071)",
            count=4,
            paces=[
                (_run(1, 0.0).start.date(), 320.0),
                (_run(2, 0.0).start.date(), 315.0),
                (_run(3, 0.0).start.date(), 310.0),
            ],
        )
    ]
