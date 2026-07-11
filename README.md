# runlog

`runlog` pulls your runs from Strava and Apple Health into a local SQLite
database, runs analytics over them, and generates training plans with Claude.
Everything runs offline; the data stays on your machine.

It stores per-activity summaries, per-second streams (heart rate, GPS,
elevation, velocity), laps, running dynamics (power, stride length, vertical
oscillation, ground contact), and the health metrics Strava doesn't have
(resting HR, HRV, VO2max, SpO2, sleep, HR-recovery). From those it computes
weekly volume, pace and HR trends, fitness/fatigue/form (CTL/ATL/TSB), ACWR,
intensity distribution, elevation-adjusted pace, same-route comparisons, and
flags days and runs that deviate from your rolling baseline.

---

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Requires Python ≥ 3.11. Run everything as `python3 -m runlog <command>` (or just
`runlog <command>` once the package is installed).

## Configuration

- **Data directory** — all local state lives under `./data` (override with the
  `RUNLOG_DATA_DIR` environment variable): `data/raw/` holds every payload
  verbatim, `data/runlog.db` is the SQLite query layer, `data/reports/` and
  `data/plans/` hold generated output. `data/` and `.env` are git-ignored.
- **Secrets** — copy `.env.example` to `.env`. Used only by two features:
  - `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` / `STRAVA_REFRESH_TOKEN` — for
    the Strava API (`strava auth` / `strava sync`). Not needed for bulk import.
  - `ANTHROPIC_API_KEY` — for `plan` (unless you use `--dry-run`).

## Quick start

```bash
python3 -m runlog db init                                    # 1. create the DB
python3 -m runlog strava import-bulk ~/Downloads/export.zip  # 2. import Strava
python3 -m runlog apple import ~/Downloads/export.zip        # 2. import Apple
python3 -m runlog link                                       # 3. de-dupe across sources
python3 -m runlog status                                     # 4. verify
python3 -m runlog report --hr-max 190                        # 5. charts + HTML + analytics
python3 -m runlog plan --goal 5k --date 2026-09-01 \
        --days mon,wed,sat --hr-max 190 --dry-run            # 6. training plan
python3 -m runlog plan-review --plan data/plans/plan-5k-2026-09-01.md \
        --start 2026-07-01 --hr-max 190 --dry-run            # 7. follow up mid-block
```

Already set up? `runlog sync` refreshes everything in order (Strava → Apple →
link) and prints status; add `--report` to regenerate the charts and HTML too.

---

## CLI reference

All commands are subcommands of `runlog`. Flags are optional unless marked
**required**.

### `db init`

Create the SQLite schema (idempotent — safe to re-run). No parameters.

### `strava auth`

Run the one-time OAuth flow to obtain a Strava refresh token, which you paste
into `.env`. No parameters. **Requires a Strava subscription** (API access is
included in it; see note below). Needs `STRAVA_CLIENT_ID`/`SECRET` in `.env`.

### `strava sync`

Incrementally pull new activities from the Strava API (summaries, laps, and
per-point streams). Only fetches activities newer than what you already hold.

| Flag | Default | Description |
| --- | --- | --- |
| `--after DATE` | newest stored run | ISO date (`YYYY-MM-DD`); only import activities after it |
| `--no-streams` | off | Skip per-point streams (faster, summaries + laps only) |

### `strava import-bulk PATH`

Import a Strava "Download your data" archive — **no API, no subscription**.
Request it from *Settings → My Account → Download or Delete Your Account →
Download Request*.

| Argument | Description |
| --- | --- |
| `PATH` (**required**) | Path to the Strava export `.zip` |

### `apple import PATH`

Import an Apple Health export (workouts with GPX route streams + interval laps,
plus resting HR / HRV / VO2max). On iPhone: **Health app → profile picture →
Export All Health Data**, then AirDrop the `export.zip` to your computer.

| Argument | Description |
| --- | --- |
| `PATH` (**required**) | Path to the Apple Health `export.zip` |

### `link`

Match the same run across Strava and Apple by start-time proximity (records a
link with a confidence score — it does **not** merge or delete anything).

| Flag | Default | Description |
| --- | --- | --- |
| `--window SECONDS` | `180` | Max start-time gap to treat two activities as the same run |

### `status`

