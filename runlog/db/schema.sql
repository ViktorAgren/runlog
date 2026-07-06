-- runlog local storage schema (SQLite).
-- Times are stored as ISO-8601 UTC strings. Idempotent ingest relies on the
-- UNIQUE(source, source_id) constraint on activities.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS activities (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source            TEXT    NOT NULL,
    source_id         TEXT    NOT NULL,
    sport_type        TEXT    NOT NULL,
    start_time_utc    TEXT    NOT NULL,
    tz                TEXT,
    elapsed_s         INTEGER,
    moving_s          INTEGER,
    distance_m        REAL,
    avg_hr            REAL,
    max_hr            REAL,
    avg_pace_s_per_km REAL,
    avg_cadence       REAL,
    elevation_gain_m  REAL,
    calories          REAL,
    name              TEXT,
    raw_path          TEXT,
    -- Strava extras
    relative_effort   REAL,
    grade_adj_distance_m REAL,
    max_speed_mps     REAL,
    elevation_loss_m  REAL,
    avg_grade         REAL,
    max_grade         REAL,
    avg_watts         REAL,
    training_load     REAL,
    intensity         REAL,
    temp_c            REAL,
    humidity          REAL,
    wind_mps          REAL,
    -- Apple running dynamics
    avg_power_w                 REAL,
    avg_stride_length_m         REAL,
    avg_vertical_oscillation_cm REAL,
    avg_ground_contact_ms       REAL,
    avg_running_speed_mps       REAL,
    UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_activities_start
    ON activities (start_time_utc);

CREATE TABLE IF NOT EXISTS laps (
    activity_id       INTEGER NOT NULL REFERENCES activities (id) ON DELETE CASCADE,
    lap_index         INTEGER NOT NULL,
    elapsed_s         INTEGER,
    distance_m        REAL,
    avg_hr            REAL,
    avg_pace_s_per_km REAL,
    PRIMARY KEY (activity_id, lap_index)
);

-- seq is the point's ordinal position in the track (its identity). offset_s is
-- seconds from the start and is data, not a key: GPS tracks can carry several
-- points within the same whole second, so offset_s is not unique per activity.
CREATE TABLE IF NOT EXISTS stream_points (
    activity_id  INTEGER NOT NULL REFERENCES activities (id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    offset_s     INTEGER,
    distance_m   REAL,
    lat          REAL,
    lng          REAL,
    altitude_m   REAL,
    hr           REAL,
    cadence      REAL,
    velocity_mps REAL,
    watts        REAL,
    PRIMARY KEY (activity_id, seq)
);

CREATE TABLE IF NOT EXISTS health_metrics (
    metric_type    TEXT NOT NULL,
    start_time_utc TEXT NOT NULL,
    end_time_utc   TEXT,
    value          REAL NOT NULL,
    unit           TEXT,
    source         TEXT,
    PRIMARY KEY (metric_type, start_time_utc)
);

CREATE TABLE IF NOT EXISTS activity_links (
    strava_activity_id INTEGER NOT NULL REFERENCES activities (id) ON DELETE CASCADE,
    apple_activity_id  INTEGER NOT NULL REFERENCES activities (id) ON DELETE CASCADE,
    match_confidence   REAL,
    PRIMARY KEY (strava_activity_id, apple_activity_id)
);

CREATE TABLE IF NOT EXISTS raw_files (
    path       TEXT NOT NULL PRIMARY KEY,
    source     TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    sha256     TEXT NOT NULL
);
