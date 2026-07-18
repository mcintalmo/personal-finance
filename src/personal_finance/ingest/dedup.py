"""Row-level idempotency for bronze ingestion.

dlt's filesystem destination is append-only and has no ``merge`` write
disposition, so we make re-ingestion idempotent ourselves: every bronze row
carries a deterministic ``row_hash``, and the pipeline drops any row whose hash
is already present in the source's bronze table before appending. Bronze thus
stays append-only (rows are never mutated or deleted) while re-dropping the
same file — or a later export whose date range overlaps an earlier one — adds
no duplicates.

The hash key prefers ``external_id`` (OFX FITID, Venmo ID) — a stable natural
key that makes dedup exact. When a source has no external id, the key falls
back to the row's content ``(source, posted_on, amount, description_raw)``;
this is best-effort, so a genuinely-repeated identical charge appearing in a
later file is treated as already-seen. Prefer sources that expose a stable id.
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
) -> str:
    """Return the deterministic idempotency key for one bronze row.

    Uses ``external_id`` when present (exact), else the row's content. The
    ``source_name`` is always part of the key so identical activity in two
    different accounts never collides.
    """
    if external_id:
        key = f"{source_name}|id|{external_id}"
    else:
        key = f"{source_name}|content|{posted_on.isoformat()}|{amount}|{description_raw}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def existing_row_hashes(bronze_dir: Path, table_name: str) -> set[str]:
    """Return the ``row_hash`` values already landed for a source.

    Empty on the first ingest, when no Parquet file exists yet for the table.
    """
    pattern = f"{bronze_dir}/bronze/{table_name}/*.parquet"
    with duckdb.connect() as conn:
        try:
            rows = conn.execute(
                f"select distinct row_hash from read_parquet('{pattern}')"
            ).fetchall()
        except duckdb.IOException:
            # No files match the glob yet — nothing has been ingested for
            # this source, so there is nothing to dedup against.
            return set()
    return {row[0] for row in rows}