Print row counts per table (and per-source activity counts). No parameters.

### `sync`

Refresh all sources in the correct order and print status. Runs Strava API sync
(skipped if no credentials), then Apple import, then linking. **Apple is imported
after Strava on purpose** — that's how per-second HR and running dynamics get
backfilled onto new Strava-logged runs (those metrics only exist Apple-side).

| Flag | Default | Description |
| --- | --- | --- |
| `--apple PATH` | the archived export | Apple export `.zip` to import (else re-imports the archived one under `data/raw/`) |
| `--report` | off | Also render the report + HTML dashboard afterwards |

### `report`

Compute analytics, render PNG charts, and write a single-file `report.html` plus
a terminal summary. Running only; runs de-duplicated across sources.

| Flag | Default | Description |
| --- | --- | --- |
| `--out DIR` | `data/reports` | Output dir for the `training/`, `recovery/`, `analytics/` PNGs and `report.html` |
| `--weeks N` | `6` | How many recent weeks to list in the terminal summary |
| `--since DATE` | all history | ISO date; only analyze runs on/after it |
| `--min-distance KM` | `1.0` | Drop runs shorter than this (removes accidental/aborted starts) |
| `--hr-max BPM` | highest recorded | Your true max HR, for accurate HR zones |

### `plan`

Generate a personalized training plan for a goal, grounded in your data. Writes
markdown to `data/plans/` and prints it. Needs `ANTHROPIC_API_KEY` unless
`--dry-run` is used.

| Flag | Default | Description |
| --- | --- | --- |
| `--goal G` (**required**) | — | Race distance, e.g. `3k`, `5k`, `10k`, `half` |
| `--date D` (**required**) | — | Race date, `YYYY-MM-DD` |
| `--days DAYS` (**required**) | — | Available training days, comma-separated, e.g. `mon,wed,sat` |
| `--target-time T` | — | Optional goal finish time, e.g. `12:00` |
| `--max-distance KM` | — | Cap on any single run |
| `--max-time MIN` | — | Cap on any single run's duration |
| `--hr-max BPM` | highest recorded | Your true max HR (anchors the Karvonen HR zones) |
| `--out PATH` | `data/plans/plan-<goal>-<date>.md` | Output markdown path |
| `--model NAME` | `claude-opus-4-8` | Claude model (e.g. `claude-sonnet-4-6`) |
| `--dry-run` | off | Print the grounded prompt instead of calling the API (no key/credits) |

### `plan-review`

Follow up on a plan you're partway through: compares the plan against every run
you actually did since it started and returns an adherence assessment, per-week
adjustments, tips, and watch-outs (**tips only — it never rewrites the plan**).
Same dry-run/API model as `plan`. Writes markdown to `data/plans/`.

| Flag | Default | Description |
| --- | --- | --- |
| `--plan PATH` (**required**) | — | The plan markdown file to review against |
| `--start DATE` (**required**) | — | The date the plan block began, `YYYY-MM-DD` |
| `--hr-max BPM` | highest recorded | Your true max HR (anchors the HR zones) |
| `--out PATH` | `data/plans/review-<plan>-<today>.md` | Output markdown path |
| `--model NAME` | `claude-opus-4-8` | Claude model to use |
| `--dry-run` | off | Print the review prompt instead of calling the API (no key/credits) |

---

## How it works

### Importing

- **Strava API** (`strava sync`) has the most detail but needs a Strava
  subscription: as of 30 June 2026 Standard-tier API access is included in the
  subscription. No subscription? `strava import-bulk` uses the account data
  download and captures the same activities with no API access.
