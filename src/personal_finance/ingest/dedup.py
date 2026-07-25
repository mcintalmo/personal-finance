"""Row-level idempotency for bronze ingestion.

dlt's filesystem destination is append-only and has no ``merge`` write
disposition, so we make re-ingestion idempotent ourselves: every bronze row
carries a deterministic ``row_hash``, and the pipeline drops any row whose hash
is already present in the source's bronze table before appending. Bronze thus
stays append-only (rows are never mutated or deleted) while re-dropping the
same file — or a later export whose date range overlaps an earlier one — adds
no duplicates.

The hash key prefers ``external_id`` (OFX FITID, Venmo ID) — a stable natural
key that makes dedup exact. Because an id is unique only within an account, an
optional ``account_id`` scopes the key so two statements bundled in one file
(e.g. checking + savings) can't collide on a shared id. When a source has no
external id, the key falls back to the row's content
``(source, posted_on, amount, description_raw)`` plus an ``occurrence`` index —
the Nth row with that exact content within one ingest batch. Without the
index, two genuinely different same-day transactions that happen to share an
amount and description (two identical vending-machine purchases) would hash
identically and collapse into one row downstream (silver's dedup keeps only
one per row_hash); the index keeps them distinct within a file while a
later file repeating the same content is still (best-effort) treated as
already-seen. Prefer sources that expose a stable id.
"""

import hashlib
from typing import TYPE_CHECKING

import duckdb

if TYPE_CHECKING:
    from datetime import date
    from decimal import Decimal
    from pathlib import Path


def compute_row_hash(
    source_name: str,
    posted_on: date,
    amount: Decimal,
    description_raw: str,
    external_id: str | None,
    account_id: str | None = None,
    occurrence: int = 0,
) -> str:
    """Return the deterministic idempotency key for one bronze row.

    Uses ``external_id`` when present (exact), else the row's content plus
    ``occurrence`` — the 0-based index of this row among others sharing the
    same content within the current ingest batch, so genuinely-repeated
    same-day/amount/description transactions in one file get distinct hashes
    instead of colliding. ``source_name`` and the optional ``account_id``
    always scope the key, so identical activity — or a reused id — in two
    different accounts never collides.
    """
    scope = f"{account_id}:" if account_id else ""
    if external_id:
        key = f"{source_name}|id|{scope}{external_id}"
    else:
        key = (
            f"{source_name}|content|{scope}{posted_on.isoformat()}|{amount}|"
            f"{description_raw}|{occurrence}"
        )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def compute_amazon_row_hash(
    source_name: str,
    website_order_id: str,
    asin: str,
    ship_date: date,
    occurrence: int = 0,
) -> str:
    """Return the deterministic idempotency key for one Amazon order-item row.

    Amazon order-history has no single-column stable id like OFX's FITID: the
    natural key is (order, item, shipment) — the same ASIN can legitimately
    repeat within one order across separate shipments, so ``ship_date`` scopes
    it, and ``occurrence`` (the Nth row so far sharing that exact key within
    one ingest batch) covers the rarer case of the same item shipping twice on
    the same day within one order (e.g. a split shipment of a multi-quantity
    line).
    """
    key = f"{source_name}|amazon|{website_order_id}|{asin}|{ship_date.isoformat()}|{occurrence}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _bronze_glob(bronze_dir: Path, table_name: str, dataset_name: str = "bronze") -> str:
    return f"{bronze_dir}/{dataset_name}/{table_name}/*.parquet"


def existing_row_hashes(
    bronze_dir: Path, table_name: str, dataset_name: str = "bronze"
) -> set[str]:
    """Return the ``row_hash`` values already landed for a source.

    Empty on the first ingest, when no Parquet file exists yet for the table.
    ``dataset_name`` defaults to the shared "bronze" dataset every
    transaction-shaped CSV/OFX source lands in; a source landed under a
    different dataset (e.g. Amazon order-history — see
    ``personal_finance.ingest.amazon_source``) must pass the same one it was
    ingested with.
    """
    with duckdb.connect() as conn:
        try:
            # Bind the glob as a parameter so a path containing quotes can't
            # break the query.
            rows = conn.execute(
                "select distinct row_hash from read_parquet(?)",
                [_bronze_glob(bronze_dir, table_name, dataset_name)],
            ).fetchall()
        except duckdb.IOException:
            # No files match the glob yet — nothing has been ingested for
            # this source, so there is nothing to dedup against.
            return set()
    return {row[0] for row in rows}


def bronze_row_count(bronze_dir: Path, table_name: str, dataset_name: str = "bronze") -> int:
    """Return how many rows have landed for a source; 0 before the first ingest."""
    with duckdb.connect() as conn:
        try:
            result = conn.execute(
                "select count(*) from read_parquet(?)",
                [_bronze_glob(bronze_dir, table_name, dataset_name)],
            ).fetchone()
        except duckdb.IOException:
            return 0
    return result[0] if result else 0
