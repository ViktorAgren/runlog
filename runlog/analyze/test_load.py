"""Unit tests for training-load structure (monotony, strain, ramp)."""

from __future__ import annotations

from datetime import date, timedelta

from runlog.analyze import load

# Two complete Monday-start weeks once gap-filled:
#   wk1 (Jan 5):  [120, 0, 60, 0, 0, 0, 0]  sum 180, mean 25.714, pstdev 43.706
#   wk2 (Jan 12): [100, 0, 0, 50, 0, 0, 60] sum 210, mean 30.0,  pstdev 37.417
_DAILY: list[tuple[date, float]] = [
    (date(2026, 1, 5), 120.0),
    (date(2026, 1, 7), 60.0),
    (date(2026, 1, 12), 100.0),
    (date(2026, 1, 15), 50.0),
    (date(2026, 1, 18), 60.0),
]


def test_monotony_series_is_mean_over_sd_per_week() -> None:
    assert load.monotony_series(_DAILY) == [
        (date(2026, 1, 5), 0.588),
        (date(2026, 1, 12), 0.802),
    ]


def test_strain_series_is_weekly_load_times_monotony() -> None:
    assert load.strain_series(_DAILY) == [
        (date(2026, 1, 5), 105.9),
        (date(2026, 1, 12), 168.4),
    ]


def test_ramp_series_is_week_over_week_percent_change() -> None:
    assert load.ramp_series(_DAILY) == [(date(2026, 1, 12), 16.7)]


def test_monotony_is_none_for_a_flat_week() -> None:
    week = [(date(2026, 1, 5) + timedelta(days=i), 50.0) for i in range(7)]
    assert load.monotony_series(week) == []
