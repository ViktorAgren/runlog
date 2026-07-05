"""Command-line interface: ``runlog <command>``.

Commands:
    db init                     create the SQLite schema
    strava auth                 one-time OAuth to obtain a refresh token
    strava sync [--after] [--no-streams]
                                incrementally pull activities from the API
    strava import-bulk PATH     import a "Download your data" export ZIP
    apple import PATH           import an Apple Health export ZIP
    link [--window S]           match Strava<->Apple activities by start time
    status                      print row counts per table
    report [--out DIR] [--weeks N] [--since DATE] [--min-distance KM]
                                render trend charts (PNG) + a terminal summary
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

from runlog.config import load_strava_credentials, resolve_paths
from runlog.db import store
from runlog.ingest import apple_ingest, link, strava_ingest
from runlog.sources.strava import auth

if TYPE_CHECKING:
    import sqlite3

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


def _cmd_link(args: argparse.Namespace) -> int:
    linked = link.link_activities(_open_db(), window_s=args.window)
    print(f"Linked {linked} Strava/Apple activity pairs.")
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    counts = store.table_counts(_open_db())
    width = max(len(name) for name in counts)
    for name in sorted(counts):
        print(f"{name:<{width}}  {counts[name]}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from runlog.analyze import report

    paths = resolve_paths()
    out_dir = Path(args.out) if args.out else paths.data_dir / "reports"
    since = date.fromisoformat(args.since) if args.since else None
    result = report.run(
        paths.db_path,
        out_dir,
        recent_weeks=args.weeks,
        since=since,
        min_distance_km=args.min_distance,
        hr_max=args.hr_max,
    )
    print(result.summary_text)
    print(f"\nCharts written to {out_dir}:")
    for path in result.charts:
        print(f"  {path.parent.name}/{path.name}")
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    import math

    import anthropic

    from runlog.plan import generate as generator
    from runlog.plan.profile import PlanRequest, build_profile
    from runlog.plan.render import to_markdown

    paths = resolve_paths()
    race_date = date.fromisoformat(args.date)
    weeks = max(1, math.ceil((race_date - date.today()).days / 7))
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
    profile = build_profile(conn, hr_max=args.hr_max)

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

    out = (
        Path(args.out)
        if args.out
        else paths.data_dir / "plans" / f"plan-{args.goal}-{args.date}.md"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    markdown = to_markdown(plan, profile.zones)
    out.write_text(markdown)
    print(markdown)
    print(f"\nPlan written to {out}")
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

    link_cmd = sub.add_parser("link", help="match Strava<->Apple activities")
    link_cmd.add_argument(
        "--window", type=int, default=180, help="match window in seconds"
    )
    link_cmd.set_defaults(func=_cmd_link)

    sub.add_parser("status", help="print row counts").set_defaults(func=_cmd_status)

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
        help="your max heart rate for HR zones (default: highest recorded)",
    )
    report_cmd.set_defaults(func=_cmd_report)

    plan_cmd = sub.add_parser("plan", help="generate an AI training plan")
    plan_cmd.add_argument("--goal", required=True, help="e.g. 3k, 5k, 10k, half")
    plan_cmd.add_argument("--date", required=True, help="race date, YYYY-MM-DD")
    plan_cmd.add_argument(
        "--days", required=True, help="training days, e.g. mon,wed,sat"
    )
    plan_cmd.add_argument("--target-time", help="optional goal time, e.g. 12:00")
    plan_cmd.add_argument("--max-distance", type=float, help="max km per run")
    plan_cmd.add_argument("--max-time", type=int, help="max minutes per run")
    plan_cmd.add_argument(
        "--hr-max", type=float, help="your true max HR (anchors the HR zones)"
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    handler: object = args.func
    assert callable(handler)
    return int(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
