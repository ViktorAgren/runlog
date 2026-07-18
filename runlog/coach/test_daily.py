"""Unit tests for the coaching-card assembly and guidance rule."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from runlog.coach import daily
from runlog.db import store
from runlog.domain import Activity, ActivityRecord, HealthMetric, SourceId, StreamPoint
from runlog.plan import schedule
from runlog.plan.progress import WorkoutDetail


class TestGuidance:
    @pytest.mark.parametrize(
        ("kwargs", "action", "reason_fragment"),
        [
            (
                {"planned_rest": True, "readiness_score": 80},
                "REST",
                "plan calls for rest",
            ),
            ({"red_flag": True, "readiness_score": 80}, "REST", "red flag"),
            ({"readiness_score": 30}, "REST", "well below normal"),
            ({"readiness_score": 40}, "EASY", "below normal band"),
            ({"readiness_score": 70, "tsb": -30}, "EASY", "deep fatigue"),
            ({"readiness_score": 70, "acwr": 1.6}, "EASY", "load spike"),
            (
                {"readiness_score": 50, "trimp_pctile": 95},
                "EASY",
                "big day yesterday",
            ),
            (
                {"readiness_score": 62, "tsb": -10, "acwr": 1.1},
                "GO",
                "recovered and absorbing",
            ),
            ({}, "EASY", "default easy"),  # all None
            ({"readiness_score": 50}, "EASY", "default easy"),  # mid, no green
        ],
    )
    def test_rule_table(
        self, kwargs: dict[str, object], action: str, reason_fragment: str
    ) -> None:
        base: dict[str, object] = {
            "readiness_score": None,
            "red_flag": False,
            "tsb": None,
            "acwr": None,
            "trimp_pctile": None,
            "planned_rest": False,
        }
        base.update(kwargs)
        result = daily.guidance_for(**base)  # type: ignore[arg-type]
        assert result.action == action
        assert any(reason_fragment in r for r in result.reasons)

    def test_rest_wins_over_easy_when_both_trigger(self) -> None:
        # Low readiness (REST) plus a load spike (EASY): REST is the action, but
        # both reasons are recorded.
        result = daily.guidance_for(
            readiness_score=30,
            red_flag=False,
            tsb=None,
            acwr=1.6,
            trimp_pctile=None,
            planned_rest=False,
        )
        assert result.action == "REST"
        assert any("load spike" in r for r in result.reasons)


def _detail(pace: float | None, hr: float | None) -> WorkoutDetail:
    return WorkoutDetail(
        day=date(2026, 7, 16),
        weekday="Thu",
        kind="Quality",
        distance_km=7.0,
        moving_s=2100,
        avg_pace_s_per_km=pace,
        avg_hr=hr,
        max_hr=181.0,
        easy_pct=41.0,
        moderate_pct=22.0,
        hard_pct=37.0,
        gap_pace_s_per_km=302.0,
        negative_split_pct=1.8,
        elevation_gain_m=40.0,
    )


def _session(
    pace: tuple[float | None, float | None], hr: tuple[int | None, int | None]
) -> schedule.PlannedSession:
    return schedule.PlannedSession(
        day=date(2026, 7, 16),
        weekday="Thu",
        kind="Tempo",
        pace_low_s=pace[0],
        pace_high_s=pace[1],
        hr_low=hr[0],
        hr_high=hr[1],
        pace_text="4:55-5:09/km",
        hr_text="172-179",
        description="tempo",
        is_rest=False,
        is_race=False,
    )


class TestCompareSession:
    @pytest.mark.parametrize(
        ("pace", "band", "expected"),
        [
            (306.0, (295.0, 309.0), "IN BAND"),
            (290.0, (295.0, 309.0), "FAST OF BAND"),
            (320.0, (295.0, 309.0), "SLOW OF BAND"),
            (306.0, (None, None), None),
        ],
    )
    def test_pace_verdict(
        self, pace: float, band: tuple[float | None, float | None], expected: str | None
    ) -> None:
        comparison = daily.compare_session(
            _detail(pace, 168.0), _session(band, (172, 179))
        )
        assert comparison.pace_verdict == expected

    @pytest.mark.parametrize(
        ("hr", "band", "expected"),
        [
            (175.0, (172, 179), "IN BAND"),
            (185.0, (172, 179), "ABOVE BAND"),
            (168.0, (172, 179), "BELOW BAND"),
            (175.0, (None, None), None),
        ],
    )
    def test_hr_verdict(
        self, hr: float, band: tuple[int | None, int | None], expected: str | None
    ) -> None:
        comparison = daily.compare_session(
            _detail(300.0, hr), _session((295.0, 309.0), band)
        )
        assert comparison.hr_verdict == expected


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(connection)
    return connection


def _add_run(
    conn: sqlite3.Connection, when: datetime, speed_mps: float, avg_hr: float
) -> None:
    seconds = int(6000 / speed_mps)
    stream = tuple(
        StreamPoint(
            offset_s=i, distance_m=speed_mps * i, hr=avg_hr, velocity_mps=speed_mps
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
                distance_m=6000.0,
                moving_s=seconds,
                avg_pace_s_per_km=1000 / speed_mps,
                avg_hr=avg_hr,
            ),
            stream=stream,
        ),
    )


_SCHEDULE_MD = """# Plan — Revised 2026-07-16

