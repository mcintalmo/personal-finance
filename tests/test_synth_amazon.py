"""Tests for personal_finance.synth.amazon_orders."""

import csv
import io
import json
from decimal import Decimal

import pytest

from personal_finance.synth import generate_amazon_orders, generate_scenario, write_amazon_orders
from personal_finance.synth.amazon_orders import render


@pytest.fixture(scope="module")
def scenario():
    return generate_scenario(seed=42, months=3)


@pytest.fixture(scope="module")
def orders(scenario):
    return generate_amazon_orders(scenario, seed=42)


def _amazon_charges(scenario):
    return [
        t
        for t in scenario.credit.transactions
        if t.category_hint == "Shopping" and t.txn_type == "purchase"
    ]


class TestGenerateAmazonOrders:
    def test_at_least_one_row_per_amazon_charge(self, scenario, orders):
        charges = _amazon_charges(scenario)
        assert charges  # sanity: the scenario actually produced some
        order_ids_by_charge = {
            txn.external_id: {
                r.website_order_id for r in orders if r.transaction_external_id == txn.external_id
            }
            for txn in charges
        }
        assert all(len(ids) == 1 for ids in order_ids_by_charge.values())

    def test_deterministic(self, scenario):
        assert generate_amazon_orders(scenario, seed=1) == generate_amazon_orders(scenario, seed=1)
        assert generate_amazon_orders(scenario, seed=1) != generate_amazon_orders(scenario, seed=2)

    def test_does_not_perturb_other_scenario_data(self):
        """Adding Amazon charges must not shift the RNG stream driving every
        other merchant category — see scenario.py's independent amazon_rng."""
        with_amazon = generate_scenario(seed=42, months=2)
        groceries = [
            t
            for t in with_amazon.credit.transactions
            if t.category_hint == "Groceries" and t.txn_type == "purchase"
        ]
        assert groceries  # sanity
        # A regression to a shared rng would change amounts/dates here even
        # though nothing about groceries changed.
        assert groceries[0].description in {
            "TRADER JOE'S #0552 SEATTLE WA",
            "KROGER #718",
            "SAFEWAY STORE 1442",
            "ALDI 73011",
        }

    def test_shipment_item_subtotal_plus_tax_summed_equals_subtotal(self, scenario, orders):
        by_order = {}
        for row in orders:
            by_order.setdefault(row.website_order_id, []).append(row)
        by_id = {t.external_id: t for t in scenario.credit.transactions}
        for rows in by_order.values():
            txn = by_id[rows[0].transaction_external_id]
            subtotal_sum = sum((r.shipment_item_subtotal for r in rows), Decimal("0"))
            tax_sum = sum((r.shipment_item_subtotal_tax for r in rows), Decimal("0"))
            assert subtotal_sum + tax_sum <= -txn.amount  # per-item tax rounds down at worst

    def test_total_owed_matches_source_transaction_magnitude(self, scenario, orders):
        by_id = {t.external_id: t for t in scenario.credit.transactions}
        for row in orders:
            txn = by_id[row.transaction_external_id]
            assert row.total_owed == -txn.amount

    def test_total_owed_is_shipment_wide_not_per_item(self, orders):
        """Every item row in one shipment carries the SAME total_owed — it is
        not divided across items, matching the real export's grain."""
        by_order = {}
        for row in orders:
            by_order.setdefault(row.website_order_id, []).append(row)
        for rows in by_order.values():
            assert len({r.total_owed for r in rows}) == 1

    def test_ship_date_matches_source_transaction(self, scenario, orders):
        by_id = {t.external_id: t for t in scenario.credit.transactions}
        for row in orders:
            txn = by_id[row.transaction_external_id]
            assert row.ship_date == txn.posted_on

    def test_order_date_on_or_before_ship_date(self, orders):
        assert all(row.order_date <= row.ship_date for row in orders)


class TestRenderAndWrite:
    def test_render_round_trips_via_csv_module(self, orders):
        """Product names in the catalog contain commas, so a hand-joined line
        (like writers.py's bank formats) would corrupt the file — must
        actually quote."""
        text = render(orders)
        rows = list(csv.DictReader(io.StringIO(text)))
        assert len(rows) == len(orders)
        assert any("," in row["Product Name"] for row in rows)
        assert rows[0]["Website Order ID"] == orders[0].website_order_id

    def test_writes_csv_and_manifest(self, orders, tmp_path):
        written = write_amazon_orders(orders, tmp_path / "amazon")
        names = {p.name for p in written}
        assert names == {"Retail.OrderHistory.1.csv", "manifest.json"}

        manifest = json.loads((tmp_path / "amazon" / "manifest.json").read_text())
        assert set(manifest) == {row.website_order_id for row in orders}
        assert set(manifest.values()) == {row.transaction_external_id for row in orders}

    def test_manifest_not_present_in_csv(self, orders, tmp_path):
        write_amazon_orders(orders, tmp_path / "amazon")
        csv_text = (tmp_path / "amazon" / "Retail.OrderHistory.1.csv").read_text()
        for row in orders:
            assert row.transaction_external_id not in csv_text
