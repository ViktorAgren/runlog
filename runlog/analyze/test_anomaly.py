"""Unit tests for rolling-baseline anomaly detection."""

from __future__ import annotations

from datetime import date, timedelta

from runlog.analyze import anomaly
from runlog.analyze.anomaly import Anomaly, Direction, RedFlagDay


def _steady(start: date, value: float, days: int) -> list[tuple[date, float]]:
    """A flat baseline series of ``days`` identical readings."""
    return [(start + timedelta(days=i), value) for i in range(days)]


# Baseline alternating 48/52: median 50, MAD 2, so robust sigma = 1.4826*2.
def _wobble_baseline() -> list[tuple[date, float]]:
    return [
        (date(2026, 1, 1) + timedelta(days=i), 48.0 + (i % 2) * 4.0) for i in range(20)
    ]


def test_detect_series_flags_high_spike_after_stable_baseline() -> None:
    spike_day = date(2026, 1, 1) + timedelta(days=20)
    series = [*_wobble_baseline(), (spike_day, 65.0)]

    # (65 - 50) / (1.4826 * 2) = 5.06 robust sigmas above the baseline median.
    assert anomaly.detect_series(series, "resting_hr", Direction.HIGH) == [
        Anomaly(
            day=spike_day,
            metric="resting_hr",
            value=65.0,
            baseline=50.0,
            deviation=5.06,
            direction=Direction.HIGH,
        )
    ]


def test_detect_series_ignores_spike_in_the_unwatched_direction() -> None:
    # A LOW-direction detector must not flag an upward spike.
    series = [*_wobble_baseline(), (date(2026, 1, 21), 65.0)]

    assert anomaly.detect_series(series, "resting_hr", Direction.LOW) == []


def test_detect_series_skips_days_without_enough_baseline() -> None:
    # Only three prior readings -> below the minimum baseline, nothing judged.
    series = _steady(date(2026, 1, 1), 50.0, 3) + [(date(2026, 1, 4), 90.0)]

    assert anomaly.detect_series(series, "resting_hr", Direction.HIGH) == []


def test_red_flag_days_requires_two_distinct_signals() -> None:
    flag_day = date(2026, 2, 10)
    other_day = date(2026, 2, 11)
    anomalies = [
        Anomaly(flag_day, "resting_hr", 62.0, 50.0, 4.0, Direction.HIGH),
        Anomaly(flag_day, "hrv_sdnn", 30.0, 60.0, -4.0, Direction.LOW),
        Anomaly(other_day, "spo2", 0.90, 0.97, -3.0, Direction.LOW),
    ]

    assert anomaly.red_flag_days(anomalies) == [
        RedFlagDay(day=flag_day, metrics=("hrv_sdnn", "resting_hr"))
    ]
