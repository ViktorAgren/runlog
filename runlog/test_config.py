"""Unit tests for configuration loading."""

from __future__ import annotations

from datetime import date

import pytest

from runlog import config
from runlog.config import Athlete


class TestAgeOn:
    def test_age_before_birthday_in_year(self) -> None:
        athlete = Athlete(sex="male", height_cm=177.0, birth_date=date(1999, 6, 7))
        assert athlete.age_on(date(2026, 6, 6)) == 26

    def test_age_on_birthday(self) -> None:
        athlete = Athlete(sex="male", height_cm=177.0, birth_date=date(1999, 6, 7))
        assert athlete.age_on(date(2026, 6, 7)) == 27

    def test_age_after_birthday_in_year(self) -> None:
        athlete = Athlete(sex="male", height_cm=177.0, birth_date=date(1999, 6, 7))
        assert athlete.age_on(date(2026, 7, 18)) == 27


class TestLoadAthlete:
    @pytest.fixture(autouse=True)
    def _no_dotenv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Keep the loader hermetic: ignore any real .env on the machine.
        monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)

    def test_loads_full_athlete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUNLOG_ATHLETE_SEX", "male")
        monkeypatch.setenv("RUNLOG_ATHLETE_HEIGHT_CM", "177")
        monkeypatch.setenv("RUNLOG_ATHLETE_BIRTH_DATE", "1999-06-07")
        assert config.load_athlete() == Athlete(
            sex="male", height_cm=177.0, birth_date=date(1999, 6, 7)
        )

    def test_none_when_any_field_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUNLOG_ATHLETE_SEX", "male")
        monkeypatch.setenv("RUNLOG_ATHLETE_HEIGHT_CM", "177")
        monkeypatch.delenv("RUNLOG_ATHLETE_BIRTH_DATE", raising=False)
        assert config.load_athlete() is None

    def test_none_when_sex_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUNLOG_ATHLETE_SEX", "unknown")
        monkeypatch.setenv("RUNLOG_ATHLETE_HEIGHT_CM", "177")
        monkeypatch.setenv("RUNLOG_ATHLETE_BIRTH_DATE", "1999-06-07")
        assert config.load_athlete() is None
