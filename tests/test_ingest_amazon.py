"""Tests for personal_finance.ingest.amazon_source and Amazon pipeline dispatch."""

from pathlib import Path

import duckdb
import pytest

from personal_finance.exceptions import IngestionError
from personal_finance.ingest import read_amazon_rows, run_amazon_ingestion, run_ingestion
from personal_finance.synth import generate_amazon_orders, generate_scenario, write_amazon_orders
from personal_finance.user_config import SourceConfig, load_user_config

EXAMPLES_CONFIG_DIR = Path(__file__).parent.parent / "config" / "examples"


def amazon_source() -> SourceConfig:
    config = load_user_config(EXAMPLES_CONFIG_DIR)
    return next(s for s in config.sources if s.kind.value == "amazon")


@pytest.fixture(scope="module")
def scenario():
    return generate_scenario(seed=42, months=2)


@pytest.fixture(scope="module")
def orders(scenario):
    return generate_amazon_orders(scenario, seed=42)


@pytest.fixture(scope="module")
def amazon_dir(orders, tmp_path_factory):
    out = tmp_path_factory.mktemp("amazon")
    write_amazon_orders(orders, out)
    return out


def bronze_rows(bronze_dir: Path, table_name: str) -> list[tuple]:
    with duckdb.connect() as conn:
        return conn.execute(
            f"select * from read_parquet('{bronze_dir}/bronze_amazon/{table_name}/*.parquet') "
            "order by website_order_id, asin"
        ).fetchall()


def bronze_columns(bronze_dir: Path, table_name: str) -> list[str]:
    with duckdb.connect() as conn:
        return [
            row[0]
            for row in conn.execute(
                "describe select * from "
                f"read_parquet('{bronze_dir}/bronze_amazon/{table_name}/*.parquet')"
            ).fetchall()
        ]


class TestReadAmazonRows:
    def test_parses_every_row(self, orders, amazon_dir):
        rows = list(read_amazon_rows(amazon_dir / "Retail.OrderHistory.1.csv"))
        assert len(rows) == len(orders)

    def test_product_name_with_commas_survives_quoting(self, amazon_dir):
        rows = list(read_amazon_rows(amazon_dir / "Retail.OrderHistory.1.csv"))
        assert any("," in row["Product Name"] for row in rows)


class TestAmazonOrderItems:
    def test_lands_expected_column_shape(self, amazon_dir, tmp_path):
        bronze = tmp_path / "bronze"
        run_amazon_ingestion(amazon_source(), amazon_dir / "Retail.OrderHistory.1.csv", bronze)
        columns = set(bronze_columns(bronze, "amazon"))
        assert {
            "website_order_id",
            "order_date",
            "ship_date",
            "asin",
            "product_name",
            "quantity",
            "unit_price",
            "total_owed",
            "row_hash",
            "source",
            "source_file",
            "ingested_at",
        } <= columns

    def test_total_owed_repeated_per_shipment_item(self, orders, amazon_dir, tmp_path):
        bronze = tmp_path / "bronze"
        run_amazon_ingestion(amazon_source(), amazon_dir / "Retail.OrderHistory.1.csv", bronze)
        rows = bronze_rows(bronze, "amazon")
        columns = bronze_columns(bronze, "amazon")
        by_order: dict[str, set] = {}
        idx_order, idx_total = columns.index("website_order_id"), columns.index("total_owed")
        for row in rows:
            by_order.setdefault(row[idx_order], set()).add(row[idx_total])
        assert all(len(totals) == 1 for totals in by_order.values())

    def test_run_ingestion_dispatches_to_amazon(self, amazon_dir, tmp_path):
        bronze = tmp_path / "bronze"
        run_ingestion(amazon_source(), amazon_dir / "Retail.OrderHistory.1.csv", bronze)
        rows = bronze_rows(bronze, "amazon")
        assert rows

    def test_reingest_same_file_is_idempotent(self, orders, amazon_dir, tmp_path):
        bronze = tmp_path / "bronze"
        source = amazon_source()
        run_amazon_ingestion(source, amazon_dir / "Retail.OrderHistory.1.csv", bronze)
        first = len(bronze_rows(bronze, "amazon"))
        run_amazon_ingestion(source, amazon_dir / "Retail.OrderHistory.1.csv", bronze)
        second = len(bronze_rows(bronze, "amazon"))
        assert first == len(orders)
        assert second == first

    def test_malformed_row_raises_ingestion_error(self, tmp_path):
        bad = tmp_path / "bad.csv"
        header = (
            "Website Order ID,Order Date,Currency,Unit Price,Unit Price Tax,Shipping Charge,"
            "Total Discounts,Total Owed,Shipment Item Subtotal,Shipment Item Subtotal Tax,ASIN,"
            "Quantity,Order Status,Shipment Status,Ship Date,Product Name\n"
        )
        # Ship Date is unparseable ("not-a-date").
        row = (
            "111-1234567-1234567,2026-01-01T12:00:00Z,USD,9.99,0.50,0.00,0.00,10.49,9.99,0.50,"
            "B000000000,1,Closed,Shipped,not-a-date,Widget\n"
        )
        bad.write_text(header + row, encoding="utf-8")
        with pytest.raises(IngestionError):
            run_amazon_ingestion(amazon_source(), bad, tmp_path / "bronze")
