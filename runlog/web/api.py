"""A small read-only JSON API over the existing analysis dataclasses.

Each endpoint reuses the same builders the report and coach use and returns
their plain dataclasses, which FastAPI serializes. Every request opens its own
SQLite connection inside a stream cache, so the API stays a thin, stateless
shell over the functional core — a foundation for an interactive web UI.

FastAPI resolves annotations at runtime to build its request/response models,
so this module (unlike the rest of the package) does not defer annotations.
"""

import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException

from runlog.analyze import activities_view, analytics, metrics, readiness, streams
from runlog.analyze.analytics import PmcPoint
from runlog.analyze.metrics import Run, WeeklyVolume
from runlog.analyze.readiness import ReadinessDay
from runlog.config import Sex, load_athlete_config
from runlog.db import store


def _resolve_hr(
    conn: sqlite3.Connection, runs: Sequence[Run]
) -> tuple[float, float, Sex | None]:
    """HR max/rest/sex from the athlete profile, falling back to stored data."""
    cfg = load_athlete_config()
    hr_max = cfg.hr_max or metrics.estimated_hr_max(
        metrics.hr_samples(conn, [r.activity_id for r in runs])
    )
    hr_rest = (
        cfg.hr_rest if cfg.hr_rest is not None else metrics.resting_hr_median(conn)
    )
    return hr_max, hr_rest, cfg.sex


def create_app(db_path: Path) -> FastAPI:
    """Build the FastAPI app reading from the SQLite DB at ``db_path``."""
    app = FastAPI(title="runlog", summary="Read-only running-analytics API")

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = store.connect(db_path)
        try:
            with streams.stream_cache(conn):
                yield conn
        finally:
            conn.close()

    conn_dep = Annotated[sqlite3.Connection, Depends(get_conn)]

    @app.get("/api/activities", response_model=None)
    def list_activities(conn: conn_dep) -> list[activities_view.ActivityRow]:
        runs = metrics.canonical_run_activities(conn)
        hr_max, hr_rest, _sex = _resolve_hr(conn, runs)
        rows, _details = activities_view.build_activities(conn, runs, hr_max, hr_rest)
        return rows

    @app.get("/api/activities/{activity_id}", response_model=None)
    def activity_detail(
        activity_id: int, conn: conn_dep
    ) -> activities_view.ActivityDetail:
        runs = metrics.canonical_run_activities(conn)
        run = next((r for r in runs if int(r.activity_id) == activity_id), None)
        if run is None:
            raise HTTPException(status_code=404, detail="activity not found")
        hr_max, hr_rest, _sex = _resolve_hr(conn, runs)
        return activities_view.build_detail(conn, run, hr_max, hr_rest)

    @app.get("/api/weekly-volume", response_model=None)
    def weekly_volume(conn: conn_dep) -> list[WeeklyVolume]:
        return metrics.weekly_volume(metrics.canonical_run_activities(conn))

    @app.get("/api/pmc", response_model=None)
    def pmc(conn: conn_dep) -> list[PmcPoint]:
        runs = metrics.canonical_run_activities(conn)
        hr_max, hr_rest, sex = _resolve_hr(conn, runs)
        return analytics.performance_management(
            analytics.daily_trimp(runs, hr_max, hr_rest, sex)
        )

    @app.get("/api/readiness", response_model=None)
    def readiness_series(conn: conn_dep) -> list[ReadinessDay]:
        return readiness.readiness_series(conn)

    return app
