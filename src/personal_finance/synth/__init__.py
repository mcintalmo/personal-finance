"""Deterministic dummy-data generation for development, tests, and demos.

Generates a coherent synthetic "financial life" (`scenario`) and renders it as
realistic bank/card/payment-app export files (`writers`) matching the verified
layouts in docs/source-schemas.md — including each format's quirks (headerless
Wells Fargo, Bank of America preamble, BMO integer dates, Venmo amount strings).
Also generates correlated receipt fixtures (`receipts`, Phase 9) and an Amazon
order-history export (`amazon_orders`, Phase 5).

No real financial data is ever used or embedded. Generation is seeded: the same
seed produces byte-identical exports.
"""

from personal_finance.synth.amazon_orders import (
    AmazonOrderItem,
    generate_amazon_orders,
    write_amazon_orders,
)
from personal_finance.synth.receipts import (
    Receipt,
    ReceiptItem,
    generate_receipts,
    render_receipt_text,
    write_receipts,
)
from personal_finance.synth.scenario import (
    Scenario,
    SynthAccount,
    SynthTransaction,
    generate_scenario,
)
from personal_finance.synth.writers import FORMATS, render, write_scenario

__all__ = [
    "FORMATS",
    "AmazonOrderItem",
    "Receipt",
    "ReceiptItem",
    "Scenario",
    "SynthAccount",
    "SynthTransaction",
    "generate_amazon_orders",
    "generate_receipts",
    "generate_scenario",
    "render",
    "render_receipt_text",
    "write_amazon_orders",
    "write_receipts",
    "write_scenario",
]
