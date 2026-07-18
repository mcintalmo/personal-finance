"""Run one CSV source's dlt resource into the Parquet-backed bronze layer."""

from typing import TYPE_CHECKING

import dlt
from dlt.destinations import filesystem

from personal_finance.exceptions import IngestionError
from personal_finance.ingest.csv_source import csv_transactions

if TYPE_CHECKING:
    from pathlib import Path

    from dlt.common.pipeline import LoadInfo

    from personal_finance.user_config import SourceConfig


def run_csv_ingestion(source: SourceConfig, file_path: Path, bronze_dir: Path) -> LoadInfo:
    """Ingest one CSV export file into the bronze layer.

    Bronze is append-only Parquet under ``bronze_dir/bronze/<source.name>/``.
    Re-ingesting the same file appends another copy of its rows — dedup
    across ingestion runs is a separate, later task (see TODO.md); dlt's
    filesystem destination does not support merge write disposition.

    Raises:
        IngestionError: If a row fails to parse. dlt wraps our own
            IngestionError in its own PipelineStepFailed/ResourceExtractionError
            — this unwraps that chain so callers only ever see our exception
            type, per the project's exception-boundary convention.
    """
    pipeline = dlt.pipeline(
        pipeline_name=f"bronze_{source.name}",
        destination=filesystem(bucket_url=str(bronze_dir)),
        dataset_name="bronze",
    )
    try:
        return pipeline.run(
            csv_transactions(source, file_path),
            table_name=source.name,
            loader_file_format="parquet",
        )
    except Exception as exc:
        cause = exc
        while cause is not None:
            if isinstance(cause, IngestionError):
                raise cause from None
            cause = cause.__cause__
        raise
