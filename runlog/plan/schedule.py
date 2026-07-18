"""Parse the dated session tables out of a training-plan markdown file.

Plans rendered since the 2026-07-16 revision carry per-week tables in the
form ``| Date | Day | Type | Pace | HR | Session |`` with rows like
``| Jul 23 | Thu | Intervals (VO2) | 4:26-4:37/km | 182-193 | WU 2 km ... |``.
This module turns those rows into typed sessions so the daily coach commands
can look up "what does the plan say for today?". Older plans with undated
(Day-only) tables simply yield an empty schedule — callers degrade to cards
without the plan comparison.

Duplicate dates keep the last row seen (a revision overriding an earlier
table in the same file).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}
_DAY_CELL = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2})$")
_PACE_BAND = re.compile(r"(\d+):(\d{2})\s*-\s*(\d+):(\d{2})")
_PACE_SINGLE = re.compile(r"(\d+):(\d{2})\s*/km")
_HR_BAND = re.compile(r"(\d{2,3})\s*-\s*(\d{2,3})")
_RACE_DISTANCE = re.compile(r"RACE\s*[—-]\s*([\d.]+)\s*km")
_REVISED = re.compile(r"Revised (\d{4}-\d{2}-\d{2})")
_ISO_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
# A session dated further than this before the plan date belongs to next year.
_ROLLOVER_DAYS = 180


@dataclass(frozen=True)
class PlannedSession:
    day: date
    weekday: str  # as written, e.g. "Thu"
    kind: str  # Type cell verbatim, e.g. "Intervals (VO2)"
    pace_low_s: float | None  # fast bound of the band, s/km
    pace_high_s: float | None  # slow bound; equals low for single targets
    hr_low: int | None
    hr_high: int | None
    pace_text: str  # raw cell, for "by feel" / "—" display
    hr_text: str
    description: str
    is_rest: bool
    is_race: bool


@dataclass(frozen=True)
class PlanSchedule:
    path: Path
    plan_date: date
    sessions: tuple[PlannedSession, ...]  # date-sorted

    def session_on(self, day: date) -> PlannedSession | None:
        return next((s for s in self.sessions if s.day == day), None)

    @property
    def race(self) -> PlannedSession | None:
        return next((s for s in self.sessions if s.is_race), None)

    @property
    def race_distance_m(self) -> float | None:
        race = self.race
        if race is None:
            return None
        match = _RACE_DISTANCE.search(race.kind)
        return float(match.group(1)) * 1000 if match else None


def find_active_plan(plans_dir: Path) -> Path | None:
    """The most recently modified plan markdown, or None when there is none."""
    candidates = sorted(
        plans_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


def load_schedule(path: Path) -> PlanSchedule:
    fallback = date.fromtimestamp(path.stat().st_mtime)
    return parse_schedule(path.read_text(encoding="utf-8"), path, fallback)


def _normalize_dashes(cell: str) -> str:
    return cell.replace("–", "-").replace("—", "-").replace("−", "-")


def _parse_pace(cell: str) -> tuple[float | None, float | None]:
    """(fast_s, slow_s) per km from a pace cell; (None, None) for no band."""
    text = _normalize_dashes(cell).strip()
    if not text or text == "-" or "by feel" in text.lower():
        return None, None
    band = _PACE_BAND.search(text)
    if band:
        low = int(band.group(1)) * 60 + int(band.group(2))
        high = int(band.group(3)) * 60 + int(band.group(4))
        return float(low), float(high)
    single = _PACE_SINGLE.search(text)
    if single:
        seconds = float(int(single.group(1)) * 60 + int(single.group(2)))
        return seconds, seconds
    return None, None


def _parse_hr(cell: str) -> tuple[int | None, int | None]:
    text = _normalize_dashes(cell).strip()
    band = _HR_BAND.search(text)
    if band:
        return int(band.group(1)), int(band.group(2))
    return None, None


def _parse_day(cell: str, anchor: date) -> date | None:
    """Resolve "Jul 23" against the plan's year, rolling Dec->Jan forward."""
    match = _DAY_CELL.match(cell.strip())
    if match is None or match.group(1) not in _MONTHS:
        return None
    day = date(anchor.year, _MONTHS[match.group(1)], int(match.group(2)))
    if (anchor - day).days > _ROLLOVER_DAYS:
        return date(anchor.year + 1, day.month, day.day)
    return day


def _plan_date(text: str, fallback: date) -> date:
    revised = _REVISED.search(text)
    if revised:
        return date.fromisoformat(revised.group(1))
    for line in text.splitlines()[:10]:
        iso = _ISO_DATE.search(line)
        if iso:
            return date.fromisoformat(iso.group(1))
    return fallback


def parse_schedule(text: str, path: Path, fallback_date: date) -> PlanSchedule:
    """Extract every dated session row; non-matching lines are ignored."""
    plan_date = _plan_date(text, fallback_date)
    by_day: dict[date, PlannedSession] = {}
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 6:
            continue
        day = _parse_day(cells[0], plan_date)
        if day is None:
            continue
        kind = cells[2]
        pace_low, pace_high = _parse_pace(cells[3])
        hr_low, hr_high = _parse_hr(cells[4])
        by_day[day] = PlannedSession(
            day=day,
            weekday=cells[1],
            kind=kind,
            pace_low_s=pace_low,
            pace_high_s=pace_high,
            hr_low=hr_low,
            hr_high=hr_high,
            pace_text=cells[3],
            hr_text=cells[4],
            description=cells[5],
            is_rest="rest" in kind.lower(),
            is_race=kind.upper().startswith("RACE"),
        )
    return PlanSchedule(
        path=path,
        plan_date=plan_date,
        sessions=tuple(sorted(by_day.values(), key=lambda s: s.day)),
    )
