"""Unit tests for the mean-maximal speed curve and CS/D' fit."""

from __future__ import annotations

import pytest

from runlog.analyze import curves
from runlog.analyze.streams import StreamSample


def _sample(offset: int, distance: float) -> StreamSample:
    return StreamSample(
        offset_s=offset,
        distance_m=distance,
        altitude_m=None,
        lat=None,
        lng=None,
        hr=None,
        velocity_mps=None,
    )


def test_mean_maximal_speed_constant_velocity() -> None:
    stream = [_sample(i, 4.0 * i) for i in range(601)]  # 4 m/s for 600 s
    assert curves.mean_maximal_speed(stream, 300) == pytest.approx(4.0)


def test_mean_maximal_speed_none_when_window_too_long() -> None:
    stream = [_sample(i, 4.0 * i) for i in range(100)]
    assert curves.mean_maximal_speed(stream, 300) is None


def test_fit_recovers_known_cs_and_d_prime() -> None:
    # distance = CS*t + D'  ->  speed(t) = CS + D'/t.
    cs_mps, d_prime = 5.0, 200.0
    curve = [(t, cs_mps + d_prime / t) for t in (120, 180, 300, 600, 1200)]
    model = curves.fit(curve)
    assert model is not None
    assert (round(model.cs_mps, 6), round(model.d_prime_m, 6)) == (5.0, 200.0)
