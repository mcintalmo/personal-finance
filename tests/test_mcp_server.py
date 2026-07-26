"""Tests for the MCP tool server (personal_finance.mcp_server).

The bulk of this file is adversarial: `run_sql` hands a language model an
arbitrary SQL channel into the user's financial warehouse, and the case for
that being safe rests entirely on two DuckDB settings. If either regresses,
every other test here would still pass while the guarantee in the module
docstring quietly became false — so the guards get tested directly, by
attacking them.

The warehouse is built by hand rather than through dbt: these tests are about
the connection's *capabilities*, not about mart contents, and a synthetic
database keeps them at milliseconds. The tools' SQL is checked against real
mart schemas in test_dbt.py, where a built warehouse already exists.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import duckdb
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from personal_finance.config import get_settings
from personal_finance.mcp_server import build_server, readonly_connection

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def warehouse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A minimal warehouse with just enough shape to query."""
    path = tmp_path / "warehouse.duckdb"
    # A parquet-backed view mirroring the real silver layer, which is dbt
    # *views* over the bronze landing zone rather than materialized tables.
    bronze = tmp_path / "bronze"
    bronze.mkdir()
    with duckdb.connect() as writer:
        writer.execute(
            f"COPY (SELECT 'COSTCO' AS merchant_name, -42.00 AS amount) "
            f"TO '{bronze / 'txns.parquet'}' (FORMAT parquet)"
        )
    with duckdb.connect(str(path)) as conn:
        conn.execute("CREATE SCHEMA main_gold")
        conn.execute("CREATE SCHEMA main_silver")
        conn.execute(
            "CREATE VIEW main_silver.silver_transactions AS "
            f"SELECT * FROM read_parquet('{bronze}/*.parquet')"
        )
        conn.execute(
            "CREATE TABLE main_gold.gold_monthly_flow AS SELECT * FROM (VALUES "
            "(DATE '2026-01-01', 5000.00, 3000.00, 2000.00, 42),"
            "(DATE '2026-02-01', 5000.00, 3500.00, 1500.00, 47)"
            ") AS t(month, total_inflow, total_outflow, net_amount, transaction_count)"
        )
        conn.execute(
            "CREATE TABLE main_silver.silver_merchants AS SELECT * FROM (VALUES "
            "('COSTCO', 12, 1400.00), ('NETFLIX', 6, 92.94)"
            ") AS t(merchant_name, transaction_count, total_outflow)"
        )
        conn.execute("CHECKPOINT")
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(path))
    monkeypatch.setenv("DATA_BRONZE_PATH", str(bronze))
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


def call(tool: str, **arguments: Any) -> Any:
    """Invoke one tool through a real MCP client, as an agent would."""

    async def _run() -> Any:
        async with Client(build_server()) as client:
            result = await client.call_tool(tool, arguments)
            return result.data

    return asyncio.run(_run())


