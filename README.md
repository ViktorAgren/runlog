# runlog

Own your running data. `runlog` extracts your runs from **Strava** and **Apple
Health**, stores them locally in a raw archive + a normalized **SQLite**
database, produces **trend charts and training analytics**, and generates a
**personalized, data-grounded training plan** via Claude.

It captures per-activity summaries, per-point streams (heart rate, GPS, pace,
cadence, altitude, power), interval laps, and the health metrics Strava does not
expose (resting HR, HRV, VO2max) — all offline and yours.

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
python3 -m runlog report --hr-max 190                        # 5. charts + analytics
python3 -m runlog plan --goal 5k --date 2026-09-01 \
        --days mon,wed,sat --hr-max 190 --dry-run            # 6. training plan
```

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

### `report`

Compute training analytics and render PNG charts to disk + a terminal summary.
Running only; runs de-duplicated across sources.

| Flag | Default | Description |
| --- | --- | --- |
| `--out DIR` | `data/reports` | Output directory for the `training/`, `recovery/`, `analytics/` PNG folders |
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

---

## How it works

### Importing

- **Strava API** (`strava sync`) is the richest source but needs a Strava
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

Prints an overview (total distance, streaks, consistency, records, Riegel race
predictions, latest VO2max / resting HR / HRV) and writes ~19 running charts to
`training/` (weekly/monthly volume, runs-per-week, cumulative distance, distance
histogram, training heatmap, rest-gap histogram, pace over time, fastest by
distance, pace by weekday, race predictions, HR over time, HR histogram, HR
zones, aerobic efficiency, training load, cadence, elevation by month,
start-time-of-day), 3 passive markers to `recovery/` (VO2max, resting HR, HRV),
and 5 derived models to `analytics/`:

- **Performance Management Chart** — Fitness (CTL) / Fatigue (ATL) / Form (TSB)
  from Banister TRIMP.
- **ACWR** — acute:chronic workload ratio (injury-risk sweet spot 0.8–1.3).
- **Efficiency factor** trend with a fitted regression slope.
- **Aerobic decoupling** — cardiac drift from the HR + velocity streams.
- **Best-effort progression** — fastest continuous 1k/5k/10k over time, via a
  sliding window over the per-point streams.

**Data hygiene** is built in: cross-source runs are de-duplicated (the Strava
row of each linked pair is kept, so totals count each run once); junk activities
below `--min-distance` are dropped; implausible paces and physiologically
impossible health readings are filtered; intraday health metrics are shown as
daily means with a rolling trend; and passive off-workout data (resting HR / HRV
/ VO2max) is kept in a separate `recovery/` folder, never mixed into training
totals. Cadence is derived from total steps ÷ duration, consistent across
Strava (`Total Steps`) and Apple (`StepCount`).

### Planning (`plan`)

Builds an athlete profile from your data (recent 90-day volume/pace, all-time
best 1k/5k/10k, current Fitness/Fatigue/Form and ACWR, VO2max/resting HR) and
computes your **training zones deterministically** — VDOT paces (Daniels) from
your best 5k and Karvonen HR bands from `--hr-max` + resting HR. Claude (Opus
4.8) then assigns each session a zone and copies the **exact** pace/HR/RPE
(nothing is guessed), tags venue (**Track optional** vs Road/GPS), and for
interval/tempo/track sessions produces a ready-to-build **Apple Watch custom
workout** (warm-up → work/recovery reps → cool-down with pace/HR alerts). It
respects your available days and max run distance/time and tapers into race day.

**No API credits?** Add `--dry-run` to print the fully-grounded prompt instead
of calling the API. Paste it into Claude Code or claude.ai (covered by a Pro/Max
subscription) to get the plan for free — the Console API is billed separately
from chat subscriptions.

---

## Data model (SQLite)

| Table | Contents |
|-------|----------|
| `activities` | one row per run/workout per source (summary metrics) |
| `laps` | splits / interval repeats |
| `stream_points` | tidy per-point time series (HR, GPS, pace, cadence, altitude, power) |
| `health_metrics` | non-workout Apple metrics (resting HR, HRV, VO2max, ...) |
| `activity_links` | matched Strava↔Apple activity pairs with a confidence score |
| `raw_files` | manifest of archived raw payloads |

## Development

```bash
pytest -q          # tests
black --check .    # formatting
ruff check .       # lint
mypy               # strict type check
```