| Date | Day | Type | Pace | HR | Session |
|---|---|---|---|---|---|
| Jul 17 | Fri | Rest | — | — | Full rest |
| Aug 30 | Sun | RACE — 3 km | target 4:00/km | max | race |
"""


def test_build_today_uses_plan_rest_day(conn: sqlite3.Connection) -> None:
    _add_run(conn, datetime(2026, 7, 15, 7, tzinfo=UTC), 4.0, 150.0)
    sched = schedule.parse_schedule(_SCHEDULE_MD, Path("p.md"), date(2026, 7, 16))

    card = daily.build_today(conn, sched, date(2026, 7, 17), hr_max=193.0)
    assert card.session is not None and card.session.is_rest
    assert card.guidance.action == "REST"
    assert "plan calls for rest" in card.guidance.reasons


def test_build_today_without_schedule(conn: sqlite3.Connection) -> None:
    _add_run(conn, datetime(2026, 7, 15, 7, tzinfo=UTC), 4.0, 150.0)
    card = daily.build_today(conn, None, date(2026, 7, 17), hr_max=193.0)
    assert card.session is None
    assert card.forecast is None
    assert card.guidance.action in {"REST", "EASY", "GO"}


def test_build_last_none_without_runs(conn: sqlite3.Connection) -> None:
    assert daily.build_last(conn, None, date(2026, 7, 17)) is None


def test_build_last_pairs_run_date_with_plan(conn: sqlite3.Connection) -> None:
    # A run on Jul 17 must pair with the Jul 17 session, not today's (Jul 20).
    _add_run(conn, datetime(2026, 7, 17, 7, tzinfo=UTC), 4.0, 150.0)
    sched = schedule.parse_schedule(_SCHEDULE_MD, Path("p.md"), date(2026, 7, 16))

    card = daily.build_last(conn, sched, date(2026, 7, 20), hr_max=193.0)
    assert card is not None
    assert card.comparison is not None
    assert card.comparison.session.day == date(2026, 7, 17)


def test_build_today_forecast_and_health(conn: sqlite3.Connection) -> None:
    # A short block of runs plus resting-HR data -> a populated card with a
    # race forecast (from the plan's race row) and a resolved TSB.
    for i in range(20):
        _add_run(
            conn, datetime(2026, 7, 1, 7, tzinfo=UTC) + timedelta(days=i), 4.0, 150.0
        )
    store.insert_health_metrics(
        conn,
        [
            HealthMetric(
                "resting_hr", datetime(2026, 7, 1, tzinfo=UTC) + timedelta(days=i), 53.0
            )
            for i in range(20)
        ],
    )
    sched = schedule.parse_schedule(_SCHEDULE_MD, Path("p.md"), date(2026, 7, 16))

    card = daily.build_today(conn, sched, date(2026, 7, 21), hr_max=193.0)
    assert card.forecast is not None
    assert card.forecast.distance_m == 3000.0
    assert card.tsb is not None
