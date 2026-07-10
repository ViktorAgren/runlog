"""Unit tests for stream-based physiology metrics."""

from __future__ import annotations

import math

import pytest

from runlog.analyze import physiology
from runlog.analyze.physiology import IntensityDistribution
from runlog.analyze.streams import StreamSample


def _hr(offset: int, hr: float | None, vel: float | None = None) -> StreamSample:
    return StreamSample(offset, float(offset), None, None, None, hr, vel)


def test_zone_seconds_weights_by_dt_and_buckets_by_fraction() -> None:
    # hr_max 200: 120 bpm = 0.60 -> Z2; 180 bpm = 0.90 -> Z5. Each sample spans
    # 10 s to the next; the final sample has no successor so it is not counted.
    stream = [_hr(0, 120.0), _hr(10, 120.0), _hr(20, 180.0), _hr(30, 180.0)]
    assert physiology.zone_seconds_for_run(stream, hr_max=200.0) == [
        0.0,
        20.0,  # two 10 s spans at 0.60
        0.0,
        0.0,
        10.0,  # one 10 s span at 0.90
    ]


def test_intensity_distribution_percentages_and_ratio() -> None:
    # Z1..Z5 seconds: easy=Z1+Z2=800, moderate=Z3=100, hard=Z4+Z5=100.
    assert physiology.intensity_distribution([500, 300, 100, 60, 40]) == (
        IntensityDistribution(
            easy_pct=80.0, moderate_pct=10.0, hard_pct=10.0, easy_to_hard=8.0
        )
    )


def test_stream_trimp_integrates_reserve_over_samples() -> None:
    # Steady 60 s at HR 150, reserve = (150-50)/(200-50) = 2/3.
    stream = [_hr(0, 150.0), _hr(60, 150.0)]
    reserve = 2 / 3
    expected = (60 / 60) * reserve * 0.64 * math.exp(1.92 * reserve)
    assert physiology.stream_trimp(stream, hr_max=200.0, hr_rest=50.0) == pytest.approx(
        round(expected, 1)
    )


def test_cardiac_drift_positive_when_efficiency_falls() -> None:
    # Constant speed 3 m/s but HR climbing 150 -> 165 over the run: speed/HR
    # falls, so drift is positive.
    stream = [_hr(i * 60, 150.0 + i * 5, vel=3.0) for i in range(4)]
    drift = physiology.cardiac_drift_pct(stream)
    assert drift is not None and drift > 0


def test_cardiac_drift_none_without_enough_paired_samples() -> None:
    assert physiology.cardiac_drift_pct([_hr(0, 150.0, vel=3.0)]) is None
