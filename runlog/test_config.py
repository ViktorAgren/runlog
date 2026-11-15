"""Unit tests for configuration loading."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from runlog import config
from runlog.config import Athlete, AthleteConfig


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Ignore the machine's .env and point the data dir at an empty tmp dir."""
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("RUNLOG_DATA_DIR", str(tmp_path))
    for key in (
        "RUNLOG_ATHLETE_SEX",
        "RUNLOG_ATHLETE_HEIGHT_CM",
        "RUNLOG_ATHLETE_BIRTH_DATE",
        "RUNLOG_HR_MAX",
        "RUNLOG_HR_REST",
    ):
        monkeypatch.delenv(key, raising=False)


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
    def test_loads_full_athlete_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUNLOG_ATHLETE_SEX", "male")
        monkeypatch.setenv("RUNLOG_ATHLETE_HEIGHT_CM", "177")
        monkeypatch.setenv("RUNLOG_ATHLETE_BIRTH_DATE", "1999-06-07")
        assert config.load_athlete() == Athlete(
            sex="male", height_cm=177.0, birth_date=date(1999, 6, 7)
        )

    def test_none_when_any_field_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUNLOG_ATHLETE_SEX", "male")
        monkeypatch.setenv("RUNLOG_ATHLETE_HEIGHT_CM", "177")
        assert config.load_athlete() is None

    def test_none_when_sex_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUNLOG_ATHLETE_SEX", "unknown")
        monkeypatch.setenv("RUNLOG_ATHLETE_HEIGHT_CM", "177")
        monkeypatch.setenv("RUNLOG_ATHLETE_BIRTH_DATE", "1999-06-07")
        assert config.load_athlete() is None

    def test_loads_from_toml(self, tmp_path: Path) -> None:
        (tmp_path / "athlete.toml").write_text(
            'sex = "male"\nheight_cm = 180\nbirth_date = 1990-01-01\n'
        )
        assert config.load_athlete(config.Paths(data_dir=tmp_path)) == Athlete(
            sex="male", height_cm=180.0, birth_date=date(1990, 1, 1)
        )


class TestLoadAthleteConfig:
    @staticmethod
    def _paths(tmp_path: Path) -> config.Paths:
        return config.Paths(data_dir=tmp_path)

    def test_reads_all_fields_from_toml(self, tmp_path: Path) -> None:
        (tmp_path / "athlete.toml").write_text(
            'sex = "female"\n'
            "height_cm = 170\n"
            "birth_date = 1994-03-02\n"
            "hr_max = 190\n"
            "hr_rest = 48\n"
        )
        assert config.load_athlete_config(self._paths(tmp_path)) == AthleteConfig(
            sex="female",
            height_cm=170.0,
            birth_date=date(1994, 3, 2),
            hr_max=190.0,
            hr_rest=48.0,
        )

    def test_empty_when_no_file_and_no_env(self, tmp_path: Path) -> None:
        assert config.load_athlete_config(self._paths(tmp_path)) == AthleteConfig()

    def test_toml_overrides_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RUNLOG_HR_MAX", "185")
        (tmp_path / "athlete.toml").write_text("hr_max = 193\n")
        assert config.load_athlete_config(self._paths(tmp_path)).hr_max == 193.0

    def test_env_fills_keys_absent_from_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RUNLOG_HR_REST", "50")
        (tmp_path / "athlete.toml").write_text("hr_max = 193\n")
        cfg = config.load_athlete_config(self._paths(tmp_path))
        assert (cfg.hr_max, cfg.hr_rest) == (193.0, 50.0)

    def test_string_values_coerce(self, tmp_path: Path) -> None:
        (tmp_path / "athlete.toml").write_text(
            'hr_max = "193"\nbirth_date = "1994-03-02"\n'
        )
        cfg = config.load_athlete_config(self._paths(tmp_path))
        assert (cfg.hr_max, cfg.birth_date) == (193.0, date(1994, 3, 2))

    def test_invalid_values_become_none(self, tmp_path: Path) -> None:
        (tmp_path / "athlete.toml").write_text(
            'sex = "other"\nhr_max = "fast"\nbirth_date = "nope"\n'
        )
        assert config.load_athlete_config(self._paths(tmp_path)) == AthleteConfig()

    def test_bool_hr_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "athlete.toml").write_text("hr_max = true\n")
        assert config.load_athlete_config(self._paths(tmp_path)).hr_max is None

    def test_reads_from_env_data_dir_without_explicit_paths(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "athlete.toml").write_text("hr_max = 188\n")
        assert config.load_athlete_config().hr_max == 188.0

    def test_demographics_none_when_incomplete(self) -> None:
        assert AthleteConfig(sex="male", height_cm=180.0).demographics() is None

    def test_demographics_full(self) -> None:
        cfg = AthleteConfig(
            sex="male",
            height_cm=180.0,
            birth_date=date(1990, 1, 1),
            hr_max=190.0,
        )
        assert cfg.demographics() == Athlete(
            sex="male", height_cm=180.0, birth_date=date(1990, 1, 1)
        )
