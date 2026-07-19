"""End-to-end test of the dbt medallion skeleton.

Seeds a temporary warehouse from the example taxonomy, then runs ``dbt build``
programmatically (models + data tests). Because this runs under pytest, dbt's
tests are wired into CI with no extra workflow step: if a dbt data test fails,
CI fails.
"""

import warnings
from pathlib import Path

import duckdb
import pytest

from personal_finance.ddl import create_schema
from personal_finance.ingest import run_ingestion
from personal_finance.seed import seed_categories
from personal_finance.synth import generate_scenario, write_scenario
from personal_finance.user_config import load_user_config

REPO_ROOT = Path(__file__).parent.parent
EXAMPLES_CONFIG_DIR = REPO_ROOT / "config" / "examples"

# A heterogeneous mix so silver_transactions is exercised across the ingestion
# surface: CSV without external_id, CSV with external_id, debit/credit columns,
# and OFX.
_BRONZE_SOURCES = [
    ("chase_checking", "chase_checking.csv"),
    ("venmo", "venmo.csv"),
    ("capital_one", "capital_one.csv"),
    ("chase_sapphire", "ofx.ofx"),
]


@pytest.fixture(scope="module")
def built_warehouse(tmp_path_factory):
    """A seeded warehouse, with a bronze layer ingested, on which `dbt build`
    has run once.

    Env-var handling and warning suppression are scoped to the dbt invocation
    only: a developer's own DATA_WAREHOUSE_PATH is restored afterwards, and
    warnings from this project's code (schema creation, seeding) still fail
    the run under the global ``filterwarnings = error`` regime — only dbt's
    dependency-stack noise is silenced.
    """
    root = tmp_path_factory.mktemp("wh")
    warehouse = root / "warehouse.duckdb"
    bronze = root / "bronze"
    config = load_user_config(EXAMPLES_CONFIG_DIR)
    with duckdb.connect(str(warehouse)) as conn:
        create_schema(conn)
        seed_categories(conn, config.taxonomy)

    exports = root / "exports"
    write_scenario(generate_scenario(seed=42, months=2), exports)
    sources = {s.name: s for s in config.sources}
    for name, filename in _BRONZE_SOURCES:
        run_ingestion(sources[name], exports / filename, bronze)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(warehouse))
    monkeypatch.setenv("DATA_BRONZE_PATH", str(bronze))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from dbt.cli.main import dbtRunner

            result = dbtRunner().invoke(
                [
                    "build",
                    "--project-dir",
                    str(REPO_ROOT / "transform"),
                    "--profiles-dir",
                    str(REPO_ROOT / "transform"),
                ]
            )
    finally:
        monkeypatch.undo()
    return warehouse, bronze, config, result


class TestDbtBuild:
    def test_build_succeeds_including_data_tests(self, built_warehouse):
        _, _, _, result = built_warehouse
        assert result.success, f"dbt build failed: {result.exception}"

    def test_silver_matches_seeded_categories(self, built_warehouse):
        warehouse, _, config, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (count,) = conn.execute("select count(*) from main_silver.silver_categories").fetchone()
        assert count == len(config.category_paths())

    def test_gold_paths_match_taxonomy_paths(self, built_warehouse):
        warehouse, _, config, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            paths = {
                path
                for (path,) in conn.execute(
                    "select path from main_gold.gold_category_paths"
                ).fetchall()
            }
        assert paths == config.category_paths()

    def test_gold_depth_consistent_with_path(self, built_warehouse):
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute("select path, depth from main_gold.gold_category_paths").fetchall()
        for path, depth in rows:
            assert depth == path.count("/")


class TestSilverTransactions:
    def _rows(self, warehouse: Path) -> list[tuple]:
        with duckdb.connect(str(warehouse)) as conn:
            return conn.execute(
                "select transaction_id, source, amount, flow, description_raw "
                "from main_silver.silver_transactions"
            ).fetchall()

    def test_unions_every_ingested_source(self, built_warehouse):
        warehouse, bronze, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            silver = conn.execute(
                "select count(*), count(distinct transaction_id), count(distinct source) "
                "from main_silver.silver_transactions"
            ).fetchone()
            (bronze_distinct,) = conn.execute(
                "select count(distinct row_hash) from "
                f"read_parquet('{bronze}/bronze/*/*.parquet', union_by_name = true)"
            ).fetchone()
        count, distinct_ids, distinct_sources = silver
        assert count == bronze_distinct  # one row per unique bronze transaction
        assert distinct_ids == count  # transaction_id is the grain (no dups)
        assert distinct_sources == len(_BRONZE_SOURCES)

    def test_flow_matches_amount_sign(self, built_warehouse):
        warehouse, _, _, _ = built_warehouse
        for _tid, _source, amount, flow, _desc in self._rows(warehouse):
            expected = "outflow" if amount < 0 else "inflow"
            assert flow == expected

    def test_amounts_normalized_to_two_decimal_places(self, built_warehouse):
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (scale,) = conn.execute(
                "select numeric_scale from information_schema.columns "
                "where table_name = 'silver_transactions' and column_name = 'amount'"
            ).fetchone()
        assert scale == 2

    def test_descriptions_present_and_trimmed(self, built_warehouse):
        warehouse, _, _, _ = built_warehouse
        for _tid, _source, _amount, _flow, desc in self._rows(warehouse):
            assert desc is None or desc == desc.strip()


class TestSilverMerchants:
    def test_merchant_name_is_normalized_key(self, built_warehouse):
        """Every cleaned name is upper-cased, trimmed, and stripped of the
        obvious noise (store/reference numbers)."""
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            names = [
                name
                for (name,) in conn.execute(
                    "select distinct merchant_name from main_silver.silver_transactions "
                    "where merchant_name is not null"
                ).fetchall()
            ]
        assert names
        for name in names:
            assert name == name.strip() == name.upper()
            assert "#" not in name

    def test_locality_and_store_numbers_stripped_end_to_end(self, built_warehouse):
        """'CHEVRON 0093 BELLEVUE WA' across locations collapses to one merchant."""
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            merchants = {
                name
                for (name,) in conn.execute(
                    "select merchant_name from main_silver.silver_merchants"
                ).fetchall()
            }
        assert "CHEVRON" in merchants
        assert "TRADER JOE'S" in merchants

    def test_dimension_covers_every_named_transaction(self, built_warehouse):
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (named_txns,) = conn.execute(
                "select count(*) from main_silver.silver_transactions "
                "where merchant_name is not null"
            ).fetchone()
            (dim_total, dim_rows) = conn.execute(
                "select sum(transaction_count), count(*) from main_silver.silver_merchants"
            ).fetchone()
        assert dim_total == named_txns  # every named transaction is counted once
        assert dim_rows > 0
