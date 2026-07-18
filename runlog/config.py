"""Filesystem layout and Strava credential loading.

All local state lives under a single data directory (``./data`` by default,
overridable with ``RUNLOG_DATA_DIR``): the raw landing zone and the SQLite DB.
"""

from __future__ import annotations

import os
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


def load_athlete() -> Athlete | None:
    """Load athlete demographics from the environment / ``.env``.

    Returns ``None`` when any field is unset or the sex is unrecognized, so the
    energy-expenditure feature degrades gracefully instead of raising.
    """
    load_dotenv()
    sex = os.environ.get("RUNLOG_ATHLETE_SEX", "").strip().lower()
    height = os.environ.get("RUNLOG_ATHLETE_HEIGHT_CM", "").strip()
    birth = os.environ.get("RUNLOG_ATHLETE_BIRTH_DATE", "").strip()
    if sex not in ("male", "female") or not height or not birth:
        return None
    try:
        return Athlete(
            sex=sex,  # type: ignore[arg-type]
            height_cm=float(height),
            birth_date=date.fromisoformat(birth),
        )
    except ValueError:
        return None