class TestReadOnlyGuarantee:
    """The two settings the whole `run_sql` design rests on.

    Attacked directly rather than asserted about, because a guard that is
    merely configured is not a guard that is enforced.
    """

    @pytest.mark.parametrize(
        "statement",
        [
            "INSERT INTO main_gold.gold_monthly_flow VALUES (DATE '2026-03-01', 1, 1, 0, 1)",
            "UPDATE main_gold.gold_monthly_flow SET total_inflow = 0",
            "DELETE FROM main_gold.gold_monthly_flow",
            "DROP TABLE main_gold.gold_monthly_flow",
            "CREATE TABLE main_gold.evil (x INT)",
            "ALTER TABLE main_gold.gold_monthly_flow RENAME TO gone",
        ],
    )
    def test_the_database_refuses_every_write(self, warehouse: Path, statement: str) -> None:
        """Enforced by the engine, not by inspecting the model's SQL for
        banned keywords — so there is no phrasing that slips past."""
        with pytest.raises(ToolError, match="Query failed"):
            call("run_sql", query=statement)
        # And the data is genuinely untouched.
        with duckdb.connect(str(warehouse), read_only=True) as conn:
            assert conn.execute("SELECT count(*) FROM main_gold.gold_monthly_flow").fetchone() == (
                2,
            )

    @pytest.mark.parametrize(
        "statement",
        [
            "SELECT * FROM read_csv('/etc/hosts')",
            "SELECT content FROM read_text('/etc/hosts')",
            "SELECT * FROM read_csv_auto('/etc/passwd')",
        ],
    )
    def test_local_files_cannot_be_read_into_the_result_set(
        self, warehouse: Path, statement: str
    ) -> None:
        """read_only=True does NOT cover this on its own.

        A read-only connection will happily read arbitrary local files, and
        every row lands in the model's context. This is the exfiltration path
        that `SET enable_external_access=false` exists to close, and the reason
        both settings are applied rather than just the obvious one.
        """
        with pytest.raises(ToolError, match="Query failed"):
            call("run_sql", query=statement)

    @pytest.mark.parametrize(
        "statement",
        [
            "SET enable_external_access=true",
            "SET GLOBAL enable_external_access=true",
            "PRAGMA enable_external_access=true",
            "RESET enable_external_access",
        ],
    )
    def test_the_file_access_guard_cannot_be_switched_back_on(
        self, warehouse: Path, statement: str
    ) -> None:
        """The guard is one-way. Without this property a model could simply
        re-enable external access in one query and read files in the next."""
        with pytest.raises(ToolError, match="Query failed"):
            call("run_sql", query=statement)

    def test_attaching_another_database_is_refused(self, warehouse: Path, tmp_path: Path) -> None:
        """ATTACH would otherwise be a way to reach a writable database."""
        with pytest.raises(ToolError, match="Query failed"):
            call("run_sql", query=f"ATTACH '{tmp_path / 'side.duckdb'}' AS side")

    def test_ordinary_reads_still_work(self, warehouse: Path) -> None:
        """The guards must not have been bought by breaking the feature."""
        result = call("run_sql", query="SELECT count(*) AS n FROM main_gold.gold_monthly_flow")
        assert result["rows"] == [{"n": 2}]

    def test_parquet_backed_views_are_still_readable(self, warehouse: Path) -> None:
        """The regression that a blanket external-access ban causes.

        The silver layer is dbt views over bronze Parquet, so disabling
        external access outright makes every transaction-level query fail with
        a permission error — the guard looks like it works while silently
        removing half the warehouse. `allowed_directories` re-permits the
        bronze landing zone specifically. Found by running the tools against a
        real dbt-built warehouse; a synthetic all-tables fixture cannot catch
        it, which is why this fixture has a Parquet-backed view.
        """
        result = call("run_sql", query="SELECT * FROM main_silver.silver_transactions")
        assert result["rows"] == [{"merchant_name": "COSTCO", "amount": -42.0}]

    def test_the_bronze_allowlist_cannot_be_widened(self, warehouse: Path) -> None:
        """Allow-listing bronze must not become a general escape hatch."""
        with pytest.raises(ToolError, match="Query failed"):
            call("run_sql", query="SET allowed_directories=['/']")
        with pytest.raises(ToolError, match="Query failed"):
            call("run_sql", query="SELECT * FROM read_csv('/etc/hosts')")

    def test_data_cannot_be_copied_out_to_a_file(self, warehouse: Path, tmp_path: Path) -> None:
        """COPY ... TO is the write-shaped exfiltration path: it would let a
        model dump the ledger somewhere it could be read back later."""
        with pytest.raises(ToolError, match="Query failed"):
            call(
                "run_sql",
                query=f"COPY (SELECT * FROM main_gold.gold_monthly_flow) TO '{tmp_path / 'leak.csv'}'",
            )
        assert not (tmp_path / "leak.csv").exists()


