"""Bronze-layer ingestion: land raw source exports as immutable Parquet.

Currently supports CSV sources configured via ``sources.yaml`` (see
``personal_finance.user_config.SourceConfig``): column mapping, headerless
files, preamble skipping, and signed/inverted/debit-credit sign conventions,
per the capability matrix in docs/source-schemas.md. A new bank should be a
config entry — see that doc before adding source-specific code. OFX support
and cross-run idempotency are separate, later tasks (see TODO.md).
"""

from personal_finance.ingest.csv_source import csv_transactions, read_rows
from personal_finance.ingest.pipeline import run_csv_ingestion

__all__ = ["csv_transactions", "read_rows", "run_csv_ingestion"]
