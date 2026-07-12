"""Tests for personal_finance.synth.receipts."""

import json
from decimal import Decimal

import pytest

from personal_finance.synth import (
    generate_receipts,
    generate_scenario,
    render_receipt_text,
    write_receipts,
)


@pytest.fixture(scope="module")
def scenario():
    return generate_scenario(seed=42, months=3)


@pytest.fixture(scope="module")
def receipts(scenario):
    return generate_receipts(scenario, seed=42)


class TestGenerateReceipts:
    def test_one_receipt_per_grocery_charge(self, scenario, receipts):
        groceries = [
            t
            for t in scenario.credit.transactions
            if t.category_hint == "Groceries" and t.txn_type == "purchase"
        ]
        assert len(receipts) == len(groceries) > 0

    def test_deterministic(self, scenario):
        assert generate_receipts(scenario, seed=1) == generate_receipts(scenario, seed=1)
        assert generate_receipts(scenario, seed=1) != generate_receipts(scenario, seed=2)

    def test_items_sum_exactly_to_subtotal(self, receipts):
        for receipt in receipts:
            assert sum((i.price for i in receipt.items), Decimal("0")) == receipt.subtotal

    def test_subtotal_plus_tax_equals_total(self, receipts):
        for receipt in receipts:
            assert receipt.subtotal + receipt.tax == receipt.total

    def test_total_matches_source_transaction(self, scenario, receipts):
        by_id = {t.external_id: t for t in scenario.credit.transactions}
        for receipt in receipts:
            assert receipt.total == -by_id[receipt.transaction_external_id].amount

    def test_merchant_and_date_match_source(self, scenario, receipts):
        by_id = {t.external_id: t for t in scenario.credit.transactions}
        for receipt in receipts:
            txn = by_id[receipt.transaction_external_id]
            assert receipt.merchant == txn.description
            assert receipt.purchased_on == txn.posted_on

    def test_item_prices_positive(self, receipts):
        assert all(i.price > 0 for r in receipts for i in r.items)


class TestPayloadAndRendering:
    def test_payload_has_no_ground_truth(self, receipts):
        payload = receipts[0].to_payload()
        assert "transaction_external_id" not in json.dumps(payload)

    def test_payload_json_serializable_with_string_amounts(self, receipts):
        payload = json.loads(json.dumps(receipts[0].to_payload()))
        assert Decimal(payload["total"]) == receipts[0].total
        assert all(Decimal(item["price"]) > 0 for item in payload["items"])

    def test_text_rendering_contains_items_and_totals(self, receipts):
        receipt = receipts[0]
        text = render_receipt_text(receipt)
        assert receipt.merchant in text
        assert "**** TOTAL" in text
        assert f"ITEMS SOLD {len(receipt.items)}" in text
        for item in receipt.items:
            assert item.name_abbrev in text


class TestWriteReceipts:
    def test_writes_json_text_and_manifest(self, receipts, tmp_path):
        written = write_receipts(receipts, tmp_path / "receipts")
        names = {p.name for p in written}
        assert "manifest.json" in names
        assert len(written) == 2 * len(receipts) + 1

        manifest = json.loads((tmp_path / "receipts" / "manifest.json").read_text())
        assert len(manifest) == len(receipts)
        assert set(manifest.values()) == {r.transaction_external_id for r in receipts}

    def test_manifest_ids_resolve_to_scenario_transactions(self, scenario, receipts, tmp_path):
        write_receipts(receipts, tmp_path / "r")
        manifest = json.loads((tmp_path / "r" / "manifest.json").read_text())
        credit_ids = {t.external_id for t in scenario.credit.transactions}
        assert set(manifest.values()) <= credit_ids
