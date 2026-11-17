"""Unit tests for the deterministic limiter diagnosis.

The ranking is the load-bearing logic: it decides what the athlete trains next.
These exercise the pure ``diagnose`` with synthetic signals and assert the full
ranked axes against hand-computed severities, plus the data-sufficiency fallback.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from runlog.analyze.cs import CsModel, CsPoint
from runlog.coach import render
from runlog.coach.limiters import (
    DiagnoseInput,
    LimiterAxis,
    _residuals,
    build_limiters,
    diagnose,
)
from runlog.db import store
from runlog.domain import Activity, ActivityRecord, SourceId, StreamPoint

# A clean line: predict_seconds(d) = (d - 200) / 5, fit over 1k-5k.
_CS = CsModel(
    cs_mps=5.0,
    d_prime_m=200.0,
    r=0.99,
    points=[CsPoint(1000.0, 160.0), CsPoint(5000.0, 960.0)],
)
_ON_CURVE = ((1000.0, 160.0), (2000.0, 360.0), (5000.0, 960.0))

_BENIGN: dict[str, object] = {
    "vdot": 60.0,
    "vo2max": 60.0,
    "engine_trend_per_month": 0.5,  # rising -> engine not limiting
    "cs_model": _CS,
    "efforts": _ON_CURVE,
    "durability_fade_pct": 3.0,  # below 4 -> no flag
    "decoupling_pct": 4.0,  # below 5 -> no flag
    "fatigue_k": 1.05,  # below 1.06 -> no flag
    "easy_pct": 82.0,  # at/above Seiler 80 -> no flag
    "polarization_index": 2.3,
    "monotony": 1.5,  # below 2.0 -> no flag
    "acwr": 1.1,  # inside 0.8-1.3 -> no flag
    "runs_per_week": 5.0,
    "longest_layoff_days": 5,
}


def _input(**overrides: object) -> DiagnoseInput:
    data = {**_BENIGN, **overrides}
    return DiagnoseInput(**data)  # type: ignore[arg-type]


def _ranked(card_findings: object) -> list[tuple[LimiterAxis, float]]:
    assert isinstance(card_findings, tuple)
    return [(f.axis, f.severity) for f in card_findings]


@pytest.mark.parametrize(
    ("overrides", "primary", "confidence", "ranked"),
    [
        (
            {
                "engine_trend_per_month": 0.5,
                "efforts": ((1000.0, 168.0), (2000.0, 360.0), (5000.0, 960.0)),
            },
            "speed",
            "high",
            [("speed", 2.0), ("engine", 0.0), ("durability", 0.0), ("structure", 0.0)],
        ),
        (
            {"engine_trend_per_month": -0.8},
            "engine",
            "high",
            [("engine", 3.0), ("durability", 0.0), ("speed", 0.0), ("structure", 0.0)],
        ),
        (
            {"durability_fade_pct": 10.0, "decoupling_pct": 8.0, "fatigue_k": 1.10},
            "durability",
            "high",
            [("durability", 2.0), ("engine", 0.0), ("speed", 0.0), ("structure", 0.0)],
        ),
        (
            {"easy_pct": 66.0, "monotony": 2.2},
            "structure",
            "high",
            [("structure", 2.0), ("engine", 0.0), ("durability", 0.0), ("speed", 0.0)],
        ),
    ],
)
def test_diagnose_ranks_the_dominant_axis(
    overrides: dict[str, object],
    primary: LimiterAxis,
    confidence: str,
    ranked: list[tuple[LimiterAxis, float]],
) -> None:
    card = diagnose(_input(**overrides), today=date(2026, 11, 17))

    assert (card.primary, card.confidence, _ranked(card.findings)) == (
        primary,
        confidence,
        ranked,
    )


def test_diagnose_sparse_data_falls_back_to_low_confidence() -> None:
    card = diagnose(
        _input(
            cs_model=None,
            efforts=(),
            engine_trend_per_month=None,
            durability_fade_pct=10.0,
            easy_pct=66.0,
        ),
        today=date(2026, 11, 17),
    )

    assert (
        card.primary,
        card.confidence,
        card.confidence_note,
        card.short_gap_pct,
    ) == (
        "durability",
        "low",
        "limited by missing best efforts at 3+ distances "
        "and 2+ critical-speed windows for the engine trend",
        None,
    )


def test_diagnose_two_distances_cannot_localize_speed() -> None:
    card = diagnose(
        _input(efforts=((1000.0, 168.0), (5000.0, 960.0))),
        today=date(2026, 11, 17),
    )
    speed = next(f for f in card.findings if f.axis == "speed")

    assert (speed.severity, card.confidence, card.short_gap_pct) == (0.0, "low", None)


def test_residuals_sign_tracks_under_and_over_performance() -> None:
    faster = _residuals(_CS, ((1000.0, 152.0),))  # 8s faster than 160 predicted
    slower = _residuals(_CS, ((1000.0, 168.0),))  # 8s slower

    assert [round(r, 1) for _, r in faster + slower] == [-5.0, 5.0]


def test_residuals_ignore_efforts_outside_the_cs_fit_range() -> None:
    model = CsModel(
        cs_mps=3.8,
        d_prime_m=90.0,
        r=1.0,
        points=[CsPoint(400.0, 80.0), CsPoint(5000.0, 1280.0)],
    )
    residuals = _residuals(model, ((200.0, 35.0), (1000.0, 240.0), (10000.0, 3100.0)))

    assert [d for d, _ in residuals] == [1000.0]


def test_engine_finding_flags_under_expression_gap() -> None:
    card = diagnose(
        _input(vdot=46.0, vo2max=61.0, engine_trend_per_month=0.5),
        today=date(2026, 11, 17),
    )
    engine = next(f for f in card.findings if f.axis == "engine")

    assert engine.severity == 0.0 and any(
        "under-expressed" in r for r in engine.reasons
    )


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(connection)
    return connection


def _seed(
    conn: sqlite3.Connection, when: datetime, distance_m: float, speed_mps: float
) -> None:
    seconds = int(distance_m / speed_mps)
    stream = tuple(
        StreamPoint(
            offset_s=i, distance_m=speed_mps * i, hr=150.0, velocity_mps=speed_mps
        )
        for i in range(seconds + 1)
    )
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source="strava",
                source_id=SourceId(when.isoformat()),
                sport_type="Run",
                start_time_utc=when,
                distance_m=distance_m,
                moving_s=seconds,
                avg_pace_s_per_km=1000 / speed_mps,
                avg_hr=150.0,
            ),
            stream=stream,
        ),
    )


def test_build_limiters_on_seeded_runs_produces_all_four_axes(
    conn: sqlite3.Connection,
) -> None:
    base = datetime(2026, 10, 5, 7, tzinfo=UTC)
    for week in range(3):
        for offset, (dist, speed) in enumerate(
            [(6000.0, 3.3), (12000.0, 2.9), (5000.0, 3.6)]
        ):
            _seed(conn, base + timedelta(days=week * 7 + offset * 2), dist, speed)

    card = build_limiters(conn, hr_max=190.0, hr_rest=50.0, today=date(2026, 11, 17))

    assert {f.axis for f in card.findings} == {
        "engine",
        "speed",
        "durability",
        "structure",
    }
    assert card.primary in {"engine", "speed", "durability", "structure"}
    assert card.confidence in {"high", "low"}
    assert render.render_limiters(card).startswith("LIMITERS —")
