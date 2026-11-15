"""Unit tests for the derived-metrics series types."""

from __future__ import annotations

from datetime import date

from runlog.analyze.series import Series, from_trends


def test_from_trends_builds_series_and_drops_empty() -> None:
    trends: list[tuple[str, str, str, list[tuple[date, float]]]] = [
        ("ctl", "training", "load", [(date(2026, 1, 1), 10.0)]),
        ("empty", "recovery", "ms", []),
    ]
    assert from_trends(trends) == [  # type: ignore[arg-type]
        Series(
            name="ctl",
            lane="training",
            unit="load",
            points=[(date(2026, 1, 1), 10.0)],
        )
    ]
