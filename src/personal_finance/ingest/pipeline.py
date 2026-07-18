"""Run a source's dlt resource into the Parquet-backed bronze layer."""

from typing import TYPE_CHECKING

import dlt
from dlt.destinations import filesystem

from personal_finance.exceptions import IngestionError
from personal_finance.ingest.csv_source import csv_transactions
from personal_finance.ingest.ofx_source import ofx_transactions
from personal_finance.user_config import SourceKind

if TYPE_CHECKING:
    from pathlib import Path

    from dlt.common.pipeline import LoadInfo
    from dlt.extract import DltResource

    from personal_finance.user_config import SourceConfig


def _run(source: SourceConfig, resource: DltResource, bronze_dir: Path) -> LoadInfo:
    """Load one resource into ``bronze_dir/bronze/<source.name>/`` as Parquet.

    Bronze is append-only; re-ingesting a file appends another copy of its rows
    (dedup is a later task — dlt's filesystem destination has no merge). Any
    IngestionError raised inside the resource is wrapped by dlt in
    PipelineStepFailed/ResourceExtractionError; this unwraps that chain so
    callers only ever see our exception type, per the exception-boundary
    convention.
    """
    pipeline = dlt.pipeline(
        pipeline_name=f"bronze_{source.name}",
        destination=filesystem(bucket_url=str(bronze_dir)),
        dataset_name="bronze",
    )
    try:
        return pipeline.run(resource, table_name=source.name, loader_file_format="parquet")
    except Exception as exc:
        cause: BaseException | None = exc
        while cause is not None:
            if isinstance(cause, IngestionError):
                raise cause from None
            cause = cause.__cause__
        raise


def run_csv_ingestion(source: SourceConfig, file_path: Path, bronze_dir: Path) -> LoadInfo:
    """Ingest one CSV export file into the bronze layer."""
    return _run(source, csv_transactions(source, file_path), bronze_dir)


def run_ofx_ingestion(source: SourceConfig, file_path: Path, bronze_dir: Path) -> LoadInfo:
    """Ingest one OFX/QFX export file into the bronze layer."""
    return _run(source, ofx_transactions(source, file_path), bronze_dir)


def run_ingestion(source: SourceConfig, file_path: Path, bronze_dir: Path) -> LoadInfo:
    """Ingest a file into bronze, dispatching on ``source.kind``.

    Raises:
        IngestionError: If the file cannot be parsed.
    """
    if source.kind == SourceKind.OFX:
        return run_ofx_ingestion(source, file_path, bronze_dir)
    return run_csv_ingestion(source, file_path, bronze_dir)
