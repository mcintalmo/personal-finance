"""Bronze-layer ingestion: land raw source exports as immutable Parquet.

Supports CSV and OFX/QFX sources configured via ``sources.yaml`` (see
``personal_finance.user_config.SourceConfig``). CSV handles column mapping,
headerless files, preamble skipping, and signed/inverted/debit-credit sign
conventions per the capability matrix in docs/source-schemas.md; OFX is
parsed structurally by ofxtools. A new bank should be a config entry — see
that doc before adding source-specific code.

Ingestion is idempotent across runs: every bronze row carries a deterministic
``row_hash`` and re-dropping a file (or an overlapping export) appends no
duplicates — see ``personal_finance.ingest.dedup``.

``run_ingestion`` dispatches on ``source.kind``; use it unless you
specifically need the format-typed entry point. ``ingest_file`` wraps it with
source resolution and row-count reporting, and ``watch_folder`` (in
``personal_finance.ingest.watch``) ingests a folder's files as they are
dropped in.
"""

from personal_finance.ingest.csv_source import csv_transactions, read_rows
from personal_finance.ingest.dedup import (
    bronze_row_count,
    compute_row_hash,
    existing_row_hashes,
)
from personal_finance.ingest.ofx_source import ofx_transactions, read_ofx_transactions
from personal_finance.ingest.pipeline import (
    run_csv_ingestion,
    run_ingestion,
    run_ofx_ingestion,
)
from personal_finance.ingest.watch import (
    IngestOutcome,
    IngestStatus,
    ingest_file,
    sweep_folder,
    watch_folder,
)

__all__ = [
    "IngestOutcome",
    "IngestStatus",
    "bronze_row_count",
    "compute_row_hash",
    "csv_transactions",
    "existing_row_hashes",
    "ingest_file",
    "ofx_transactions",
    "read_ofx_transactions",
    "read_rows",
    "run_csv_ingestion",
    "run_ingestion",
    "run_ofx_ingestion",
    "sweep_folder",
    "watch_folder",
]
