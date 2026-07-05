"""Unit tests for the Apple Health export.xml parser."""

from __future__ import annotations

import io
from datetime import UTC, datetime

from runlog.domain import HealthMetric, Lap
from runlog.sources.apple_health.export import AppleWorkout, parse_export

# A curated resting HR / HRV / VO2max plus an instantaneous HeartRate record
# (which must be ignored), and one running workout with segments and a route.
_EXPORT_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_GB">
 <Record type="HKQuantityTypeIdentifierRestingHeartRate" sourceName="Watch"
   unit="count/min" startDate="2026-06-01 00:00:00 +0100"
   endDate="2026-06-01 00:00:00 +0100" value="52"/>
 <Record type="HKQuantityTypeIdentifierHeartRateVariabilitySDNN" sourceName="Watch"
   unit="ms" startDate="2026-06-01 06:00:00 +0100"
   endDate="2026-06-01 06:00:00 +0100" value="65"/>
 <Record type="HKQuantityTypeIdentifierVO2Max" sourceName="Watch"
   unit="mL/min-kg" startDate="2026-06-01 06:00:00 +0100"
   endDate="2026-06-01 06:00:00 +0100" value="52.3"/>
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Watch"
   unit="count/min" startDate="2026-06-01 07:30:05 +0100"
   endDate="2026-06-01 07:30:05 +0100" value="120"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30"
   durationUnit="min" totalDistance="5" totalDistanceUnit="km"
   totalEnergyBurned="300" totalEnergyBurnedUnit="kcal" sourceName="Watch"
   startDate="2026-06-01 07:30:00 +0100" endDate="2026-06-01 08:00:00 +0100">
   <WorkoutEvent type="HKWorkoutEventTypeSegment"
     date="2026-06-01 07:30:00 +0100" duration="4" durationUnit="min"/>
   <WorkoutEvent type="HKWorkoutEventTypeSegment"
     date="2026-06-01 07:34:00 +0100" duration="2" durationUnit="min"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate"
     average="150" maximum="178"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierStepCount" sum="5400"/>
   <WorkoutRoute sourceName="Watch" startDate="2026-06-01 07:30:00 +0100">
     <FileReference path="/workout-routes/route_2026-06-01_7.30am.gpx"/>
   </WorkoutRoute>
 </Workout>
</HealthData>
"""


def test_parse_export_extracts_curated_metrics() -> None:
    result = parse_export(io.BytesIO(_EXPORT_XML))
    assert result.metrics == (
        HealthMetric(
            metric_type="resting_hr",
            start_time_utc=datetime(2026, 5, 31, 23, 0, tzinfo=UTC),
            end_time_utc=datetime(2026, 5, 31, 23, 0, tzinfo=UTC),
            value=52.0,
            unit="count/min",
            source="Watch",
        ),
        HealthMetric(
            metric_type="hrv_sdnn",
            start_time_utc=datetime(2026, 6, 1, 5, 0, tzinfo=UTC),
            end_time_utc=datetime(2026, 6, 1, 5, 0, tzinfo=UTC),
            value=65.0,
            unit="ms",
            source="Watch",
        ),
        HealthMetric(
            metric_type="vo2max",
            start_time_utc=datetime(2026, 6, 1, 5, 0, tzinfo=UTC),
            end_time_utc=datetime(2026, 6, 1, 5, 0, tzinfo=UTC),
            value=52.3,
            unit="mL/min-kg",
            source="Watch",
        ),
    )


_MODERN_WORKOUT_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_GB">
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30"
   durationUnit="min" sourceName="Watch"
   startDate="2026-06-01 07:30:00 +0100" endDate="2026-06-01 08:00:00 +0100">
   <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate"
     average="150" maximum="178"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning"
     sum="5.2" unit="km"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierActiveEnergyBurned"
     sum="310" unit="kcal"/>
 </Workout>
</HealthData>
"""


def test_parse_export_reads_distance_and_energy_from_statistics() -> None:
    # Modern exports omit totalDistance/totalEnergyBurned attributes and carry
    # them in WorkoutStatistics instead.
    workout = parse_export(io.BytesIO(_MODERN_WORKOUT_XML)).workouts[0]
    assert (workout.distance_m, workout.calories) == (5200.0, 310.0)


def test_parse_export_extracts_workout_with_laps_and_route() -> None:
    result = parse_export(io.BytesIO(_EXPORT_XML))
    assert result.workouts == (
        AppleWorkout(
            activity_type="Running",
            start_time_utc=datetime(2026, 6, 1, 6, 30, tzinfo=UTC),
            end_time_utc=datetime(2026, 6, 1, 7, 0, tzinfo=UTC),
            duration_s=1800,
            distance_m=5000.0,
            calories=300.0,
            avg_hr=150.0,
            max_hr=178.0,
            avg_cadence=180.0,  # 5400 steps over 30 min
            route_file="route_2026-06-01_7.30am.gpx",
            laps=(
                Lap(lap_index=0, elapsed_s=240),
                Lap(lap_index=1, elapsed_s=120),
            ),
        ),
    )
