"""Boundary tests for the report view-model tone helpers."""

from __future__ import annotations

import pytest

from runlog.analyze import report_model


@pytest.mark.parametrize(
    ("acwr", "tone"),
    [
        (1.6, "bad"),
        (1.5, "good"),  # the injury line is exclusive
        (1.0, "good"),
        (0.8, "good"),  # lower sweet-spot edge is inclusive
        (0.79, "warn"),
        (0.5, "warn"),
    ],
)
def test_acwr_tone(acwr: float, tone: str) -> None:
    assert report_model.acwr_tone(acwr) == tone


@pytest.mark.parametrize(
    ("tsb", "tone"),
    [
        (-25.0, "bad"),
        (-20.0, "neutral"),  # -20 is not yet "bad"
        (0.0, "neutral"),
        (5.0, "neutral"),  # 5 is not yet "good"
        (6.0, "good"),
        (10.0, "good"),
    ],
)
def test_form_tone(tsb: float, tone: str) -> None:
    assert report_model.form_tone(tsb) == tone
