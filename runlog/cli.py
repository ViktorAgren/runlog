"""Command-line interface: ``runlog <command>``.

Commands:
    db init                     create the SQLite schema
    strava auth                 one-time OAuth to obtain a refresh token
    strava sync [--after] [--no-streams]
                                incrementally pull activities from the API
    strava import-bulk PATH     import a "Download your data" export ZIP
    apple import PATH           import an Apple Health export ZIP
    runna import PATH           import a Runna export ZIP (treadmill-authoritative)
    link [--window S]           match Strava<->Apple activities by start time
    status                      print row counts per table
    sync [--apple PATH] [--report]
                                refresh all sources in order (Strava -> Apple ->
                                link) then print status
    report [--out DIR] [--weeks N] [--since DATE] [--min-distance KM]
                                render trend charts (PNG) + a terminal summary
    today [--plan PATH] [--hr-max N]
                                morning readiness + planned-session card
    last [--plan PATH] [--hr-max N]
                                post-run analysis of the latest run vs the plan
    plan --goal G --date D --days mon,wed,sat [--target-time T]
         [--max-distance KM] [--max-time MIN] [--dry-run]
                                generate an AI training plan (needs API key;
                                --dry-run prints the prompt instead)
"""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from runlog.config import load_athlete_config, load_strava_credentials, resolve_paths
from runlog.db import store
from runlog.ingest import apple_ingest, link, runna_ingest, strava_ingest
from runlog.sources.strava import auth

if TYPE_CHECKING:
    import sqlite3

    from runlog.config import AthleteConfig, Paths
    from runlog.plan.schedule import PlanSchedule


def _hr_from(
    args: argparse.Namespace, cfg: AthleteConfig
) -> tuple[float | None, float | None]:
    """HR max/rest to pass down: an explicit --hr-max flag overrides the profile."""
    hr_max = args.hr_max if args.hr_max is not None else cfg.hr_max
    return hr_max, cfg.hr_rest


_REDIRECT_URI = "http://localhost"


def _open_db() -> sqlite3.Connection:
    """Open the configured DB, ensuring the schema exists."""
    conn = store.connect(resolve_paths().db_path)
    store.init_db(conn)
    return conn


def _cmd_db_init(_args: argparse.Namespace) -> int:
    _open_db()
    print(f"Initialized database at {resolve_paths().db_path}")
    return 0


def _cmd_strava_auth(_args: argparse.Namespace) -> int:
    creds = load_strava_credentials()
    url = auth.authorize_url(creds.client_id, _REDIRECT_URI)
    print("1. Open this URL and authorize access:\n")
    print(f"   {url}\n")
    print("2. After approving you are redirected to a URL like")
    print("   http://localhost/?state=&code=<CODE>&scope=...")
    code = input("3. Paste the 'code' value here: ").strip()
    tokens = auth.exchange_code(creds, code)
    print("\nSuccess. Add this line to your .env:\n")
    print(f"   STRAVA_REFRESH_TOKEN={tokens.refresh_token}")
    return 0


def _cmd_strava_sync(args: argparse.Namespace) -> int:
    creds = load_strava_credentials()
    after = (
        datetime.fromisoformat(args.after).replace(tzinfo=UTC) if args.after else None
    )
    stored = strava_ingest.sync_api(
        _open_db(),
        resolve_paths(),
        creds,
        after=after,
        with_streams=not args.no_streams,
    )
    print(f"Synced {stored} activities from the Strava API.")
    return 0


def _cmd_strava_import_bulk(args: argparse.Namespace) -> int:
    stored = strava_ingest.import_bulk_archive(
        _open_db(), resolve_paths(), Path(args.path)
    )
    print(f"Imported {stored} activities from {args.path}.")
    return 0


def _cmd_apple_import(args: argparse.Namespace) -> int:
    workouts, metrics = apple_ingest.import_export(
        _open_db(), resolve_paths(), Path(args.path)
    )
    print(f"Imported {workouts} workouts and {metrics} health metrics.")
    return 0


