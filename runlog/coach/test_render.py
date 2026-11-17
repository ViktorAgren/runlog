"""Golden-ish tests for the plain-text card rendering."""

from __future__ import annotations

from datetime import date

from runlog.analyze.forecast import RaceForecast
from runlog.analyze.readiness import ReadinessDay
from runlog.analyze.records import RecordEvent
from runlog.coach import render
from runlog.coach.daily import Guidance, LastCard, SessionComparison, TodayCard
from runlog.coach.limiters import LimiterCard, LimiterFinding
from runlog.plan.progress import WorkoutDetail
from runlog.plan.schedule import PlannedSession

_ENGINE_RX = (
    "Add a 2nd quality day: weekly LT2 threshold plus fortnightly vVO2max reps "
    "(3-5 min) to lift the aerobic ceiling."
)
_SPEED_RX = (
    "Weekly neuromuscular speed: strides / short reps (10-20 x 100-200 m) "
    "and hill sprints."
)
_DURABILITY_RX = (
    "Progress the long run and add tempo-finish long runs; hold weekly "
    "aerobic volume to build fatigue resistance."
)
_SPEED_REASON = "short-distance efforts +5% vs your CS curve · D' 200 m reserve"


def _session() -> PlannedSession:
    return PlannedSession(
        day=date(2026, 7, 16),
        weekday="Thu",
        kind="Tempo",
        pace_low_s=295.0,
        pace_high_s=309.0,
        hr_low=172,
        hr_high=179,
        pace_text="4:55-5:09/km",
        hr_text="172-179",
        description="WU + 2x2.5 km @ Threshold",
        is_rest=False,
        is_race=False,
    )


def test_render_today_full_card() -> None:
    card = TodayCard(
        day=date(2026, 7, 17),
        readiness=ReadinessDay(
            date(2026, 7, 17), 62.0, {"resting_hr": 0.8, "hrv_sdnn": -0.2}
        ),
        yesterday_trimp=84.0,
        trimp_pctile=78.0,
        tsb=-13.4,
        acwr=1.21,
        session=PlannedSession(
            day=date(2026, 7, 17),
            weekday="Fri",
            kind="Rest",
            pace_low_s=None,
            pace_high_s=None,
            hr_low=None,
            hr_high=None,
            pace_text="—",
            hr_text="—",
            description="Full rest",
            is_rest=True,
            is_race=False,
        ),
        guidance=Guidance("REST", ("plan calls for rest",)),
        forecast=RaceForecast(
            race_day=date(2026, 8, 30),
            distance_m=3000.0,
            predicted_s=761.0,
            ci_low_s=742.0,
            ci_high_s=785.0,
            method="trend",
            n_weeks=11,
            current_best_s=740.0,
        ),
        fresh_records=(),
        standing_records=(
            RecordEvent(date(2026, 5, 10), "1k", "all_time", 221.0, "1k 3:41"),
            RecordEvent(date(2026, 7, 9), "5k", "all_time", 1392.0, "5k 23:12"),
        ),
    )
    text = render.render_today(card)
    assert text.splitlines() == [
        "TODAY — Fri 2026-07-17",
        "Readiness   62.0  (normal 40-60)",
        "  markers   resting_hr +0.8 · hrv_sdnn -0.2",
        "Yesterday   TRIMP 84 (78th pctile 90d) · TSB -13.4 · ACWR 1.21",
        "Plan        Rest — Full rest",
        "Guidance    REST — plan calls for rest",
        "Race        3 km · 2026-08-30 (44 d) · 12:41 (95% CI 12:22-13:05, trend n=11)",
        "PBs         1k 3:41 · 5k 23:12",
    ]


def test_render_today_shows_new_records_when_fresh() -> None:
    card = TodayCard(
        day=date(2026, 7, 17),
        readiness=None,
        yesterday_trimp=None,
        trimp_pctile=None,
        tsb=None,
        acwr=None,
        session=None,
        guidance=Guidance("GO", ("recovered and absorbing load",)),
        forecast=None,
        fresh_records=(
            RecordEvent(date(2026, 7, 16), "5k", "all_time", 1392.0, "5k 23:12"),
        ),
        standing_records=(),
    )
    assert "Records     NEW all-time 5k 23:12" in render.render_today(card)


def test_render_today_sparse() -> None:
    card = TodayCard(
        day=date(2026, 7, 17),
        readiness=None,
        yesterday_trimp=None,
        trimp_pctile=None,
        tsb=None,
        acwr=None,
        session=None,
        guidance=Guidance("EASY", ("no green light — default easy",)),
        forecast=None,
        fresh_records=(),
        standing_records=(),
    )
    assert render.render_today(card).splitlines() == [
        "TODAY — Fri 2026-07-17",
        "Readiness   —",
        "Yesterday   —",
        "Plan        no active plan schedule",
        "Guidance    EASY — no green light — default easy",
    ]


