"""Unit tests for the dated markdown schedule parser."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from runlog.plan import schedule

_PLAN_SNIPPET = """# 3k Training Plan — Revised 2026-07-16 (mid-block update)

## Training zones (current — supersede the old table)

| Zone | Pace | HR | RPE | Purpose |
|---|---|---|---|---|
| Recovery | 6:25–7:08/km | 123–137 | 2–3 | Easy shakeout / recovery |
| Interval | 4:26–4:37/km | 182–193 | 8–9 | VO2max repeats |

## Week 3

| Date | Day | Type | Pace | HR | Session |
|---|---|---|---|---|---|
| Jul 17 | Fri | Rest | — | — | Full rest |
| Jul 23 | Thu | Intervals (VO2) | 4:26–4:37/km | 182–193 | WU 2 km + 5×1000 m, 400 m jog + CD · 9 km |
| Jul 26 | Sun | Reps + GRP intro | Rep 4:04–4:15 / GRP 4:00 | by feel | WU 2 km · 6×400 m @ Rep · 8 km |
| Aug 30 | Sun | RACE — 3 km | target 4:00/km | max | km1 ~4:06 · km2 ~4:02 · km3 all-in |
"""


def _parse(text: str = _PLAN_SNIPPET) -> schedule.PlanSchedule:
    return schedule.parse_schedule(text, Path("plan.md"), date(2026, 7, 16))


def test_parses_dated_session_row() -> None:
    session = _parse().session_on(date(2026, 7, 23))
    assert session == schedule.PlannedSession(
        day=date(2026, 7, 23),
        weekday="Thu",
        kind="Intervals (VO2)",
        pace_low_s=266.0,
        pace_high_s=277.0,
        hr_low=182,
        hr_high=193,
        pace_text="4:26–4:37/km",
        hr_text="182–193",
        description="WU 2 km + 5×1000 m, 400 m jog + CD · 9 km",
        is_rest=False,
        is_race=False,
    )


def test_rest_row() -> None:
    session = _parse().session_on(date(2026, 7, 17))
    assert session is not None
    assert (
        session.is_rest,
        session.pace_low_s,
        session.hr_low,
        session.description,
    ) == (True, None, None, "Full rest")


def test_race_row_and_distance() -> None:
    parsed = _parse()
    race = parsed.race
    assert race is not None
    assert (race.is_race, race.day, race.pace_low_s, race.pace_high_s) == (
        True,
        date(2026, 8, 30),
        240.0,
        240.0,
    )
    assert (race.hr_low, race.hr_text) == (None, "max")
    assert parsed.race_distance_m == 3000.0


def test_compound_pace_takes_first_band() -> None:
    session = _parse().session_on(date(2026, 7, 26))
    assert session is not None
    assert (session.pace_low_s, session.pace_high_s) == (244.0, 255.0)
    assert (session.hr_low, session.hr_text) == (None, "by feel")


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("4:26–4:37/km", (266.0, 277.0)),
        ("4:26-4:37/km", (266.0, 277.0)),  # plain hyphen
        ("target 4:00/km", (240.0, 240.0)),
        ("Rep 4:04–4:15 / GRP 4:00", (244.0, 255.0)),
        ("by feel", (None, None)),
        ("—", (None, None)),
        ("", (None, None)),
    ],
)
def test_pace_cell_grammar(
    cell: str, expected: tuple[float | None, float | None]
) -> None:
    assert schedule._parse_pace(cell) == expected


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("182–193", (182, 193)),
        ("172-179", (172, 179)),
        ("max", (None, None)),
        ("by feel", (None, None)),
        ("—", (None, None)),
    ],
)
def test_hr_cell_grammar(cell: str, expected: tuple[int | None, int | None]) -> None:
    assert schedule._parse_hr(cell) == expected


def test_year_rollover_for_december_plan() -> None:
    text = (
        "# Plan — Revised 2026-12-20\n\n"
        "| Date | Day | Type | Pace | HR | Session |\n"
        "|---|---|---|---|---|---|\n"
        "| Dec 22 | Tue | Easy | 5:39–6:25/km | 144–162 | 6 km |\n"
        "| Jan 4 | Mon | Easy | 5:39–6:25/km | 144–162 | 6 km |\n"
    )
    parsed = schedule.parse_schedule(text, Path("p.md"), date(2026, 12, 20))
    assert [s.day for s in parsed.sessions] == [date(2026, 12, 22), date(2027, 1, 4)]


def test_zone_table_and_headers_are_skipped() -> None:
    # The snippet contains the zones table and header/separator rows; only the
    # four dated session rows must parse.
    assert len(_parse().sessions) == 4


def test_undated_legacy_plan_yields_empty_schedule() -> None:
    text = (
        "| Day | Type | Venue | Pace | HR | Session |\n"
        "|---|---|---|---|---|---|\n"
        "| Mon | Rest | — | — | — | Full rest |\n"
    )
    parsed = schedule.parse_schedule(text, Path("plan.md"), date(2026, 7, 16))
    assert parsed.sessions == ()


def test_find_active_plan_picks_newest_mtime(tmp_path: Path) -> None:
    old = tmp_path / "plan-old.md"
    new = tmp_path / "plan-new.md"
    old.write_text("x")
    new.write_text("y")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    assert schedule.find_active_plan(tmp_path) == new


def test_find_active_plan_none_when_empty(tmp_path: Path) -> None:
    assert schedule.find_active_plan(tmp_path) is None
