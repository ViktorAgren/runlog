# runlog

`runlog` pulls your runs from Strava and Apple Health into a local SQLite
database, runs analytics over them, and generates training plans with Claude.
Everything runs offline; the data stays on your machine.

It stores per-activity summaries, per-second streams (heart rate, GPS,
elevation, velocity), laps, running dynamics (power, stride length, vertical
oscillation, ground contact), and the health metrics Strava doesn't have
(resting HR, HRV, VO2max, SpO2, sleep, HR-recovery). From those it computes
weekly volume, pace and HR trends, fitness/fatigue/form (CTL/ATL/TSB), ACWR,
intensity distribution, elevation-adjusted pace, critical speed, a daily
readiness score, a ranked performance-limiter diagnosis, and flags days and
runs that deviate from your rolling baseline.

---

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Requires Python ≥ 3.11. Run everything as `python3 -m runlog <command>` (or just
`runlog <command>` once the package is installed). Every command takes `--help`
for its full flag list.

## Configuration

- **Data directory** — all local state lives under `./data` (override with
  `RUNLOG_DATA_DIR`): `data/raw/` archives every payload verbatim,
  `data/runlog.db` is the SQLite query layer, and `data/reports/` and
  `data/plans/` hold generated output. `data/` and `.env` are git-ignored.
- **Secrets** — copy `.env.example` to `.env`. Needed only for the Strava API
  (`STRAVA_CLIENT_ID` / `_SECRET` / `_REFRESH_TOKEN`, used by `strava auth` and
  `strava sync`; bulk import needs none) and for live plan generation
  (`ANTHROPIC_API_KEY`, unless you use `--dry-run`).
- **Athlete profile** (optional) — copy `athlete.toml.example` to
  `data/athlete.toml`. Every command reads it: `hr_max` / `hr_rest` anchor the
  HR zones (so `--hr-max` need not be passed each time; the flag still overrides
  for a single run), and `sex` / `height_cm` / `birth_date` feed the energy
  model (all three required, or it is omitted). Each key also works as a
  `RUNLOG_*` environment variable.

## Quick start

```bash
python3 -m runlog db init                                    # 1. create the DB
python3 -m runlog strava import-bulk ~/Downloads/export.zip  # 2. import Strava
python3 -m runlog apple import ~/Downloads/export.zip        # 2. import Apple
python3 -m runlog link                                       # 3. de-dupe across sources
python3 -m runlog status                                     # 4. verify
python3 -m runlog report                                     # 5. charts + HTML + analytics
python3 -m runlog plan --goal 5k --date 2026-09-01 \
        --days mon,wed,sat --dry-run                         # 6. training plan
```

Already set up? `runlog sync` refreshes everything in order (Strava → Apple →
link) and prints status; add `--report` to regenerate the charts and HTML too.

---

## Commands

### Import and maintenance

- **`db init`** — create the SQLite schema (idempotent).
- **`strava import-bulk PATH`** — import a Strava "Download your data" archive;
  no API or subscription required. **`strava auth`** then **`strava sync`** use
  the API instead, incrementally pulling summaries, laps, and per-point streams
  (`--after DATE`, `--no-streams`); API access needs a Strava subscription (see
  below).
- **`apple import PATH`** — import an Apple Health export: GPX route streams,
  interval laps, and the off-workout markers (resting HR, HRV, VO2max).
- **`link`** — match the same run across Strava and Apple by start-time
  proximity (`--window SECONDS`, default 180); records a confidence-scored link
  without merging or deleting.
- **`sync`** — refresh all sources in order (Strava API → Apple import → link),
  then print status. Apple is imported *after* Strava on purpose, so per-second
  HR and running dynamics backfill onto Strava-logged runs. `--report` also
  renders the dashboard.
- **`status`** — row counts per table and per source.

### Analysis and daily use

- **`report`** — compute analytics, render ~40 PNG charts, and write a
  single-file `report.html` plus a terminal summary (see *Methods* below).
  `--since DATE` limits the window, `--weeks N` the summary length,
  `--min-distance KM` drops aborted starts, and `--hr-max BPM` overrides the
  configured max.
- **`today`** — a morning card: readiness score and its contributors,
  yesterday's load (TRIMP percentile, TSB, ACWR), today's planned session, an
  updating race forecast, and a deterministic **go / easy / rest** call.
- **`last`** — analyses the most recent run (km and HR-zone splits, GAP,
  negative split) and grades its pace and HR against the planned bands
  (`IN BAND` / `FAST OF BAND` / …), flagging new records.
- **`limiters`** — a deterministic, no-LLM diagnosis that ranks what caps
  performance across four axes — **engine** (aerobic ceiling: VDOT trend, and
  under-expression when watch VO2max outruns race-derived VDOT), **speed**
  (short efforts against the critical-speed curve), **durability** (efficiency
  fade, decoupling, Riegel exponent), and **structure** (polarization, monotony,
  ACWR, consistency) — each scored to a common 0–3 severity with a
  research-anchored reason and a prescription. A confidence flag gates the read
  on data sufficiency.

`today`, `last`, and `limiters` need no API; the first two take `--plan PATH`
(default: newest in `data/plans/`).

### Planning

- **`plan`** — generate a plan for a goal, grounded in your data (see *Methods*).
  `--goal` and `--days` are required; `--date` sets a race (omit for an ongoing
  `--weeks` block), with optional `--target-time`, `--max-distance`, and
  `--max-time` caps. Writes markdown to `data/plans/`.