def _cmd_runna_import(args: argparse.Namespace) -> int:
    stored = runna_ingest.import_export(_open_db(), resolve_paths(), Path(args.path))
    print(f"Imported {stored} Runna activities.")
    return 0


def _latest_runna_export(paths: Paths) -> Path | None:
    exports = sorted(paths.raw_dir("runna").glob("*.zip"))
    return exports[-1] if exports else None


def _cmd_link(args: argparse.Namespace) -> int:
    conn = _open_db()
    linked = link.link_activities(conn, window_s=args.window)
    runna_linked = link.link_runna_apple(conn, window_s=args.window)
    print(
        f"Linked {linked} Strava/Apple and {runna_linked} Runna/Apple activity pairs."
    )
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    counts = store.table_counts(_open_db())
    width = max(len(name) for name in counts)
    for name in sorted(counts):
        print(f"{name:<{width}}  {counts[name]}")
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    """Refresh all sources in order: Strava -> Apple -> Runna -> link.

    Apple is imported after Strava so per-second HR and running dynamics are
    backfilled onto any new Strava-logged runs (they only exist Apple-side).
    Runna follows so each activity's Strava twin exists to link exactly.
    """
    paths = resolve_paths()
    conn = _open_db()

    try:
        creds = load_strava_credentials()
    except RuntimeError:
        print("Strava: no credentials configured, skipping API sync.")
    else:
        stored = strava_ingest.sync_api(conn, paths, creds)
        print(f"Strava: synced {stored} activities from the API.")

    apple_path = (
        Path(args.apple) if args.apple else paths.raw_dir("apple_health") / "export.zip"
    )
    if apple_path.exists():
        workouts, metrics = apple_ingest.import_export(conn, paths, apple_path)
        print(f"Apple: imported {workouts} workouts and {metrics} health metrics.")
    else:
        print(f"Apple: no export at {apple_path}, skipping.")

    runna_path = Path(args.runna) if args.runna else _latest_runna_export(paths)
    if runna_path is not None and runna_path.exists():
        count = runna_ingest.import_export(conn, paths, runna_path)
        print(f"Runna: imported {count} activities.")
    else:
        print("Runna: no export found, skipping.")

    linked = link.link_activities(conn)
    print(f"Linked {linked} Strava/Apple activity pairs.\n")

    counts = store.table_counts(conn)
    width = max(len(name) for name in counts)
    for name in sorted(counts):
        print(f"{name:<{width}}  {counts[name]}")

    if args.report:
        from runlog.analyze import report

        result = report.run(paths.db_path, paths.data_dir / "reports")
        print(f"\nReport written; HTML at {result.report_html}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from runlog.analyze import report

    paths = resolve_paths()
    cfg = load_athlete_config(paths)
    out_dir = Path(args.out) if args.out else paths.data_dir / "reports"
    since = date.fromisoformat(args.since) if args.since else None
    hr_max, hr_rest = _hr_from(args, cfg)
    result = report.run(
        paths.db_path,
        out_dir,
        recent_weeks=args.weeks,
        since=since,
        min_distance_km=args.min_distance,
        hr_max=hr_max,
        hr_rest=hr_rest,
        sex=cfg.sex,
        athlete=cfg.demographics(),
        schedule=_resolve_schedule(getattr(args, "plan", None)),
        save=not args.no_save,
    )
    print(result.summary_text)
    print(f"\nCharts written to {out_dir}:")
    for path in result.charts:
        print(f"  {path.parent.name}/{path.name}")
    if result.report_html is not None:
        print(f"\nHTML report: {result.report_html}")
    for path in result.data_exports:
        print(f"Derived data: {path}")
    return 0


def _cmd_series(args: argparse.Namespace) -> int:
    from runlog.analyze import persist

    paths = resolve_paths()
    out_dir = Path(args.out) if args.out else paths.data_dir / "reports"
    conn = store.connect(paths.db_path)
    store.init_db(conn)
    exports = persist.export(conn, out_dir)
    for path in exports:
        print(f"Exported {path}")
    return 0


def _resolve_schedule(plan_arg: str | None) -> PlanSchedule | None:
    """Load the given plan file, else the newest one in data/plans."""
    from runlog.plan import schedule

    if plan_arg:
        return schedule.load_schedule(Path(plan_arg))
    active = schedule.find_active_plan(resolve_paths().data_dir / "plans")
    return schedule.load_schedule(active) if active is not None else None


def _cmd_today(args: argparse.Namespace) -> int:
    from runlog.coach import daily, render

    conn = _open_db()
    cfg = load_athlete_config()
    hr_max, hr_rest = _hr_from(args, cfg)
    card = daily.build_today(
        conn,
        _resolve_schedule(args.plan),
        date.today(),
        hr_max=hr_max,
        hr_rest=hr_rest,
        sex=cfg.sex,
    )
    print(render.render_today(card))
    return 0


def _cmd_last(args: argparse.Namespace) -> int:
    from runlog.coach import daily, render

    conn = _open_db()
    hr_max, hr_rest = _hr_from(args, load_athlete_config())
    card = daily.build_last(
        conn,
        _resolve_schedule(args.plan),
        date.today(),
        hr_max=hr_max,
        hr_rest=hr_rest,
    )
    if card is None:
        print("No runs in the database yet.")
        return 1
    print(render.render_last(card))
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    import math

    import anthropic

    from runlog.plan import generate as generator
    from runlog.plan.profile import PlanRequest, build_profile
    from runlog.plan.render import to_markdown

    paths = resolve_paths()
    _DEFAULT_BLOCK_WEEKS = 8
    race_date = date.fromisoformat(args.date) if args.date else None
    if race_date is not None:
        weeks = max(1, math.ceil((race_date - date.today()).days / 7))
    else:
        weeks = args.weeks or _DEFAULT_BLOCK_WEEKS
    days = tuple(d.strip().capitalize() for d in args.days.split(",") if d.strip())
    request = PlanRequest(
        goal=args.goal,
        race_date=race_date,
        training_days=days,
        weeks_to_goal=weeks,
        target_time=args.target_time,
        max_distance_km=args.max_distance,
        max_time_min=args.max_time,
    )
    conn = store.connect(paths.db_path)
    store.init_db(conn)
    cfg = load_athlete_config(paths)
    hr_max, hr_rest = _hr_from(args, cfg)
    profile = build_profile(conn, hr_max=hr_max, hr_rest=hr_rest, sex=cfg.sex)

    if args.dry_run:
        from runlog.plan.prompt import dry_run_text

        print(dry_run_text(profile, request))
        return 0

    try:
        plan = generator.generate(profile, request, model=args.model)
    except (anthropic.AnthropicError, RuntimeError) as error:
        print(
            f"Plan generation failed: {error}\n"
            "Set ANTHROPIC_API_KEY in .env (see .env.example)."
        )
        return 1

    stem = args.date if race_date is not None else f"ongoing-{date.today()}"
    out = (
        Path(args.out)
        if args.out
        else paths.data_dir / "plans" / f"plan-{args.goal}-{stem}.md"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    markdown = to_markdown(plan, profile.zones)
    out.write_text(markdown)
    print(markdown)
    print(f"\nPlan written to {out}")
    return 0


def _cmd_plan_review(args: argparse.Namespace) -> int:
    import anthropic

    from runlog.plan import generate as generator
    from runlog.plan.profile import build_profile
    from runlog.plan.progress import build_progress
    from runlog.plan.prompt import review_dry_run_text
    from runlog.plan.render import review_to_markdown

    plan_path = Path(args.plan)
    if not plan_path.exists():
        print(f"Plan file not found: {plan_path}")
        return 1
    plan_md = plan_path.read_text()
    start = date.fromisoformat(args.start)

    paths = resolve_paths()
    conn = store.connect(paths.db_path)
    store.init_db(conn)
    cfg = load_athlete_config(paths)
    hr_max, hr_rest = _hr_from(args, cfg)
    profile = build_profile(conn, hr_max=hr_max, hr_rest=hr_rest, sex=cfg.sex)
    progress = build_progress(conn, start, hr_max=hr_max, hr_rest=hr_rest, sex=cfg.sex)

    if args.dry_run:
        print(review_dry_run_text(plan_md, progress, profile))
        return 0

    try:
        review = generator.generate_review(plan_md, progress, profile, model=args.model)
    except (anthropic.AnthropicError, RuntimeError) as error:
        print(
            f"Review generation failed: {error}\n"
            "Set ANTHROPIC_API_KEY in .env (see .env.example)."
        )
        return 1

    out = (
        Path(args.out)
        if args.out
        else paths.data_dir / "plans" / f"review-{plan_path.stem}-{date.today()}.md"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    markdown = review_to_markdown(review)
    out.write_text(markdown)
    print(markdown)
    print(f"\nReview written to {out}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from runlog.web import create_app
    except ImportError:
        print("The JSON API needs the web extra: pip install 'runlog[web]'.")
        return 1

    paths = resolve_paths()
    uvicorn.run(create_app(paths.db_path), host=args.host, port=args.port)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="runlog", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    db = sub.add_parser("db", help="database maintenance").add_subparsers(
        dest="db_command", required=True
    )
    db.add_parser("init", help="create the schema").set_defaults(func=_cmd_db_init)

    strava = sub.add_parser("strava", help="Strava API and bulk import")
    strava_sub = strava.add_subparsers(dest="strava_command", required=True)
    strava_sub.add_parser("auth", help="obtain a refresh token").set_defaults(
        func=_cmd_strava_auth
    )
    sync = strava_sub.add_parser("sync", help="incremental API sync")
    sync.add_argument("--after", help="ISO date; only activities after it")
    sync.add_argument(
        "--no-streams", action="store_true", help="skip per-point streams"
    )
    sync.set_defaults(func=_cmd_strava_sync)
    bulk = strava_sub.add_parser("import-bulk", help="import an export ZIP")
    bulk.add_argument("path", help="path to the Strava export ZIP")
    bulk.set_defaults(func=_cmd_strava_import_bulk)

    apple = sub.add_parser("apple", help="Apple Health import")
    apple_import = apple.add_subparsers(dest="apple_command", required=True).add_parser(
        "import", help="import an Apple Health export ZIP"
    )
    apple_import.add_argument("path", help="path to the Apple Health export ZIP")
    apple_import.set_defaults(func=_cmd_apple_import)

    runna = sub.add_parser("runna", help="Runna export import")
    runna_import = runna.add_subparsers(dest="runna_command", required=True).add_parser(
        "import", help="import a Runna export ZIP"
    )
    runna_import.add_argument("path", help="path to the Runna export ZIP")
    runna_import.set_defaults(func=_cmd_runna_import)

    link_cmd = sub.add_parser("link", help="match Strava<->Apple activities")
    link_cmd.add_argument(
        "--window", type=int, default=180, help="match window in seconds"
    )
    link_cmd.set_defaults(func=_cmd_link)

    sub.add_parser("status", help="print row counts").set_defaults(func=_cmd_status)

    sync_all = sub.add_parser(
        "sync", help="refresh all sources: Strava -> Apple -> link (+ status)"
    )
    sync_all.add_argument(
        "--apple", help="Apple export ZIP (default: the archived export)"
    )
    sync_all.add_argument(
        "--runna", help="Runna export ZIP (default: the newest archived export)"
    )
    sync_all.add_argument(
        "--report", action="store_true", help="also render the report afterwards"
    )
    sync_all.set_defaults(func=_cmd_sync)

    report_cmd = sub.add_parser("report", help="generate trend charts + summary")
    report_cmd.add_argument("--out", help="output directory (default data/reports)")
    report_cmd.add_argument(
        "--weeks", type=int, default=6, help="recent weeks to show in the summary"
    )
    report_cmd.add_argument(
        "--since", help="ISO date; only analyze runs on/after it (e.g. 2025-07-01)"
    )
    report_cmd.add_argument(
        "--min-distance",
        type=float,
        default=1.0,
        help="drop runs shorter than this many km (default 1.0)",
    )
    report_cmd.add_argument(
        "--hr-max",
        type=float,
        help="max HR for zones (overrides athlete.toml; default: highest recorded)",
    )
    report_cmd.add_argument(
        "--no-save",
        action="store_true",
        help="do not persist/export the derived timeseries",
    )
    report_cmd.set_defaults(func=_cmd_report)

    today_cmd = sub.add_parser("today", help="morning readiness + planned-session card")
    today_cmd.add_argument(
        "--plan", help="plan markdown path (default: newest in data/plans)"
    )
    today_cmd.add_argument(
        "--hr-max", type=float, help="your true max HR (overrides athlete.toml)"
    )
    today_cmd.set_defaults(func=_cmd_today)

    last_cmd = sub.add_parser(
        "last", help="post-run analysis of your most recent run vs the plan"
    )
    last_cmd.add_argument(
        "--plan", help="plan markdown path (default: newest in data/plans)"
    )
    last_cmd.add_argument(
        "--hr-max", type=float, help="your true max HR (overrides athlete.toml)"
    )
    last_cmd.set_defaults(func=_cmd_last)

    plan_cmd = sub.add_parser("plan", help="generate an AI training plan")
    plan_cmd.add_argument("--goal", required=True, help="e.g. 3k, 5k, 10k, half")
    plan_cmd.add_argument(
        "--date", help="race date, YYYY-MM-DD (omit for an ongoing block)"
    )
    plan_cmd.add_argument(
        "--weeks",
        type=int,
        help="block length in weeks when no --date is given (default 8)",
    )
    plan_cmd.add_argument(
        "--days", required=True, help="training days, e.g. mon,wed,sat"
    )
    plan_cmd.add_argument("--target-time", help="optional goal time, e.g. 12:00")
    plan_cmd.add_argument("--max-distance", type=float, help="max km per run")
    plan_cmd.add_argument("--max-time", type=int, help="max minutes per run")
    plan_cmd.add_argument(
        "--hr-max", type=float, help="your true max HR (overrides athlete.toml)"
    )
    plan_cmd.add_argument("--out", help="output markdown path")
    plan_cmd.add_argument(
        "--model", default="claude-opus-4-8", help="Claude model to use"
    )
    plan_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="print the grounded prompt instead of calling the API (no key/credits)",
    )
    plan_cmd.set_defaults(func=_cmd_plan)

    review_cmd = sub.add_parser(
        "plan-review", help="review progress against an existing plan"
    )
    review_cmd.add_argument(
        "--plan", required=True, help="path to the plan markdown file"
    )
    review_cmd.add_argument(
        "--start", required=True, help="date the plan block began, YYYY-MM-DD"
    )
    review_cmd.add_argument(
        "--hr-max", type=float, help="your true max HR (overrides athlete.toml)"
    )
    review_cmd.add_argument("--out", help="output markdown path")
    review_cmd.add_argument(
        "--model", default="claude-opus-4-8", help="Claude model to use"
    )
    review_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="print the review prompt instead of calling the API (no key/credits)",
    )
    review_cmd.set_defaults(func=_cmd_plan_review)

    series_cmd = sub.add_parser(
        "series", help="export the persisted derived timeseries as CSV"
    )
    series_cmd.add_argument("--out", help="output directory (default: data/reports)")
    series_cmd.set_defaults(func=_cmd_series)

    serve_cmd = sub.add_parser("serve", help="run the read-only JSON API (web extra)")
    serve_cmd.add_argument("--host", default="127.0.0.1", help="bind host")
    serve_cmd.add_argument("--port", type=int, default=8000, help="bind port")
    serve_cmd.set_defaults(func=_cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    handler: object = args.func
    assert callable(handler)
    return int(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
