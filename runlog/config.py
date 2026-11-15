"""Filesystem layout and Strava credential loading.

All local state lives under a single data directory (``./data`` by default,
overridable with ``RUNLOG_DATA_DIR``): the raw landing zone and the SQLite DB.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

from runlog.domain import Source

Sex = Literal["male", "female"]

_RAW_SUBDIRS: dict[Source | str, str] = {
    "strava_api": "raw/strava/api",
    "strava_bulk": "raw/strava/bulk",
    "apple_health": "raw/apple_health",
    "runna": "raw/runna",
}


@dataclass(frozen=True)
class Paths:
    """Resolved locations for the local data directory."""

    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "runlog.db"

    def raw_dir(self, kind: str) -> Path:
        """Return (creating if needed) the raw landing directory for ``kind``.

        ``kind`` is one of the keys in ``_RAW_SUBDIRS`` (``strava_api``,
        ``strava_bulk``, ``apple_health``).
        """
        subdir = _RAW_SUBDIRS[kind]
        path = self.data_dir / subdir
        path.mkdir(parents=True, exist_ok=True)
        return path


def resolve_paths(data_dir: Path | None = None) -> Paths:
    """Resolve the data directory, honoring ``RUNLOG_DATA_DIR`` when unset."""
    if data_dir is None:
        env = os.environ.get("RUNLOG_DATA_DIR")
        data_dir = Path(env) if env else Path.cwd() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Paths(data_dir=data_dir.resolve())


@dataclass(frozen=True)
class StravaCredentials:
    """OAuth application credentials for the Strava API."""

    client_id: str
    client_secret: str
    refresh_token: str | None = None


def load_strava_credentials() -> StravaCredentials:
    """Load Strava credentials from the environment / ``.env``.

    Raises ``RuntimeError`` if the client id/secret are missing, since no API
    call can be made without them.
    """
    load_dotenv()
    client_id = os.environ.get("STRAVA_CLIENT_ID", "").strip()
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            "STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be set "
            "(see .env.example)."
        )
    refresh_token = os.environ.get("STRAVA_REFRESH_TOKEN", "").strip() or None
    return StravaCredentials(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
    )


@dataclass(frozen=True)
class Athlete:
    """Demographics needed to estimate resting energy expenditure (BMR)."""

    sex: Sex
    height_cm: float
    birth_date: date

    def age_on(self, day: date) -> int:
        """Whole years old on ``day`` (accounts for whether the birthday passed)."""
        had_birthday = (day.month, day.day) >= (
            self.birth_date.month,
            self.birth_date.day,
        )
        return day.year - self.birth_date.year - (0 if had_birthday else 1)


@dataclass(frozen=True)
class AthleteConfig:
    """Persisted athlete settings read by every command.

    Demographics (for the energy model) plus the HR anchors (for the zone
    math). Any field may be unset; callers fall back to a data-derived default
    at the point of use.
    """

    sex: Sex | None = None
    height_cm: float | None = None
    birth_date: date | None = None
    hr_max: float | None = None
    hr_rest: float | None = None

    def demographics(self) -> Athlete | None:
        """The energy model needs all three demographics, or it is skipped."""
        if self.sex is None or self.height_cm is None or self.birth_date is None:
            return None
        return Athlete(
            sex=self.sex, height_cm=self.height_cm, birth_date=self.birth_date
        )


def _data_dir(paths: Paths | None) -> Path:
    """Where athlete.toml lives, resolved without creating the directory."""
    if paths is not None:
        return paths.data_dir
    env = os.environ.get("RUNLOG_DATA_DIR")
    return Path(env) if env else Path.cwd() / "data"


def _load_athlete_toml(paths: Paths | None) -> Mapping[str, object]:
    path = _data_dir(paths) / "athlete.toml"
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _pick(toml: Mapping[str, object], key: str, env_key: str) -> object:
    """Prefer the athlete.toml value, else the environment variable."""
    if key in toml:
        return toml[key]
    return os.environ.get(env_key)


def _as_sex(value: object) -> Sex | None:
    if isinstance(value, str):
        match value.strip().lower():
            case "male":
                return "male"
            case "female":
                return "female"
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None  # bool is an int subclass; a true/false HR is a mistake
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_date(value: object) -> date | None:
    if isinstance(value, date):
        return value  # tomllib parses a bare YYYY-MM-DD to a date
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def load_athlete_config(paths: Paths | None = None) -> AthleteConfig:
    """Load athlete settings from ``data/athlete.toml``, then the environment.

    Every field is optional and falls back to a data-derived default at the
    point of use, so a missing file or key degrades gracefully.
    """
    load_dotenv()
    toml = _load_athlete_toml(paths)
    return AthleteConfig(
        sex=_as_sex(_pick(toml, "sex", "RUNLOG_ATHLETE_SEX")),
        height_cm=_as_float(_pick(toml, "height_cm", "RUNLOG_ATHLETE_HEIGHT_CM")),
        birth_date=_as_date(_pick(toml, "birth_date", "RUNLOG_ATHLETE_BIRTH_DATE")),
        hr_max=_as_float(_pick(toml, "hr_max", "RUNLOG_HR_MAX")),
        hr_rest=_as_float(_pick(toml, "hr_rest", "RUNLOG_HR_REST")),
    )


def load_athlete(paths: Paths | None = None) -> Athlete | None:
    """Athlete demographics for the energy model, or None when incomplete."""
    return load_athlete_config(paths).demographics()
