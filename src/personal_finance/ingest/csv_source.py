"""Parse one CSV export file into canonical bronze rows, per its SourceConfig.

A row's shape is driven entirely by config (docs/source-schemas.md's
capability matrix) — no per-bank code. Amount parsing handles all three
observed sign conventions and defensively strips currency symbols/commas, so
formats like Ally's "$42.50" or Venmo's "+ $32.00" need no special case.
"""

import csv
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

# Path/Iterator/date must be REAL (not TYPE_CHECKING-only) imports: dlt's
# @dlt.resource decorator eagerly introspects the decorated function's
# signature via inspect.signature(), which evaluates its annotations at
# import time — Python 3.14's lazy-annotation default doesn't save us here.
from pathlib import Path

import dlt

from personal_finance.exceptions import IngestionError
from personal_finance.ingest.dedup import compute_row_hash
from personal_finance.user_config import SignConvention, SourceConfig

BronzeRow = dict[str, object]


def _parse_date(raw: str, date_format: str | None) -> date:
    fmt = date_format or "%Y-%m-%d"
    return datetime.strptime(raw.strip(), fmt).date()


def _strip_currency(raw: str) -> str:
    return raw.strip().replace("$", "").replace(",", "").strip()


def _parse_amount_signed(raw: str) -> Decimal:
    """Parse a single signed amount column.

    Handles a leading sign detached from the currency symbol by whitespace
    (Venmo's ``"+ $32.00"``) as well as the plain ``"-42.50"`` case.
    """
    raw = raw.strip()
    sign = 1
    if raw.startswith("+"):
        raw = raw[1:]
    elif raw.startswith("-"):
        sign = -1
        raw = raw[1:]
    return Decimal(_strip_currency(raw)) * sign


def _parse_amount_unsigned(raw: str) -> Decimal:
    """Parse one side of a debit/credit column pair; blank means zero."""
    stripped = _strip_currency(raw)
    return abs(Decimal(stripped)) if stripped else Decimal("0")


def _parse_row(source: SourceConfig, row: dict[str, str]) -> dict[str, object]:
    posted_on = _parse_date(row[source.column_map["posted_on"]], source.date_format)
    description_raw = row[source.column_map["description_raw"]].strip()
    if source.sign_convention == SignConvention.DEBIT_CREDIT:
        debit = _parse_amount_unsigned(row[source.column_map["debit"]])
        credit = _parse_amount_unsigned(row[source.column_map["credit"]])
        amount = credit - debit
    else:
        amount = _parse_amount_signed(row[source.column_map["amount"]])
        if source.sign_convention == SignConvention.INVERTED:
            amount = -amount
    external_id: str | None = None
    if "external_id" in source.column_map:
        external_id = row[source.column_map["external_id"]].strip() or None
    # external_id is always emitted (None when the source has none) so bronze
    # has a stable schema across sources; the resource's column hint keeps the
    # column even when every value is null. See csv_transactions.
    return {
        "posted_on": posted_on,
        "amount": amount,
        "description_raw": description_raw,
        "external_id": external_id,
        "row_hash": compute_row_hash(source.name, posted_on, amount, description_raw, external_id),
    }


def read_rows(source: SourceConfig, file_path: Path) -> Iterator[dict[str, str]]:
    """Yield raw string rows from a CSV file per the source's layout config."""
    with file_path.open(newline="", encoding="utf-8") as handle:
        for _ in range(source.skip_rows):
            next(handle, None)
        fieldnames = None if source.has_header else source.columns
        yield from csv.DictReader(handle, fieldnames=fieldnames)


@dlt.resource(
    name="transactions",
    write_disposition="append",
    # Pin external_id to text so the column is always present in bronze even
    # when a source has no ids (all-null) — dlt otherwise drops an untyped
    # all-null column, leaving silver's union without the column. See
    # docs/source-schemas.md and ingest/dedup.py.
    columns={"external_id": {"data_type": "text", "nullable": True}},
)
def csv_transactions(source: SourceConfig, file_path: Path) -> Iterator[BronzeRow]:
    """dlt resource yielding canonical bronze rows for one CSV export file.

    Fail-fast by design: the first unparseable row (bad date, unparsable
    amount, missing/None configured column, footer/summary line) raises and
    aborts the whole file, so nothing lands in bronze. Once the pipeline is
    proven, unparseable rows should instead be routed to a quarantine table
    and the rest of the file allowed through (see TODO.md).

    Raises:
        IngestionError: If any row cannot be parsed.
    """
    ingested_at = datetime.now(UTC)
    for raw_row in read_rows(source, file_path):
        try:
            parsed = _parse_row(source, raw_row)
        # AttributeError/TypeError catch None cell values: csv.DictReader fills
        # missing fields in a short/ragged row with None, and None.strip() /
        # Decimal(None) raise those rather than ValueError.
        except (KeyError, ValueError, InvalidOperation, AttributeError, TypeError) as exc:
            msg = f"{file_path}: failed to parse row {raw_row!r}: {exc}"
            raise IngestionError(msg) from exc
        yield {
            "source": source.name,
            "account_name": source.account_name,
            "account_type": source.account_type.value,
            "currency": source.currency,
            "source_file": str(file_path),
            "ingested_at": ingested_at,
            **parsed,
        }
