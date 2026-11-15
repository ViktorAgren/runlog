"""Tests for the read-only JSON API."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from runlog import config
from runlog.db import store
from runlog.domain import Activity, ActivityRecord, SourceId, StreamPoint
from runlog.web import create_app


def _store_run(path: Path, source_id: str, when: datetime) -> None:
    conn = store.connect(path)
    store.init_db(conn)
    stream = tuple(
        StreamPoint(offset_s=i, distance_m=float(i) * 3.0, hr=150.0, velocity_mps=3.0)
        for i in range(600)
    )
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source="strava",
                source_id=SourceId(source_id),
                sport_type="Run",
                start_time_utc=when,
                distance_m=1800.0,
                moving_s=600,
                avg_pace_s_per_km=333.0,
                avg_hr=150.0,
            ),
            stream=stream,
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("RUNLOG_DATA_DIR", str(tmp_path))
    for key in ("RUNLOG_HR_MAX", "RUNLOG_HR_REST", "RUNLOG_ATHLETE_SEX"):
        monkeypatch.delenv(key, raising=False)
    db = tmp_path / "runlog.db"
    _store_run(db, "s:1", datetime(2026, 6, 1, 7, tzinfo=UTC))
    _store_run(db, "s:2", datetime(2026, 6, 8, 7, tzinfo=UTC))
    return TestClient(create_app(db))


def test_list_activities_returns_a_row_per_run(client: TestClient) -> None:
    resp = client.get("/api/activities")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    assert {r["distance_km"] for r in rows} == {1.8}
    assert all("activity_id" in r and "pace_disp" in r for r in rows)


def test_activity_detail_ok(client: TestClient) -> None:
    activity_id = client.get("/api/activities").json()[0]["activity_id"]
    resp = client.get(f"/api/activities/{activity_id}")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["activity_id"] == activity_id
    assert detail["charts"] and detail["session"]


def test_activity_detail_404_for_unknown_id(client: TestClient) -> None:
    resp = client.get("/api/activities/999999")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "activity not found"}


def test_weekly_volume_and_pmc(client: TestClient) -> None:
    assert client.get("/api/weekly-volume").status_code == 200
    pmc = client.get("/api/pmc")
    assert pmc.status_code == 200
    assert pmc.json()  # avg HR present, so TRIMP produces a PMC series


def test_readiness_endpoint_ok(client: TestClient) -> None:
    # No passive health data stored, so the series is empty but the route works.
    resp = client.get("/api/readiness")
    assert resp.status_code == 200
    assert resp.json() == []
