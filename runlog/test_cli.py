"""End-to-end CLI test exercising the offline commands over a temp data dir."""

from __future__ import annotations

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
