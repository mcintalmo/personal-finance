"""Run a source's dlt resource into the Parquet-backed bronze layer."""

import threading
from typing import TYPE_CHECKING

import dlt
from dlt.destinations import filesystem

from personal_finance.exceptions import IngestionError
from personal_finance.ingest.amazon_source import amazon_order_items
from personal_finance.ingest.csv_source import csv_transactions
from personal_finance.ingest.dedup import existing_row_hashes
from personal_finance.ingest.ofx_source import ofx_transactions
from personal_finance.user_config import SourceKind

if TYPE_CHECKING:
    from pathlib import Path

    from dlt.common.pipeline import LoadInfo
    from dlt.extract import DltResource

    from personal_finance.user_config import SourceConfig

# Ingestion is serialized process-wide. dlt keys its pipeline working directory
# by pipeline_name, and our dedup reads a source's existing hashes before
# appending — so two ingests running at once (e.g. the folder watcher's initial
# sweep on the main thread overlapping an event on the observer thread) would
# collide on that directory and each read stale hashes, breaking idempotency.
# For a local single-user tool, serializing all ingestion is simplest and
# correct; throughput is not a concern.
_INGEST_LOCK = threading.Lock()


def _run(
    source: SourceConfig, resource: DltResource, bronze_dir: Path, dataset_name: str = "bronze"
) -> LoadInfo:
    """Load one resource into ``bronze_dir/<dataset_name>/<source.name>/`` as Parquet.

    Idempotent append: bronze is append-only (dlt's filesystem destination has
    no merge), so before appending we drop any row whose ``row_hash`` already
    exists in this source's bronze table. Re-ingesting the same file — or a
    later export whose date range overlaps an earlier one — therefore adds no
    duplicates, while genuinely-new rows still land. See ``dedup`` for how the
    hash is keyed. The whole read-then-append is held under ``_INGEST_LOCK`` so
    concurrent ingests can't interleave.

    ``dataset_name`` defaults to "bronze", the dataset every transaction-shaped
    CSV/OFX source shares (dbt's ``bronze.transactions`` source globs
    ``bronze/*/*.parquet`` — every subdirectory, auto-discovering a new bank
    with no dbt change needed). A source whose rows are NOT transaction-shaped
    (e.g. Amazon order-history — see ``amazon_source``) must land under a
    *different* dataset, or that same wildcard glob would silently sweep it
    into the generic transactions union too, corrupting it with NULL-heavy
    rows for every column it doesn't share.

    Any IngestionError raised inside the resource is wrapped by dlt in
    PipelineStepFailed/ResourceExtractionError; this unwraps that chain so
    callers only ever see our exception type, per the exception-boundary
    convention.
    """
    with _INGEST_LOCK:
        return _run_locked(source, resource, bronze_dir, dataset_name)


def _run_locked(
    source: SourceConfig, resource: DltResource, bronze_dir: Path, dataset_name: str
) -> LoadInfo:
    seen = existing_row_hashes(bronze_dir, source.name, dataset_name)
    if seen:
        resource.add_filter(lambda row: row["row_hash"] not in seen)
    pipeline = dlt.pipeline(
        pipeline_name=f"{dataset_name}_{source.name}",
        destination=filesystem(bucket_url=str(bronze_dir)),
        dataset_name=dataset_name,
        # Keep dlt's working state alongside the data, not in the user's home
        # (~/.dlt) — isolates state per warehouse and avoids cross-run
        # collisions on a globally-shared pipeline directory.
        pipelines_dir=str(bronze_dir / ".dlt"),
    )
    try:
        return pipeline.run(resource, table_name=source.name, loader_file_format="parquet")
    except Exception as exc:
        # dlt wraps resource errors in PipelineStepFailed/ResourceExtractionError;
        # recover our IngestionError from the chain so callers only see it.
        # Follow both explicit (__cause__) and implicit (__context__) chaining,
        # guarding against cycles.
        cause: BaseException | None = exc
        seen: set[int] = set()
        while cause is not None and id(cause) not in seen:
            if isinstance(cause, IngestionError):
                raise cause from None
            seen.add(id(cause))
            cause = cause.__cause__ or cause.__context__
        raise


def run_csv_ingestion(source: SourceConfig, file_path: Path, bronze_dir: Path) -> LoadInfo:
    """Ingest one CSV export file into the bronze layer."""
    return _run(source, csv_transactions(source, file_path), bronze_dir)


def dataset_name_for(source: SourceConfig) -> str:
    """Return the bronze dataset a source's rows land in — see ``_run``.

    Every ``SourceConfig.kind`` maps to exactly one dataset; used both here
    and by callers (``watch.ingest_file``) that need to read back a source's
    row count without duplicating the kind -> dataset mapping.
    """
    if source.kind == SourceKind.AMAZON:
        return "bronze_amazon"
    return "bronze"


def run_ofx_ingestion(source: SourceConfig, file_path: Path, bronze_dir: Path) -> LoadInfo:
    """Ingest one OFX/QFX export file into the bronze layer."""
    return _run(source, ofx_transactions(source, file_path), bronze_dir)


def run_amazon_ingestion(source: SourceConfig, file_path: Path, bronze_dir: Path) -> LoadInfo:
    """Ingest one Amazon order-history export into its own bronze dataset.

    Lands under ``bronze_amazon/``, not ``bronze/`` — see ``_run``'s docstring
    for why an order-history row can't share the generic transactions dataset.
    """
    return _run(
        source,
        amazon_order_items(source, file_path),
        bronze_dir,
        dataset_name=dataset_name_for(source),
    )


def run_ingestion(source: SourceConfig, file_path: Path, bronze_dir: Path) -> LoadInfo:
    """Ingest a file into bronze, dispatching on ``source.kind``.

    Raises:
        IngestionError: If the file cannot be parsed.
    """
    if source.kind == SourceKind.OFX:
        return run_ofx_ingestion(source, file_path, bronze_dir)
    if source.kind == SourceKind.AMAZON:
        return run_amazon_ingestion(source, file_path, bronze_dir)
    return run_csv_ingestion(source, file_path, bronze_dir)
