"""Folder ingestion: sweep a directory once, or watch it for dropped files.

The single-file entry point :func:`ingest_file` is shared by ``pf ingest`` and
the watcher, so both resolve a source and report row counts identically.
:func:`watch_folder` wires watchdog's OS filesystem observer to it: a file
that appears in the folder is ingested.

**Deliver files by atomic rename.** A watcher can't tell a completed file from
one still being written — an ``on_created`` event fires the instant a file
appears, before an in-place writer finishes, so ingesting then would silently
land 0 or a partial set of rows. The safe contract is therefore: write the file
somewhere else, then atomically rename it into the watched folder. After the
rename the file is instantly complete, so whichever event fires (a move from
outside the tree surfaces as ``on_created``; a move from within it as
``on_moved`` — and which is which is platform-dependent) reads a whole file.
:func:`deposit_file` (and ``pf deposit``) do exactly this, staging through a
``.part`` name that the watcher's patterns ignore. Writing the final filename
in place (e.g. ``curl -o inbox/x.csv``) is unsupported.

Because ingestion is idempotent (see :mod:`personal_finance.ingest.dedup`),
re-depositing a file — or sweeping a folder that overlaps an earlier one —
never duplicates rows.
"""

import fnmatch
import logging
import os
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from personal_finance.exceptions import IngestionError
from personal_finance.ingest.dedup import bronze_row_count
from personal_finance.ingest.pipeline import run_ingestion

if TYPE_CHECKING:
    from collections.abc import Callable

    from watchdog.observers.api import BaseObserver

    from personal_finance.user_config import SourceConfig

logger = logging.getLogger(__name__)

# Export formats we recognise when sweeping/watching a folder.
DEFAULT_PATTERNS: tuple[str, ...] = ("*.csv", "*.ofx", "*.qfx")

# Suffix for the in-flight staging file used by deposit_file. It must not match
# DEFAULT_PATTERNS so the watcher ignores it until the atomic rename completes.
_STAGING_SUFFIX = ".part"


def deposit_file(src_file: Path, folder: Path, *, name: str | None = None) -> Path:
    """Atomically place a completed file into a watched folder.

    Copies ``src_file`` to a temporary ``.part`` name inside ``folder`` (which
    the watcher's patterns ignore), then atomically renames it to its final
    name, so a watcher only ever observes a complete file. Use this as the last
    step of a download pipeline: download into a staging area, then deposit into
    the watched folder. Returns the final path.
    """
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / (name or src_file.name)
    staging = folder / f"{dest.name}{_STAGING_SUFFIX}"
    shutil.copy2(src_file, staging)
    staging.replace(dest)  # atomic within one directory/filesystem
    return dest


class IngestStatus(StrEnum):
    INGESTED = "ingested"  # ran to completion (new_rows may be 0 if already present)
    UNMATCHED = "unmatched"  # no source config matched the file
    FAILED = "failed"  # the file could not be parsed


@dataclass(frozen=True)
class IngestOutcome:
    """The result of attempting to ingest one file."""

    file: Path
    source: str | None
    status: IngestStatus
    new_rows: int = 0
    total_rows: int = 0
    detail: str | None = None


def ingest_file(
    file_path: Path,
    sources: dict[str, SourceConfig],
    bronze_dir: Path,
    *,
    source_name: str | None = None,
) -> IngestOutcome:
    """Ingest one export file, resolving its source and reporting row counts.

    ``source_name`` forces a source; when ``None`` the source is inferred from
    the file's stem (``chase_checking.csv`` -> ``chase_checking``). Never
    raises for a bad file — parse failures come back as a ``FAILED`` outcome.
    """
    resolved = source_name or file_path.stem
    source = sources.get(resolved)
    if source is None:
        return IngestOutcome(
            file_path,
            resolved,
            IngestStatus.UNMATCHED,
            detail=f"no source config named {resolved!r}",
        )
    before = bronze_row_count(bronze_dir, source.name)
    try:
        run_ingestion(source, file_path, bronze_dir)
    except IngestionError as exc:
        return IngestOutcome(file_path, source.name, IngestStatus.FAILED, detail=str(exc))
    after = bronze_row_count(bronze_dir, source.name)
    return IngestOutcome(
        file_path,
        source.name,
        IngestStatus.INGESTED,
        new_rows=after - before,
        total_rows=after,
    )


def sweep_folder(
    folder: Path,
    sources: dict[str, SourceConfig],
    bronze_dir: Path,
    *,
    source_name: str | None = None,
    patterns: tuple[str, ...] = DEFAULT_PATTERNS,
) -> list[IngestOutcome]:
    """Ingest every matching file already present in ``folder`` (non-recursive)."""
    files = sorted({p for pattern in patterns for p in folder.glob(pattern) if p.is_file()})
    return [ingest_file(p, sources, bronze_dir, source_name=source_name) for p in files]


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


class _ExportEventHandler(FileSystemEventHandler):
    """Translate watchdog file events into ingest calls for matching files."""

    def __init__(self, patterns: tuple[str, ...], on_file: Callable[[Path], None]) -> None:
        self._patterns = patterns
        self._on_file = on_file

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_ingest(event.is_directory, event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        # An atomic rename whose source is inside the watched tree (e.g.
        # deposit_file's `.part` -> final rename). A move from *outside* the
        # tree instead surfaces as on_created — so both handlers are needed.
        self._maybe_ingest(event.is_directory, event.dest_path)

    def _maybe_ingest(self, is_directory: bool, raw_path: str | bytes) -> None:
        if is_directory:
            return
        path = Path(os.fsdecode(raw_path))
        if _matches(path.name, self._patterns):
            self._on_file(path)


def watch_folder(
    folder: Path,
    sources: dict[str, SourceConfig],
    bronze_dir: Path,
    *,
    source_name: str | None = None,
    patterns: tuple[str, ...] = DEFAULT_PATTERNS,
    on_outcome: Callable[[IngestOutcome], None] | None = None,
    sweep_existing: bool = True,
) -> BaseObserver:
    """Start watching ``folder`` and ingest export files as they appear.

    Returns a started watchdog observer that ingests newly created/moved files;
    the caller owns its lifecycle (``observer.stop(); observer.join()``). Any
    matching files already present are then ingested (unless ``sweep_existing``
    is False). ``on_outcome`` is called for every file processed, by the sweep
    and by the observer thread.

    The observer is started *before* the sweep so a file dropped during the
    sweep still fires an event rather than falling into the gap; idempotency
    makes the harmless overlap (caught by both) a no-op on the second pass.
    """

    def handle(path: Path) -> None:
        outcome = ingest_file(path, sources, bronze_dir, source_name=source_name)
        logger.info("ingested %s -> %s (%d new)", path, outcome.source, outcome.new_rows)
        if on_outcome is not None:
            on_outcome(outcome)

    handler = _ExportEventHandler(patterns, handle)
    observer = Observer()
    observer.schedule(handler, str(folder), recursive=False)
    observer.start()

    if sweep_existing:
        for outcome in sweep_folder(
            folder, sources, bronze_dir, source_name=source_name, patterns=patterns
        ):
            if on_outcome is not None:
                on_outcome(outcome)

    return observer