- **`plan-review`** — compare a plan (`--plan`, `--start`) against the runs you
  actually did and return adherence, per-week adjustments, and tips, without
  rewriting the plan.

Both need `ANTHROPIC_API_KEY` unless `--dry-run` prints the grounded prompt to
paste into Claude Code or claude.ai; `--model` selects the Claude model.

### `serve`

A small read-only JSON API over the same analysis layer, for an interactive UI
the static report can't provide (date-range filtering, sorting). Needs the
`web` extra:

```bash
pip install -e '.[web]'
python3 -m runlog serve            # http://127.0.0.1:8000  (--host / --port)
```

Endpoints: `/api/activities`, `/api/activities/{id}`, `/api/weekly-volume`,
`/api/pmc`, `/api/readiness`; interactive docs at `/docs`. It only reads.

---

## Methods

### Importing

The Strava API carries the most detail but needs a subscription (as of 30 June
2026, Standard-tier API access is included in it); `strava import-bulk` captures
the same activities from the account data download with no API. Apple Health
adds what Strava lacks — resting HR, HRV, VO2max, cadence, interval segments —
and covers Apple-Watch-only runs. Ingest is idempotent (keyed on `source` +
`source_id`), so re-running overwrites rather than duplicates; raw payloads are
archived first, so the DB can always be rebuilt.

### Analysing (`report`)

The report prints a text summary (distance, streaks, consistency, records,
Riegel race predictions, training mix and status, intensity split, latest
markers, anomalies, load-response, lifestyle), writes a self-contained
`report.html` (KPI cards plus every figure as base64), and saves charts under
four folders:

- **`training/`** — volume, runs-per-week, cumulative and histogram distance,
  heatmap, pace and grade-adjusted pace, fastest-by-distance, race predictions,
  HR series/zones/histogram, training load, cadence, elevation, running-form
  dynamics (power, stride length, vertical oscillation, ground contact,
  economy), and weekly hours by sport.
- **`recovery/`** — off-workout markers: VO2max, resting HR, HRV, sleep,
  HR-recovery, body mass, walking HR, respiratory rate.
- **`lifestyle/`** — passive patterns: steps, exercise minutes, active energy,
  and a weekday rhythm profile.
- **`analytics/`** — derived models: the Performance Management Chart
  (CTL/ATL/TSB from Banister TRIMP), ACWR (sweet spot 0.8–1.3), efficiency-factor
  trend, aerobic decoupling, best-effort progression, the critical-speed (CS/D′)
  model, intensity distribution, and the anomaly and readiness timelines against
  a rolling baseline.

Per-second streams drive the stream metrics: elevation-adjusted pace (Minetti
cost model), per-run HR-zone splits, stream-integrated TRIMP, and OLS cardiac
drift. Energy expenditure splits each day into resting and active kcal — BMR
from Mifflin–St Jeor over the body-mass series and configured demographics
(a measured basal reading preferred when present), added to Apple's
resting-excluded active energy as `TDEE = BMR + active`; per-run kcal/km is
reported one source at a time, since Strava and Apple model calories
differently. Anomaly detection flags days where resting HR, HRV, SpO2, sleep, or
HR-recovery leave their trailing ~42-day baseline (a red-flag day when ≥2 fire),
plus runs whose efficiency drops below baseline.

Data hygiene: cross-source runs are de-duplicated (the Strava row of each linked
pair kept, Apple-only dynamics coalesced onto it); runs below `--min-distance`
are dropped; implausible paces and out-of-range readings are filtered; corrupted
streams are excluded at ingest; intraday metrics show as daily means; cumulative
daily metrics (steps, energy, flights) are taken from the single
largest-recording device rather than summed across iPhone and Watch; and
off-workout data stays out of training totals.

### Planning (`plan`, `plan-review`)

`plan` builds an athlete profile from your data (recent 90-day volume and pace,
all-time best 1k/5k/10k, current CTL/ATL/TSB and ACWR, VO2max/resting HR) and
computes training zones in code — VDOT paces (Daniels) from your best 5k and
Karvonen HR bands from max + resting HR. Claude then assigns each session a zone
and copies its pace/HR/RPE rather than guessing, tags venue, emits Apple Watch
custom workouts for structured sessions, respects your available days and run
caps, and tapers into race day. The VDOT zones are cross-checked against a
critical-speed model fit to your best efforts, so the hard bands lean on
measured evidence when you outrun the table.

`plan-review` builds a progress report from the DB — per-week volume vs. plan, a
per-workout log reconstructed from the streams, per-rep detail for structured
sessions, an advanced-fitness block (critical speed, best efforts, decoupling),
fitness trajectory, ACWR, and readiness flags — and asks Claude to compare each
performed session to the planned one and advise. The plan markdown is passed
verbatim, so it works with any generated plan.

---

## Data model (SQLite)

| Table | Contents |
|-------|----------|
| `activities` | one row per run/workout per source (summary + Strava extras + Apple running dynamics) |
| `laps` | splits / interval repeats / workout phases (warm-up, work, cool-down) |
| `stream_points` | tidy per-point time series (HR, GPS, altitude, velocity) |
| `health_metrics` | non-workout Apple metrics (resting HR, HRV, VO2max, SpO2, sleep, …) |
| `activity_links` | matched Strava↔Apple activity pairs with a confidence score |
| `raw_files` | manifest of archived raw payloads |

## Development

```bash
pytest -q          # tests
black --check .    # formatting
ruff check .       # lint
mypy               # strict type check
```