def _detail() -> WorkoutDetail:
    return WorkoutDetail(
        day=date(2026, 7, 16),
        weekday="Thu",
        kind="Quality",
        distance_km=7.10,
        moving_s=2172,
        avg_pace_s_per_km=306.0,
        avg_hr=168.0,
        max_hr=181.0,
        easy_pct=41.0,
        moderate_pct=22.0,
        hard_pct=37.0,
        gap_pace_s_per_km=302.0,
        negative_split_pct=1.8,
        elevation_gain_m=40.0,
    )


def test_render_last_full_card() -> None:
    from runlog.coach.daily import EffortLine

    card = LastCard(
        detail=_detail(),
        km_splits=(352.0, 310.0, 298.0),
        comparison=SessionComparison(_session(), "IN BAND", "BELOW BAND"),
        efforts=(
            EffortLine("1k", 262.0, 221.0, is_pb=False),
            EffortLine("5k", 1498.0, 1498.0, is_pb=True),
        ),
    )
    text = render.render_last(card)
    assert text.splitlines() == [
        "LAST RUN — Thu 2026-07-16 · Quality · 7.10 km · 36:12 · 5:06/km · HR 168/181",
        "Splits      5:52  5:10  4:58",
        "Pacing      GAP 5:02/km · negative split +1.8% · zones E 41 / M 22 / H 37 %",
        "Plan        Tempo (Jul 16): WU + 2x2.5 km @ Threshold",
        "  pace      4:55-5:09/km → avg 5:06  IN BAND",
        "  hr        172-179 → avg 168  BELOW BAND",
        "Efforts     1k 4:22 (PB 3:41) · 5k 24:58 PB!",
    ]


def test_render_last_no_band() -> None:
    session = PlannedSession(
        day=date(2026, 7, 16),
        weekday="Thu",
        kind="Easy",
        pace_low_s=None,
        pace_high_s=None,
        hr_low=None,
        hr_high=None,
        pace_text="by feel",
        hr_text="—",
        description="easy 6 km",
        is_rest=False,
        is_race=False,
    )
    card = LastCard(
        detail=_detail(),
        km_splits=(),
        comparison=SessionComparison(session, None, None),
        efforts=(),
    )
    text = render.render_last(card)
    assert "by feel — no band" in text


def test_render_limiters_full_card() -> None:
    card = LimiterCard(
        day=date(2026, 11, 17),
        primary="speed",
        findings=(
            LimiterFinding(
                "speed",
                2.0,
                (_SPEED_REASON,),
                _SPEED_RX,
            ),
            LimiterFinding(
                "engine", 0.0, ("VDOT 60.0 rising (+0.5/mo) · VO2max 60.0",), _ENGINE_RX
            ),
            LimiterFinding(
                "durability",
                0.0,
                ("fatigue resistance near reference",),
                _DURABILITY_RX,
            ),
            LimiterFinding(
                "structure",
                0.0,
                ("distribution & consistency on track",),
                "Hold the 80/20 structure and weekly consistency.",
            ),
        ),
        confidence="high",
        confidence_note=None,
        short_gap_pct=5.0,
        long_gap_pct=0.0,
    )
    assert render.render_limiters(card).splitlines() == [
        "LIMITERS — Tue 2026-11-17 · confidence high",
        "Primary     Speed (sev 2.0)",
        "Curve       short +5% · long +0% vs CS prediction",
        f"Speed       2.0 · {_SPEED_REASON}",
        f"  do        {_SPEED_RX}",
        "Engine      0.0 · VDOT 60.0 rising (+0.5/mo) · VO2max 60.0",
        f"  do        {_ENGINE_RX}",
        "Durability  0.0 · fatigue resistance near reference",
        f"  do        {_DURABILITY_RX}",
        "Structure   0.0 · distribution & consistency on track",
        "  do        Hold the 80/20 structure and weekly consistency.",
    ]


def test_render_limiters_low_confidence_shows_note_and_no_curve() -> None:
    card = LimiterCard(
        day=date(2026, 11, 17),
        primary="structure",
        findings=(
            LimiterFinding(
                "structure",
                2.0,
                ("only 66% easy (Seiler ~80%), PI 2.3",),
                "Shift to ~80/20: raise easy-run time, keep hard days hard.",
            ),
            LimiterFinding(
                "engine",
                0.0,
                ("engine trend unavailable (need 2+ CS windows)",),
                _ENGINE_RX,
            ),
            LimiterFinding(
                "speed",
                0.0,
                ("speed vs curve unavailable (need 3+ effort distances)",),
                _SPEED_RX,
            ),
            LimiterFinding(
                "durability",
                0.0,
                ("fatigue resistance near reference",),
                _DURABILITY_RX,
            ),
        ),
        confidence="low",
        confidence_note="limited by missing best efforts at 3+ distances",
        short_gap_pct=None,
        long_gap_pct=None,
    )
    lines = render.render_limiters(card).splitlines()
    assert lines[:3] == [
        "LIMITERS — Tue 2026-11-17 · confidence low",
        "Primary     Structure (sev 2.0)",
        "Note        limited by missing best efforts at 3+ distances",
    ]
    assert not any(line.startswith("Curve") for line in lines)