- **Apple Health** (`apple import`) adds what Strava lacks — resting HR, HRV,
  VO2max, cadence (from step count), and interval segments — and covers
  Apple-Watch-only runs (e.g. track intervals you don't push to Strava).
- **Ingest is idempotent** (keyed on `source` + `source_id`): re-running any
  import or sync overwrites rather than duplicates. Raw payloads are archived
  under `data/raw/` first, so the DB can always be rebuilt.

### Analyzing (`report`)

Prints a text summary (total distance, streaks, consistency, records, Riegel
race predictions, training status, intensity split, latest markers, anomalies),
writes `report.html` (KPI cards + every figure embedded as base64, so it's one
file), and saves ~30 charts under three folders:

- **`training/`** — weekly/monthly volume, runs-per-week, cumulative distance,
  distance histogram, training heatmap, rest gaps, pace over time,
  grade-adjusted pace, fastest by distance, pace by weekday, best-effort
  progression, same-route pace, race predictions, HR over time, HR zones, HR
  histogram, aerobic efficiency, training load, start time of day, running-form
  dynamics (power, stride length, vertical oscillation, ground contact), climb,
  and pacing.
- **`recovery/`** — off-workout markers: VO2max, resting HR, HRV, SpO2, sleep,
  HR-recovery, body mass, walking asymmetry.
- **`analytics/`** — derived models:
  - **Performance Management Chart** — Fitness (CTL) / Fatigue (ATL) / Form (TSB)
    from Banister TRIMP.
  - **ACWR** — acute:chronic workload ratio (sweet spot 0.8–1.3).
  - **Efficiency factor** trend with a fitted regression slope.
  - **Aerobic decoupling** and **cardiac drift** from the HR + velocity streams.
  - **Best-effort progression** — fastest continuous 1k/5k/10k over time.
  - **Intensity distribution** — % time easy / moderate / hard.
  - **Anomaly timeline** — red-flag days and off-runs vs. baseline.

The per-second streams (GPS, elevation, velocity, reconstructed HR) drive the
stream metrics: elevation-adjusted pace (Minetti cost model), same-route
grouping, per-run HR-zone splits, stream-integrated TRIMP, and OLS cardiac
drift.

Anomaly detection flags days where resting HR, HRV, SpO2, sleep, or HR-recovery
deviate from their trailing ~42-day baseline (a red-flag day when ≥2 fire
together), plus runs whose efficiency drops below baseline.

Data hygiene: cross-source runs are de-duplicated (the Strava row of each linked
pair is kept, Apple-only running dynamics coalesced onto it); runs below
`--min-distance` are dropped; implausible paces and out-of-range health readings
are filtered; corrupted streams are flagged at ingest and excluded; intraday
health metrics show as daily means; and off-workout data stays in `recovery/`,
out of training totals.

### Planning (`plan`)

Builds an athlete profile from your data (recent 90-day volume/pace, all-time
best 1k/5k/10k, current Fitness/Fatigue/Form and ACWR, VO2max/resting HR) and
computes your training zones in code — VDOT paces (Daniels) from your best 5k
and Karvonen HR bands from `--hr-max` + resting HR. Claude (Opus 4.8) then
assigns each session a zone and copies the pace/HR/RPE from it (rather than
guessing), tags venue (Track optional vs Road/GPS), and for interval/tempo/track
sessions produces an Apple Watch custom workout (warm-up → work/recovery reps →
cool-down with pace/HR alerts). It respects your available days and max run
distance/time and tapers into race day.

No API credits? Add `--dry-run` to print the prompt instead of calling the API.
Paste it into Claude Code or claude.ai (covered by a Pro/Max subscription) — the
Console API is billed separately from chat subscriptions.

### Following up (`plan-review`)

Point `plan-review` at the plan markdown plus its start date. It builds a
progress report from your DB — per-plan-week volume vs. plan, a per-workout log
(date, kind, distance, pace, HR, zone split, GAP) reconstructed from the
streams, fitness trajectory (CTL then vs. now), ACWR, and readiness flags — and
asks Claude to compare each performed session to the planned one and advise. The
plan markdown is passed verbatim (never re-parsed), so it works with any plan
you've generated. Same `--dry-run` path as `plan`.

---

## Data model (SQLite)

| Table | Contents |
|-------|----------|
| `activities` | one row per run/workout per source (summary + Strava extras + Apple running dynamics) |
| `laps` | splits / interval repeats / workout phases (warm-up, work, cool-down) |
| `stream_points` | tidy per-point time series (HR, GPS, altitude, velocity) |
| `health_metrics` | non-workout Apple metrics (resting HR, HRV, VO2max, SpO2, sleep, ...) |
| `activity_links` | matched Strava↔Apple activity pairs with a confidence score |
| `raw_files` | manifest of archived raw payloads |

## Development

```bash
pytest -q          # tests
black --check .    # formatting
ruff check .       # lint
mypy               # strict type check
```
