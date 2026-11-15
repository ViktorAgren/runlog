"""Formal types for the derived-metrics layer.

The analysis code has long produced dated scalar series as bare
``list[tuple[date, float]]`` and the report accumulated them as an informal
``(label, lane, unit, points)`` 4-tuple. :class:`Series` and :class:`Scalar`
give those a name so they can be persisted, exported, and served uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

# The analytic "lane" a series belongs to, mirroring the report's grouping.
Lane = Literal["training", "recovery", "lifestyle", "energy", "cross"]


@dataclass(frozen=True)
class Series:
    """A named dated-scalar timeseries in a given analytic lane."""

    name: str
    lane: Lane
    unit: str
    points: list[tuple[date, float]]


@dataclass(frozen=True)
class Scalar:
    """A single named derived value (e.g. a current KPI)."""

    name: str
    value: float
    unit: str


def from_trends(
    trends: Sequence[tuple[str, Lane, str, list[tuple[date, float]]]],
) -> list[Series]:
    """Build :class:`Series` from the report's ``(name, lane, unit, points)`` tuples.

    Empty series are dropped, so only series with data are persisted.
    """
    return [
        Series(name=name, lane=lane, unit=unit, points=points)
        for name, lane, unit, points in trends
        if points
    ]
