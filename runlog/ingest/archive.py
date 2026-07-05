"""Raw landing-zone helper: persist payloads verbatim before parsing.

Every byte we pull from a source is written to disk and recorded in the
``raw_files`` manifest before it is parsed, so the normalized tables can always
be rebuilt from the archive.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from runlog.db import store
from runlog.domain import RawFile

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from runlog.config import Paths
    from runlog.domain import Source, SourceId


def write_raw(
    conn: sqlite3.Connection,
    paths: Paths,
    kind: str,
    source: Source,
    source_id: SourceId,
    filename: str,
    data: bytes,
) -> Path:
    """Write ``data`` to the raw archive for ``kind`` and record it. Returns path.

    ``kind`` selects the landing subdirectory (``strava_api``, ``strava_bulk``,
    ``apple_health``); ``filename`` is the archived file's name within it.
    """
    path = paths.raw_dir(kind) / filename
    path.write_bytes(data)
    store.record_raw_file(
        conn,
        RawFile(
            path=str(path),
            source=source,
            source_id=source_id,
            fetched_at=datetime.now(UTC),
            sha256=hashlib.sha256(data).hexdigest(),
        ),
    )
    return path
