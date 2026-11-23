"""End-to-end CLI test exercising the offline commands over a temp data dir."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

import pytest

from runlog.cli import main
from runlog.db import store
from runlog.ingest.test_apple_ingest import _build_export
from runlog.ingest.test_strava_ingest import _build_archive


def test_cli_pipeline_imports_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("RUNLOG_DATA_DIR", str(data_dir))
    bulk_zip = tmp_path / "strava.zip"
    apple_zip = tmp_path / "apple.zip"
    _build_archive(bulk_zip)
    _build_export(apple_zip)

    exit_codes = [
        main(["db", "init"]),
        main(["strava", "import-bulk", str(bulk_zip)]),
        main(["apple", "import", str(apple_zip)]),
        main(["link"]),
        main(["status"]),
    ]

    conn = store.connect(data_dir / "runlog.db")
    assert (exit_codes, store.table_counts(conn)) == (
        [0, 0, 0, 0, 0],
        {
            "activities": 3,
            "laps": 2,
            "stream_points": 4,
            "health_metrics": 3,
            "activity_links": 0,
            "runna_links": 0,
            "raw_files": 2,
            "activities:strava": 2,
            "activities:apple_health": 1,
        },
    )


def _no_strava_creds() -> None:
    raise RuntimeError("no credentials")


def test_cli_sync_imports_apple_and_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("RUNLOG_DATA_DIR", str(data_dir))
    # Force the Strava-skip path so the test needs no credentials or network.
    monkeypatch.setattr("runlog.cli.load_strava_credentials", _no_strava_creds)
    bulk_zip = tmp_path / "strava.zip"
    apple_zip = tmp_path / "apple.zip"
    _build_archive(bulk_zip)
    _build_export(apple_zip)

    main(["db", "init"])
    main(["strava", "import-bulk", str(bulk_zip)])
    code = main(["sync", "--apple", str(apple_zip)])

    out = capsys.readouterr().out
    conn = store.connect(data_dir / "runlog.db")
    counts = store.table_counts(conn)
    assert (code, "no credentials configured" in out) == (0, True)
    # Apple import ran after Strava (2 strava + 1 apple), then link executed.
    assert (counts["activities:strava"], counts["activities:apple_health"]) == (2, 1)


def test_cli_requires_a_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main([])


def test_cli_plan_ongoing_and_dated_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RUNLOG_DATA_DIR", str(tmp_path / "data"))
    assert main(["db", "init"]) == 0
    capsys.readouterr()

    # No --date -> ongoing 8-week block; the prompt shows a HORIZON, not a race.
    assert main(["plan", "--goal", "10k", "--days", "mon,tue,wed", "--dry-run"]) == 0
    ongoing = capsys.readouterr().out
    assert "HORIZON: rolling 8-week block" in ongoing and "RACE DATE:" not in ongoing

    # A --date still drives the dated race path.
    assert (
        main(
            [
                "plan",
                "--goal",
                "3k",
                "--date",
                "2026-12-01",
                "--days",
                "mon,wed",
                "--dry-run",
            ]
        )
        == 0
    )
    assert "RACE DATE: 2026-12-01" in capsys.readouterr().out


def _write_plan(plans_dir: Path) -> None:
    plans_dir.mkdir(parents=True)
    (plans_dir / "plan-10k-ongoing-2026-11-16.md").write_text(
        "# Training plan — 10k\n"
        "block start 2026-11-16\n\n"
        "| Date | Day | Type | Pace | HR | Session |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| Nov 16 | Mon | Easy | 5:40/km | 133-148 | 8 km |\n"
        "| Nov 18 | Wed | Intervals | 4:00/km | 178-193 | 4x3min |\n"
    )


def _recording(
    calls: dict[str, argparse.Namespace], key: str
) -> Callable[[argparse.Namespace], int]:
    def handler(args: argparse.Namespace) -> int:
        calls[key] = args
        return 0

    return handler


def test_cli_weekly_chains_sync_then_resolves_plan_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("RUNLOG_DATA_DIR", str(data_dir))
    _write_plan(data_dir / "plans")
    calls: dict[str, argparse.Namespace] = {}
    monkeypatch.setattr("runlog.cli._cmd_sync", _recording(calls, "sync"))
    monkeypatch.setattr("runlog.cli._cmd_plan_review", _recording(calls, "review"))

    code = main(["weekly"])

    assert (code, calls["sync"].report, calls["review"].start) == (
        0,
        True,
        "2026-11-16",
    )
    assert str(calls["review"].plan).endswith("plan-10k-ongoing-2026-11-16.md")


def test_cli_weekly_no_review_stops_after_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNLOG_DATA_DIR", str(tmp_path / "data"))
    calls: dict[str, argparse.Namespace] = {}
    monkeypatch.setattr("runlog.cli._cmd_sync", _recording(calls, "sync"))
    monkeypatch.setattr("runlog.cli._cmd_plan_review", _recording(calls, "review"))

    code = main(["weekly", "--no-review"])

    assert (code, "sync" in calls, "review" in calls) == (0, True, False)


def test_cli_today_and_last_on_empty_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RUNLOG_DATA_DIR", str(tmp_path / "data"))
    assert main(["db", "init"]) == 0
    capsys.readouterr()  # drop the db-init output

    today_code = main(["today"])
    today_out = capsys.readouterr().out
    last_code = main(["last"])
    last_out = capsys.readouterr().out

    # today always renders; last exits 1 with a message when there are no runs.
    assert (today_code, last_code) == (0, 1)
    assert today_out.startswith("TODAY —")
    assert "no active plan schedule" in today_out
    assert "No runs in the database yet." in last_out
