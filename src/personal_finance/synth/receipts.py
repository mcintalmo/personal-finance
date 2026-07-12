"""Generate synthetic receipts correlated with scenario card charges.

Each receipt decomposes one grocery card transaction into line items that sum
exactly to the charge (subtotal + tax == charge amount), mimicking a warehouse
receipt per the Costco anatomy in docs/source-schemas.md: item numbers,
abbreviated names (the reason line items need NLP disambiguation), tax flags,
and totals block.

Two artifacts per receipt:
    - a JSON payload shaped like the Phase 5 vision-LLM output (no ground
      truth inside it), and
    - a rendered plain-text receipt.

Ground truth (which transaction a receipt belongs to) lives OUTSIDE the
payload, in the Receipt dataclass / the written manifest — so matching and
split-decomposition can be evaluated against known answers without leaking
them to the models being tested.
"""

import json
from dataclasses import dataclass
from decimal import Decimal
from random import Random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date
    from pathlib import Path

    from personal_finance.synth.scenario import Scenario

_CENT = Decimal("0.01")

# (item number, abbreviated name, full name, tax flag)
# Abbreviations are deliberately cryptic — that's the real-world NLP problem.
CATALOG: tuple[tuple[str, str, str, str], ...] = (
    ("38847", "HNYCRISP APPLE 4LB", "Honeycrisp apples, 4 lb bag", "E"),
    ("96716", "ORG EGGS 24CT", "Organic eggs, 24 count", "E"),
    ("11205", "KS DICED TOM", "Kirkland Signature diced tomatoes", "E"),
    ("40318", "ORG BANANAS 3LB", "Organic bananas, 3 lb", "E"),
    ("55521", "SRDGH BREAD", "Sourdough bread loaf", "E"),
    ("77813", "WHL MILK GAL", "Whole milk, 1 gallon", "E"),
    ("21930", "CHKN BRST 6LB", "Chicken breast, 6 lb pack", "E"),
    ("62204", "OLIVE OIL 2L", "Extra virgin olive oil, 2 L", "E"),
    ("83401", "PPR TWL 12PK", "Paper towels, 12 pack", "A"),
    ("94112", "DISH SOAP 90OZ", "Dish soap, 90 oz", "A"),
    ("15678", "APPLE PIE 10IN", "Bakery apple pie, 10 inch", "E"),
    ("70233", "CLD BREW 12PK", "Cold brew coffee, 12 pack", "A"),
)


@dataclass
class ReceiptItem:
    """One receipt line item."""

    item_number: str
    name_abbrev: str
    full_name: str
    price: Decimal
    tax_flag: str  # A = taxable, E = exempt


@dataclass
class Receipt:
    """A synthetic receipt plus its ground-truth transaction link."""

    merchant: str
    purchased_on: date
    time: str
    items: list[ReceiptItem]
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    payment_last4: str
    # Ground truth for evaluation — never included in to_payload().
    transaction_external_id: str

    def to_payload(self) -> dict[str, object]:
        """The shape a Phase 5 vision-LLM parse is expected to produce."""
        return {
            "merchant": self.merchant,
            "date": self.purchased_on.isoformat(),
            "time": self.time,
            "items": [
                {
                    "item_number": item.item_number,
                    "name": item.name_abbrev,
                    "price": str(item.price),
                    "tax_flag": item.tax_flag,
                }
                for item in self.items
            ],
            "subtotal": str(self.subtotal),
            "tax": str(self.tax),
            "total": str(self.total),
            "payment": {"method": "VISA", "last4": self.payment_last4},
        }


def _pseudo_time(external_id: str) -> str:
    seconds = (int(external_id[3:]) * 5077) % 43200 + 28800  # 08:00-20:00
    hour, rem = divmod(seconds, 3600)
    return f"{hour:02d}:{rem // 60:02d}"


def _decompose(rng: Random, subtotal: Decimal) -> list[ReceiptItem]:
    """Split a subtotal into catalog line items that sum to it exactly."""
    items: list[ReceiptItem] = []
    remaining = subtotal
    while remaining > Decimal("15.00") and len(items) < 11:
        number, abbrev, full, flag = rng.choice(CATALOG)
        ceiling = min(remaining - Decimal("2.00"), Decimal("20.00"))
        price = (Decimal(rng.randrange(199, int(ceiling * 100))) * _CENT).quantize(_CENT)
        items.append(ReceiptItem(number, abbrev, full, price, flag))
        remaining -= price
    number, abbrev, full, flag = rng.choice(CATALOG)
    items.append(ReceiptItem(number, abbrev, full, remaining.quantize(_CENT), flag))
    return items


def generate_receipts(scenario: Scenario, seed: int = 42) -> list[Receipt]:
    """Generate one receipt per grocery charge in the scenario.

    Deterministic for a given (scenario, seed). Each receipt's
    subtotal + tax equals the magnitude of its source transaction amount.
    """
    rng = Random(seed)
    receipts: list[Receipt] = []
    groceries = [
        t
        for t in scenario.credit.transactions
        if t.category_hint == "Groceries" and t.txn_type == "purchase"
    ]
    for txn in groceries:
        total = -txn.amount
        tax = (total * Decimal(rng.randrange(0, 7)) / 100).quantize(_CENT)
        subtotal = total - tax
        receipts.append(
            Receipt(
                merchant=txn.description,
                purchased_on=txn.posted_on,
                time=_pseudo_time(txn.external_id),
                items=_decompose(rng, subtotal),
                subtotal=subtotal,
                tax=tax,
                total=total,
                payment_last4="1234",
                transaction_external_id=txn.external_id,
            )
        )
    return receipts


def render_receipt_text(receipt: Receipt) -> str:
    """Render a warehouse-style plain-text receipt."""
    lines = [
        receipt.merchant,
        f"{receipt.purchased_on.isoformat()} {receipt.time}",
        "MEMBER 111222333444",
        "",
    ]
    lines += [
        f"{item.item_number:>7} {item.name_abbrev:<22} {item.price:>8} {item.tax_flag}"
        for item in receipt.items
    ]
    lines += [
        "",
        f"{'SUBTOTAL':>30} {receipt.subtotal:>8}",
        f"{'TAX':>30} {receipt.tax:>8}",
        f"{'**** TOTAL':>30} {receipt.total:>8}",
        f"VISA ending in {receipt.payment_last4}",
        f"ITEMS SOLD {len(receipt.items)}",
    ]
    return "\n".join(lines) + "\n"


def _receipt_stem(index: int) -> str:
    return f"receipt_{index:03d}"


def write_receipts(receipts: list[Receipt], out_dir: Path) -> list[Path]:
    """Write per-receipt JSON payload + text rendering, plus a ground-truth manifest.

    ``manifest.json`` maps each receipt file stem to its source transaction's
    external ID — the answer key for receipt↔charge matching evaluation.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    manifest: dict[str, str] = {}
    for index, receipt in enumerate(receipts, start=1):
        stem = _receipt_stem(index)
        json_path = out_dir / f"{stem}.json"
        text_path = out_dir / f"{stem}.txt"
        json_path.write_text(
            json.dumps(receipt.to_payload(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        text_path.write_text(render_receipt_text(receipt), encoding="utf-8")
        manifest[stem] = receipt.transaction_external_id
        written += [json_path, text_path]
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    written.append(manifest_path)
    return written
