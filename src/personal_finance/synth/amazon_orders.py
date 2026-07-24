"""Generate a synthetic Amazon order-history export correlated with scenario
card charges.

Each Amazon-category card charge (see ``scenario.AMAZON_CATEGORY_HINT``)
becomes one shipment: 1-3 catalog line items whose subtotal + tax sums to the
charge magnitude, decomposed the same way ``synth.receipts`` decomposes a
grocery charge into receipt items. ``Total Owed`` (and ``Shipping Charge``/
``Total Discounts``) is the shipment-level total, repeated on every item row
of that shipment — matching the real export's grain (one row per shipment
*item*, not per shipment or per order) documented in docs/source-schemas.md.

Ground truth (which card transaction a shipment belongs to) is NOT embedded in
the written CSV — a real export wouldn't carry it — it lives in
``AmazonOrderItem.transaction_external_id`` / the written manifest, for
order-matching evaluation, same pattern as ``synth.receipts``.
"""

import csv
import io
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from random import Random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from personal_finance.synth.scenario import Scenario

_CENT = Decimal("0.01")

# (asin, product name)
CATALOG: tuple[tuple[str, str], ...] = (
    ("B08N5WRWNW", "Echo Dot (4th Gen)"),
    ("B07FZ8S74R", "AmazonBasics AA Batteries, 48-Pack"),
    ("B01N5IB20Q", "Anker PowerLine USB-C Cable, 6ft"),
    ("B0002YFSCK", "Command Strips, 16-Pack"),
    ("B075H2QG5B", "Kindle Paperwhite Fabric Case"),
    ("B08F7PTF53", "Ninja Foodi Digital Air Fryer"),
    ("B09B8V1LZ3", "Bounty Paper Towels, 12 Rolls"),
    ("B0012J3WNK", "Tide Laundry Detergent, 100 oz"),
)


@dataclass
class AmazonOrderItem:
    """One shipment-item row, matching Retail.OrderHistory.1.csv's grain."""

    website_order_id: str
    order_date: date
    ship_date: date
    asin: str
    product_name: str
    quantity: int
    unit_price: Decimal
    unit_price_tax: Decimal
    shipping_charge: Decimal
    total_discounts: Decimal
    shipment_item_subtotal: Decimal
    shipment_item_subtotal_tax: Decimal
    total_owed: Decimal
    currency: str = "USD"
    order_status: str = "Closed"
    shipment_status: str = "Shipped"
    # Ground truth for evaluation — never written to the CSV.
    transaction_external_id: str = ""


def _order_id(rng: Random) -> str:
    """A realistic-looking ``111-1234567-1234567`` order id."""
    first = "".join(str(rng.randrange(10)) for _ in range(7))
    second = "".join(str(rng.randrange(10)) for _ in range(7))
    return f"111-{first}-{second}"


def _decompose_items(rng: Random, subtotal: Decimal) -> list[tuple[str, str, Decimal]]:
    """Split a shipment subtotal into 1-3 catalog (asin, name, unit_price) items summing to it."""
    items: list[tuple[str, str, Decimal]] = []
    remaining = subtotal
    for _ in range(rng.randrange(0, 2)):
        ceiling = min(remaining - Decimal("2.00"), Decimal("60.00"))
        if ceiling < Decimal("2.00"):
            break
        price = (Decimal(rng.randrange(199, int(ceiling * 100))) * _CENT).quantize(_CENT)
        asin, name = rng.choice(CATALOG)
        items.append((asin, name, price))
        remaining -= price
    asin, name = rng.choice(CATALOG)
    items.append((asin, name, remaining.quantize(_CENT)))
    return items


def generate_amazon_orders(scenario: Scenario, seed: int = 42) -> list[AmazonOrderItem]:
    """Generate one shipment (1-3 item rows) per Amazon-category card charge.

    Deterministic for a given (scenario, seed). Each shipment's items'
    subtotal + tax equals the magnitude of its source transaction amount,
    surfaced shipment-wide as ``total_owed`` on every item row.
    """
    rng = Random(seed)
    rows: list[AmazonOrderItem] = []
    amazon_charges = [
        t
        for t in scenario.credit.transactions
        if t.category_hint == "Shopping" and t.txn_type == "purchase"
    ]
    for txn in amazon_charges:
        total = -txn.amount
        tax = (total * Decimal(rng.randrange(0, 10)) / 100).quantize(_CENT)
        subtotal = total - tax
        items = _decompose_items(rng, subtotal)
        website_order_id = _order_id(rng)
        ship_date = txn.posted_on
        order_date = ship_date - timedelta(days=rng.randrange(1, 4))
        # Tax attributed proportionally per item; shipment-level total_owed is
        # what actually needs to reconcile with the charge, not per-item tax.
        for asin, name, price in items:
            item_tax = (price * tax / total).quantize(_CENT) if total else Decimal("0.00")
            rows.append(
                AmazonOrderItem(
                    website_order_id=website_order_id,
                    order_date=order_date,
                    ship_date=ship_date,
                    asin=asin,
                    product_name=name,
                    quantity=1,
                    unit_price=price,
                    unit_price_tax=item_tax,
                    shipping_charge=Decimal("0.00"),
                    total_discounts=Decimal("0.00"),
                    shipment_item_subtotal=price,
                    shipment_item_subtotal_tax=item_tax,
                    total_owed=total,
                    transaction_external_id=txn.external_id,
                )
            )
    return rows


_HEADER = (
    "Website Order ID",
    "Order Date",
    "Currency",
    "Unit Price",
    "Unit Price Tax",
    "Shipping Charge",
    "Total Discounts",
    "Total Owed",
    "Shipment Item Subtotal",
    "Shipment Item Subtotal Tax",
    "ASIN",
    "Quantity",
    "Order Status",
    "Shipment Status",
    "Ship Date",
    "Product Name",
)


def _iso_datetime(day: date) -> str:
    """Amazon's export uses ISO-8601 UTC datetimes; noon avoids timezone-shift edge cases."""
    return f"{day.isoformat()}T12:00:00Z"


def render(rows: list[AmazonOrderItem]) -> str:
    """Render rows as the real Retail.OrderHistory.1.csv layout (minus PII columns
    this project doesn't ingest — see personal_finance.ingest.amazon_source).

    Uses the csv module (unlike writers.py's hand-built bank formats): real
    Amazon product names routinely contain commas (our own catalog does too),
    so fields need proper quoting rather than a plain join.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_HEADER)
    for row in rows:
        writer.writerow(
            [
                row.website_order_id,
                _iso_datetime(row.order_date),
                row.currency,
                str(row.unit_price),
                str(row.unit_price_tax),
                str(row.shipping_charge),
                str(row.total_discounts),
                str(row.total_owed),
                str(row.shipment_item_subtotal),
                str(row.shipment_item_subtotal_tax),
                row.asin,
                str(row.quantity),
                row.order_status,
                row.shipment_status,
                _iso_datetime(row.ship_date),
                row.product_name,
            ]
        )
    return buffer.getvalue()


def write_amazon_orders(rows: list[AmazonOrderItem], out_dir: Path) -> list[Path]:
    """Write the order-history CSV plus a ground-truth manifest.

    ``manifest.json`` maps each shipment's Website Order ID to its source
    transaction's external ID — the answer key for order↔charge matching
    evaluation, kept out of the CSV itself like a real export would be.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "Retail.OrderHistory.1.csv"
    csv_path.write_text(render(rows), encoding="utf-8")
    manifest = {row.website_order_id: row.transaction_external_id for row in rows}
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return [csv_path, manifest_path]
