"""Unit tests for within-run durability (efficiency fade)."""

from __future__ import annotations

import pytest

from runlog.analyze import durability
from runlog.analyze.streams import StreamSample


def _sample(offset: int, distance: float, hr: float) -> StreamSample:
    return StreamSample(
        offset_s=offset,
        distance_m=distance,
        altitude_m=None,
        lat=None,
        lng=None,
        hr=hr,
        velocity_mps=None,
    )


def _fade_stream() -> list[StreamSample]:
    # 900 s run at constant HR 150: 4 m/s for the first two thirds, 3 m/s in the
    # last third. Fresh EF = 4/150, fatigued EF = 3/150 -> 25% decline.
    stream = []
    distance = 0.0
    for i in range(901):
        speed = 4.0 if i <= 600 else 3.0
        if i > 0:
            distance += speed
        stream.append(_sample(i, distance, 150.0))
    return stream


def test_run_durability_measures_efficiency_fade() -> None:
    assert durability.run_durability(_fade_stream()) == pytest.approx(25.0)


def test_run_durability_none_without_hr() -> None:
    stream = [
        StreamSample(
            offset_s=i,
            distance_m=float(i) * 4,
            altitude_m=None,
            lat=None,
            lng=None,
            hr=None,
            velocity_mps=None,
        )
        for i in range(901)
    ]
    assert durability.run_durability(stream) is None