class TestRunSql:
    def test_returns_rows_with_column_names(self, warehouse: Path) -> None:
        result = call("run_sql", query="SELECT month, net_amount FROM main_gold.gold_monthly_flow")
        assert result["row_count"] == 2
        assert result["truncated"] is False
        assert result["rows"][0] == {"month": "2026-01-01", "net_amount": 2000.0}

    def test_caps_rows_and_says_so(self, warehouse: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Silent truncation would let a model conclude "only 2 results" from
        a capped page — worse than being told the answer is incomplete."""
        monkeypatch.setenv("MCP_MAX_ROWS", "1")
        get_settings.cache_clear()
        result = call("run_sql", query="SELECT * FROM main_gold.gold_monthly_flow")
        assert result["row_count"] == 1
        assert result["truncated"] is True
        assert result["row_limit"] == 1

    def test_does_not_claim_truncation_when_the_result_exactly_fills_the_cap(
        self, warehouse: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The off-by-one worth pinning: two rows under a cap of two is
        complete, and reporting it as truncated would send the model looking
        for data that does not exist."""
        monkeypatch.setenv("MCP_MAX_ROWS", "2")
        get_settings.cache_clear()
        result = call("run_sql", query="SELECT * FROM main_gold.gold_monthly_flow")
        assert result["row_count"] == 2
        assert result["truncated"] is False

    def test_a_runaway_query_is_interrupted_rather_than_hanging(
        self, warehouse: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cartesian join costs a model nothing to write. Without the
        timeout the agent waits forever and the user sees a hung chat."""
        monkeypatch.setenv("MCP_QUERY_TIMEOUT_SECONDS", "0.5")
        get_settings.cache_clear()
        with pytest.raises(ToolError, match="time limit"):
            call("run_sql", query="SELECT count(*) FROM range(1000000000) a, range(100000) b")

    def test_a_syntax_error_returns_the_databases_own_message(self, warehouse: Path) -> None:
        """The model wrote the SQL and can fix it, so the parser's message is
        the most useful thing to hand back — not a generic failure."""
        with pytest.raises(ToolError, match="Query failed"):
            call("run_sql", query="SELECT FROM WHERE")


class TestSchemaDiscovery:
    def test_list_tables_reports_what_can_be_queried(self, warehouse: Path) -> None:
        names = {row["qualified_name"] for row in call("list_tables")}
        assert "main_gold.gold_monthly_flow" in names
        assert "main_silver.silver_merchants" in names

    def test_list_tables_includes_row_counts(self, warehouse: Path) -> None:
        rows = {row["qualified_name"]: row["row_count"] for row in call("list_tables")}
        assert rows["main_gold.gold_monthly_flow"] == 2

    def test_describe_table_returns_columns(self, warehouse: Path) -> None:
        columns = {
            row["column_name"]
            for row in call("describe_table", qualified_name="main_gold.gold_monthly_flow")
        }
        assert {"month", "total_inflow", "net_amount"} <= columns

    def test_unknown_table_says_how_to_find_the_real_ones(self, warehouse: Path) -> None:
        with pytest.raises(ToolError, match="list_tables"):
            call("describe_table", qualified_name="main_gold.does_not_exist")

    def test_a_schema_outside_the_warehouse_is_refused(self, warehouse: Path) -> None:
        with pytest.raises(ToolError, match="Unknown schema"):
            call("describe_table", qualified_name="pg_catalog.pg_type")


class TestCuratedTools:
    def test_monthly_flow_returns_every_month_unfiltered(self, warehouse: Path) -> None:
        assert len(call("monthly_flow")) == 2

    def test_monthly_flow_bounds_are_inclusive(self, warehouse: Path) -> None:
        rows = call("monthly_flow", start_month="2026-02-01", end_month="2026-02-01")
        assert [row["month"] for row in rows] == ["2026-02-01"]

    def test_top_merchants_is_ordered_by_spend(self, warehouse: Path) -> None:
        rows = call("top_merchants", limit=5)
        assert [row["merchant_name"] for row in rows] == ["COSTCO", "NETFLIX"]

    def test_recurring_flows_rejects_a_bad_direction(self, warehouse: Path) -> None:
        """Caught before it reaches SQL so the model gets told the vocabulary
        rather than an empty result it would read as "nothing recurring"."""
        with pytest.raises(ToolError, match="inflow"):
            call("recurring_flows", flow="sideways")

    def test_a_tool_whose_mart_is_missing_says_which_command_builds_it(
        self, warehouse: Path
    ) -> None:
        """The synthetic warehouse has no gold_forecasts. An agent that hits
        this needs to know the fix, not see a CatalogException."""
        with pytest.raises(ToolError, match="pf transform"):
            call("forecast")


class TestConnection:
    def test_a_missing_warehouse_is_explained(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(tmp_path / "absent.duckdb"))
        get_settings.cache_clear()
        with pytest.raises(ToolError, match="pf init-db"), readonly_connection():
            pass  # pragma: no cover - the context manager raises on entry
        get_settings.cache_clear()

    def test_the_connection_is_closed_even_when_the_body_raises(self, warehouse: Path) -> None:
        held: list[duckdb.DuckDBPyConnection] = []
        with pytest.raises(RuntimeError), readonly_connection() as conn:
            held.append(conn)
            raise RuntimeError("boom")
        with pytest.raises(duckdb.Error):
            held[0].execute("SELECT 1")


class TestServerSurface:
    def test_exposes_the_expected_tools(self) -> None:
        """Pinned deliberately: this is a *governed* surface, and a tool
        appearing here without being noticed is how a write path would get
        handed to an agent."""

        async def _tools() -> set[str]:
            async with Client(build_server()) as client:
                return {tool.name for tool in await client.list_tools()}

        assert asyncio.run(_tools()) == {
            "list_tables",
            "describe_table",
            "list_categories",
            "monthly_flow",
            "spend_by_category",
            "top_merchants",
            "budget_status",
            "recurring_flows",
            "forecast",
            "search_transactions",
            "callouts",
            "run_sql",
        }

    def test_every_tool_is_described_for_a_model(self) -> None:
        """A tool with no description is a tool a small local model will
        either ignore or misuse."""

        async def _descriptions() -> dict[str, str | None]:
            async with Client(build_server()) as client:
                return {tool.name: tool.description for tool in await client.list_tools()}

        for name, description in asyncio.run(_descriptions()).items():
            assert description, f"{name} has no description"
            assert len(description) > 40, f"{name}'s description is too thin to guide a model"
