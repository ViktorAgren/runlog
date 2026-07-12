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
   <WorkoutStatistics type="HKQuantityTypeIdentifierRunningPower"
     average="245" unit="W"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierRunningStrideLength"
     average="1.15" unit="m"/>
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


_HEALTH_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_GB">
 <Record type="HKQuantityTypeIdentifierOxygenSaturation"
   startDate="2026-06-01 08:00:00 +0000" endDate="2026-06-01 08:00:00 +0000"
   value="0.97" unit="%"/>
 <Record type="HKQuantityTypeIdentifierActiveEnergyBurned"
   startDate="2026-06-01 08:00:00 +0000" endDate="2026-06-01 08:05:00 +0000"
   value="30" unit="kcal"/>
 <Record type="HKQuantityTypeIdentifierActiveEnergyBurned"
   startDate="2026-06-01 09:00:00 +0000" endDate="2026-06-01 09:05:00 +0000"
   value="45" unit="kcal"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis"
   value="HKCategoryValueSleepAnalysisAsleepCore"
   startDate="2026-05-31 23:00:00 +0000" endDate="2026-06-01 01:00:00 +0000"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis"
   value="HKCategoryValueSleepAnalysisAsleepDeep"
   startDate="2026-06-01 01:00:00 +0000" endDate="2026-06-01 06:00:00 +0000"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis"
   value="HKCategoryValueSleepAnalysisInBed"
   startDate="2026-06-01 06:00:00 +0000" endDate="2026-06-01 07:00:00 +0000"/>
</HealthData>
"""


def test_parse_export_periodic_daily_and_sleep_metrics() -> None:
    metrics = parse_export(io.BytesIO(_HEALTH_XML)).metrics
    by_type = {m.metric_type: m.value for m in metrics}
    assert by_type["spo2"] == 0.97
    assert by_type["active_energy"] == 75.0  # 30 + 45 summed for the day
    assert by_type["sleep_hours"] == 7.0  # 2h + 5h asleep; InBed ignored


# The same night recorded by two devices (a sleep app and the watch): their
# intervals overlap, so per-night totals must come from one source, not the sum.
_TWO_SOURCE_SLEEP_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_GB">
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Sleep Cycle"
   value="HKCategoryValueSleepAnalysisAsleepUnspecified"
   startDate="2026-07-08 23:00:00 +0000" endDate="2026-07-09 06:00:00 +0000"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Watch"
   value="HKCategoryValueSleepAnalysisAsleepCore"
   startDate="2026-07-08 23:10:00 +0000" endDate="2026-07-09 03:10:00 +0000"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Watch"
   value="HKCategoryValueSleepAnalysisAsleepDeep"
   startDate="2026-07-09 03:10:00 +0000" endDate="2026-07-09 06:40:00 +0000"/>
</HealthData>
"""


def test_sleep_hours_takes_largest_source_not_the_sum_across_devices() -> None:
    metrics = parse_export(io.BytesIO(_TWO_SOURCE_SLEEP_XML)).metrics
    sleep = [m.value for m in metrics if m.metric_type == "sleep_hours"]
    # Sleep Cycle logged 7.0 h, the watch 7.5 h across two stages; summing
    # sources would give a bogus 14.5 h night.
    assert sleep == [7.5]


_DYNAMICS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_GB">
 <Record type="HKQuantityTypeIdentifierRunningPower" unit="W"
   startDate="2026-06-01 07:30:05 +0000" endDate="2026-06-01 07:30:05 +0000"
   value="200"/>
 <Record type="HKQuantityTypeIdentifierRunningPower" unit="W"
   startDate="2026-06-01 07:30:15 +0000" endDate="2026-06-01 07:30:15 +0000"
   value="220"/>
 <Record type="HKQuantityTypeIdentifierRunningSpeed" unit="km/hr"
   startDate="2026-06-01 07:30:05 +0000" endDate="2026-06-01 07:30:05 +0000"
   value="18"/>
</HealthData>
"""


def test_parse_export_collects_standalone_dynamics_sorted_and_converted() -> None:
    samples = parse_export(io.BytesIO(_DYNAMICS_XML)).dynamics_samples
    assert samples == {
        "avg_power_w": (
            (datetime(2026, 6, 1, 7, 30, 5, tzinfo=UTC), 200.0),
            (datetime(2026, 6, 1, 7, 30, 15, tzinfo=UTC), 220.0),
        ),
        "avg_running_speed_mps": (
            (datetime(2026, 6, 1, 7, 30, 5, tzinfo=UTC), 5.0),  # 18 km/h -> 5 m/s
        ),
    }


_OVERLAPPING_SEGMENTS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_GB">
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="10"
   durationUnit="min" sourceName="Watch"
   startDate="2026-06-01 07:00:00 +0000" endDate="2026-06-01 07:10:00 +0000">
   <WorkoutEvent type="HKWorkoutEventTypeSegment"
     date="2026-06-01 07:00:00 +0000" duration="4" durationUnit="min"/>
   <WorkoutEvent type="HKWorkoutEventTypeSegment"
     date="2026-06-01 07:00:00 +0000" duration="7" durationUnit="min"/>
   <WorkoutEvent type="HKWorkoutEventTypeSegment"
     date="2026-06-01 07:04:00 +0000" duration="6" durationUnit="min"/>
   <WorkoutEvent type="HKWorkoutEventTypeSegment"
     date="2026-06-01 07:00:00 +0000" duration="4" durationUnit="min"/>
 </Workout>
</HealthData>
"""


_WORKOUT_ACTIVITY_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_GB">
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="20"
   durationUnit="min" sourceName="Watch"
   startDate="2026-06-01 07:00:00 +0000" endDate="2026-06-01 07:20:00 +0000">
   <WorkoutActivity startDate="2026-06-01 07:00:00 +0000"
     endDate="2026-06-01 07:10:00 +0000">
     <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" average="150"/>
     <WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning"
       sum="2" unit="km"/>
   </WorkoutActivity>
   <WorkoutActivity startDate="2026-06-01 07:10:00 +0000"
     endDate="2026-06-01 07:20:00 +0000">
     <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" average="178"/>
     <WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning"
       sum="2.5" unit="km"/>
   </WorkoutActivity>
   <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" average="164"/>
 </Workout>
</HealthData>
"""


def test_parse_export_builds_laps_from_workout_activities() -> None:
    # Two WorkoutActivity phases, each with its own distance + HR; pace is
    # derived. Phase 1: 2 km in 600 s -> 300 s/km @ 150 bpm.
    workout = parse_export(io.BytesIO(_WORKOUT_ACTIVITY_XML)).workouts[0]
    assert [
        (lap.elapsed_s, lap.distance_m, lap.avg_hr, lap.avg_pace_s_per_km)
        for lap in workout.laps
    ] == [(600, 2000.0, 150.0, 300.0), (600, 2500.0, 178.0, 240.0)]


def test_parse_export_dedupes_overlapping_apple_segments() -> None:
    # Two segments share 07:00 (4 and 7 min, overlapping) plus a duplicate; only
    # the non-overlapping sequence [07:00 +4min, 07:04 +6min] should survive.
    workout = parse_export(io.BytesIO(_OVERLAPPING_SEGMENTS_XML)).workouts[0]
    assert [lap.elapsed_s for lap in workout.laps] == [240, 360]


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
            avg_power_w=245.0,
            avg_stride_length_m=1.15,
            route_file="route_2026-06-01_7.30am.gpx",
            laps=(
                Lap(lap_index=0, elapsed_s=240),
                Lap(lap_index=1, elapsed_s=120),
            ),
        ),
    )
