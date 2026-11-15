"""Unit tests for the derived-metrics series types."""

from __future__ import annotations

from datetime import date

import pytest

from runlog.analyze.series import Series, from_trends


def test_from_trends_builds_series_and_drops_empty() -> None:
    trends: list[tuple[str, str, str, list[tuple[date, float]]]] = [
        ("ctl", "training", "load", [(date(2026, 1, 1), 10.0)]),
        ("empty", "recovery", "ms", []),
    ]
    assert from_trends(trends) == [
        Series(
            name="ctl",
            lane="training",
            unit="load",
            points=[(date(2026, 1, 1), 10.0)],
        )
    ]


def test_from_trends_rejects_unknown_lane() -> None:
    with pytest.raises(ValueError, match="unknown series lane"):
        from_trends([("x", "bogus", "u", [(date(2026, 1, 1), 1.0)])])
