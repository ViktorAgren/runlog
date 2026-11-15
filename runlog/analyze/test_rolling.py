"""Unit tests for rolling windowed correlation."""

from __future__ import annotations

from datetime import date, timedelta

from runlog.analyze import rolling


def test_rolling_correlation_perfect_negative_with_lag() -> None:
    # driver on day d, marker on day d+1 is exactly -driver -> r = -1.
    base = date(2026, 1, 1)
    driver = [(base + timedelta(days=i), float(i % 5)) for i in range(40)]
    marker = [(base + timedelta(days=i + 1), -float(i % 5)) for i in range(40)]
    series = rolling.rolling_correlation(
        driver, marker, window_days=40, lag_days=1, min_points=10
    )
    assert series  # enough paired points
    assert series[-1][1] == -1.0


def test_rolling_correlation_empty_without_overlap() -> None:
    driver = [(date(2026, 1, 1), 1.0)]
    marker = [(date(2026, 6, 1), 1.0)]
    assert rolling.rolling_correlation(driver, marker) == []
