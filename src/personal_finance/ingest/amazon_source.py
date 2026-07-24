"""Parse an Amazon order-history export (Privacy Central's
``Retail.OrderHistory.1.csv``) into canonical bronze rows.

Fixed external schema (documented in docs/source-schemas.md), so — like
OFX — no ``column_map``/sign convention is needed: the real file's column
names are hardcoded here, not user-configurable. One row per *shipment item*,
not per order or per charge: a multi-item order that ships in two boxes
produces two card charges, and ``Total Owed`` (and ``Shipping Charge``/
``Total Discounts``) is the same shipment-level total repeated on every row
of that shipment — silver aggregation must take that value once per
(order, ship date), never sum it across a shipment's item rows (that would
multiply it by item count). See ``silver_amazon_shipments.sql``.

PII-heavy columns not needed for matching/categorization (shipping/billing
address, gift message/sender/recipient, carrier tracking number, purchase
order number) are dropped rather than landed into bronze.
"""

import csv
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

# Path/Iterator/date must be REAL imports: dlt's @dlt.resource introspects the
# decorated function's signature at import time. See csv_source.py.
from pathlib import Path

import dlt

from personal_finance.exceptions import IngestionError
from personal_finance.ingest.dedup import compute_amazon_row_hash
from personal_finance.user_config import SourceConfig

BronzeRow = dict[str, object]


def _strip_currency(raw: str) -> str:
    return raw.strip().replace("$", "").replace(",", "").strip()


def _parse_amount(raw: str) -> Decimal:
    stripped = _strip_currency(raw)
    return Decimal(stripped) if stripped else Decimal("0")


def _parse_date(raw: str) -> date:
    # Amazon's Privacy Central export uses ISO-8601 UTC datetimes
    # ("2026-01-15T10:23:41Z") for Order Date/Ship Date — unverified against a
    # real export (no sample in this repo yet); confirm before relying on this
    # for real data, per docs/source-schemas.md's "verify against real
    # exports first" guidance.
    return datetime.strptime(raw.strip(), "%Y-%m-%dT%H:%M:%SZ").date()


def _parse_row(
    source_name: str, row: dict[str, str], occurrence_counts: dict[tuple[object, ...], int]
) -> dict[str, object]:
    website_order_id = row["Website Order ID"].strip()
    asin = row["ASIN"].strip()
    ship_date = _parse_date(row["Ship Date"])
    key = (website_order_id, asin, ship_date)
    occurrence = occurrence_counts.get(key, 0)
    occurrence_counts[key] = occurrence + 1
    return {
        "website_order_id": website_order_id,
        "order_date": _parse_date(row["Order Date"]),
        "ship_date": ship_date,
        "asin": asin,
        "product_name": row["Product Name"].strip(),
        "quantity": int(row["Quantity"].strip()),
        "unit_price": _parse_amount(row["Unit Price"]),
        "unit_price_tax": _parse_amount(row["Unit Price Tax"]),
        "shipping_charge": _parse_amount(row["Shipping Charge"]),
        "total_discounts": _parse_amount(row["Total Discounts"]),
        "shipment_item_subtotal": _parse_amount(row["Shipment Item Subtotal"]),
        "shipment_item_subtotal_tax": _parse_amount(row["Shipment Item Subtotal Tax"]),
        "total_owed": _parse_amount(row["Total Owed"]),
        "currency": row["Currency"].strip() or "USD",
        "order_status": row["Order Status"].strip(),
        "shipment_status": row["Shipment Status"].strip(),
        "row_hash": compute_amazon_row_hash(
            source_name, website_order_id, asin, ship_date, occurrence=occurrence
        ),
    }


def read_rows(file_path: Path) -> Iterator[dict[str, str]]:
    """Yield raw string rows from an Amazon order-history CSV export."""
    with file_path.open(newline="", encoding="utf-8") as handle:
        yield from csv.DictReader(handle)


@dlt.resource(name="amazon_order_items", write_disposition="append")
def amazon_order_items(source: SourceConfig, file_path: Path) -> Iterator[BronzeRow]:
    """dlt resource yielding canonical bronze rows for one Amazon order-history export.

    Fail-fast by design, same contract as ``csv_transactions``: the first
    unparseable row aborts the whole file so nothing partial lands in bronze.

    Raises:
        IngestionError: If any row cannot be parsed.
    """
    ingested_at = datetime.now(UTC)
    occurrence_counts: dict[tuple[object, ...], int] = {}
    for raw_row in read_rows(file_path):
        try:
            parsed = _parse_row(source.name, raw_row, occurrence_counts)
        except (KeyError, ValueError, InvalidOperation, AttributeError, TypeError) as exc:
            msg = f"{file_path}: failed to parse row {raw_row!r}: {exc}"
            raise IngestionError(msg) from exc
        yield {
            "source": source.name,
            "source_file": str(file_path),
            "ingested_at": ingested_at,
            **parsed,
        }
