"""Tests for parsing Runna's activities.json into native activity summaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from runlog.sources.runna.export import RunnaActivity, parse_activities

_ACTIVITIES = [
    {
        "id": "aaa",
        "type": "INDOOR_RUN",
        "treadmillType": "DUMB",
        "recordType": "PHONE",
        "name": "Treadmill Tempo",
        "stravaActivities": [{"stravaActivityId": 123}],
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
        "id": "bbb",
        "type": "RUN",
        "recordType": "APPLE_WATCH",
        "name": "Morning Run",
        "stravaActivities": [],
        "summary": {
            "startTimestamp": 1700100000000,
            "distance": 10000.0,
            "totalTime": 3000000,
            "movingTime": 2950000,
            "averageHeartRate": 145.0,
            "maxHeartRate": 165.0,
            "averageCadence": 158.0,
            "averageSpeed": 3.4,
            "maxSpeed": 4.5,
            "calories": 600,
        },
    },
    {
        "id": "ccc",
        "type": "RUN",
        "recordType": "STRAVA",
        "summary": {"startTimestamp": 1700200000000, "distance": 8000.0},
    },
]


def test_parse_activities_skips_strava_echoes_and_maps_fields() -> None:
    result = parse_activities(json.dumps(_ACTIVITIES).encode("utf-8"))
    assert result == [
        RunnaActivity(
            runna_id="aaa",
            strava_activity_id=123,
            is_indoor=True,
            record_type="PHONE",
            start_time_utc=datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC),
            name="Treadmill Tempo",
            distance_m=5000.0,
            elapsed_s=1500,
            moving_s=1490,
            avg_hr=150.0,
            max_hr=170.0,
            avg_cadence=160.0,
            avg_speed_mps=3.3,
            max_speed_mps=4.0,
            calories=300.0,
        ),
        RunnaActivity(
            runna_id="bbb",
            strava_activity_id=None,
            is_indoor=False,
            record_type="APPLE_WATCH",
            start_time_utc=datetime(2023, 11, 16, 2, 0, 0, tzinfo=UTC),
            name="Morning Run",
            distance_m=10000.0,
            elapsed_s=3000,
            moving_s=2950,
            avg_hr=145.0,
            max_hr=165.0,
            avg_cadence=158.0,
            avg_speed_mps=3.4,
            max_speed_mps=4.5,
            calories=600.0,
        ),
    ]


def test_fit_file_name_derives_from_runna_id() -> None:
    [indoor, _] = parse_activities(json.dumps(_ACTIVITIES).encode("utf-8"))
    assert indoor.fit_file == "aaa.fit"


def test_parse_activities_collapses_same_session_keeping_phone() -> None:
    # One treadmill session logged by both the phone (manual speed) and the
    # watch (arm-swing) 20 s apart: only the phone recording survives.
    same_session = [
        {
            "id": "phone",
            "type": "INDOOR_RUN",
            "recordType": "PHONE",
            "summary": {"startTimestamp": 1700000000000, "distance": 5000.0},
        },
        {
            "id": "watch",
            "type": "INDOOR_RUN",
            "recordType": "APPLE_FITNESS",
            "summary": {"startTimestamp": 1700000020000, "distance": 4000.0},
        },
    ]
    result = parse_activities(json.dumps(same_session).encode("utf-8"))
    assert [(a.runna_id, a.distance_m) for a in result] == [("phone", 5000.0)]
