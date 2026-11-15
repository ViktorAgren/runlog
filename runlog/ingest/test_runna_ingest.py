"""End-to-end test for Runna export ingest against a real FIT fixture."""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from runlog.analyze import metrics
from runlog.config import resolve_paths
from runlog.db import store
from runlog.domain import Activity, ActivityRecord, SourceId
from runlog.ingest.runna_ingest import import_export

_SAMPLE_FIT = (
    Path(__file__).parent.parent / "sources" / "runna" / "testdata" / "sample.fit"
)

_ACTIVITIES = [
    {
        "id": "run1",
        "type": "INDOOR_RUN",
        "treadmillType": "DUMB",
        "recordType": "PHONE",
        "name": "Treadmill Tempo",
        "stravaActivities": [{"stravaActivityId": 999}],
        "summary": {
            "startTimestamp": 1700000000000,
            "distance": 5000.0,
            "totalTime": 1500000,
            "movingTime": 1490000,
            "averageHeartRate": 150.0,
            "maxHeartRate": 170.0,
            "averageCadence": 160.0,
            "averageSpeed": 3.3,
            "maxSpeed": 4.0,
            "calories": 300,
        },
    },
    {
        "id": "echo",
        "recordType": "STRAVA",
        "summary": {"startTimestamp": 1700000000000},
    },
]


def _build_export(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w") as export:
        export.writestr("activities.json", json.dumps(_ACTIVITIES))
        export.writestr("fit-files/run1.fit", _SAMPLE_FIT.read_bytes())


def _seed_strava_twin(conn: object) -> None:
    store.store_record(
        conn,  # type: ignore[arg-type]
        ActivityRecord(
            activity=Activity(
                source="strava",
                source_id=SourceId("999"),
                sport_type="Run",
                start_time_utc=datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC),
                distance_m=4100.0,  # wrong arm-swing treadmill distance
                moving_s=1500,
                avg_pace_s_per_km=366.0,
            ),
        ),
    )


def test_import_stores_activity_stream_and_links_strava_twin(tmp_path: Path) -> None:
    zip_path = tmp_path / "export.zip"
    _build_export(zip_path)
    paths = resolve_paths(tmp_path / "data")
    conn = store.connect(paths.db_path)
    store.init_db(conn)
    _seed_strava_twin(conn)

    stored = import_export(conn, paths, zip_path)

    counts = store.table_counts(conn)
    assert (stored, counts) == (
        1,
        {
            "activities": 2,
            "laps": 2,
            "stream_points": 283,
            "health_metrics": 0,
            "activity_links": 0,
            "runna_links": 1,
            "raw_files": 1,
            "activities:runna": 1,
            "activities:strava": 1,
        },
    )


def test_import_makes_indoor_runna_canonical_over_strava(tmp_path: Path) -> None:
    zip_path = tmp_path / "export.zip"
    _build_export(zip_path)
    paths = resolve_paths(tmp_path / "data")
    conn = store.connect(paths.db_path)
    store.init_db(conn)
    _seed_strava_twin(conn)

    import_export(conn, paths, zip_path)

    canonical = metrics.canonical_run_activities(conn)
    assert [(r.source, r.distance_m) for r in canonical] == [("runna", 5000.0)]
