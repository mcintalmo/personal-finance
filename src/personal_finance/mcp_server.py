"""Governed MCP tool server over the warehouse (Phase 7).

This is the agent-facing sibling of :mod:`personal_finance.api`. Both are thin
adapters over the same warehouse; neither is layered on the other. The REST API
exists to render a dashboard, so its shapes follow screens. This server exists
for a language model, so its shapes follow *questions* — which is why it is
hand-written rather than generated from the FastAPI app with
``FastMCP.from_fastapi``. Auto-generation would produce tools shaped like HTTP
routes and, more seriously, would expose ``PUT /config/{name}`` — handing an
agent the ability to rewrite the user's YAML config.

**Everything here is read-only, enforced by the database rather than by
inspecting SQL.** :func:`readonly_connection` opens DuckDB with
``read_only=True`` and disables external access. Both are load-bearing, and
neither is sufficient alone:

* ``read_only=True`` makes INSERT/UPDATE/DROP/CREATE/ATTACH fail inside the
  engine. A model cannot talk its way past it, because nothing is parsing the
  model's SQL looking for keywords to ban.
* It does **not** stop ``read_csv('/etc/passwd')`` or ``read_text(...)``. A
  read-only connection will happily read arbitrary local files into the result
  set, and from there into the model's context. ``SET
  enable_external_access=false`` closes that, and DuckDB refuses every attempt
  to turn it back on (``SET``, ``SET GLOBAL``, ``PRAGMA``, ``RESET`` all raise),
  so the guard survives hostile SQL.

**No filesystem allowlist is needed, and that is deliberate.** An earlier
version of this module allow-listed the bronze landing zone so the silver
layer would resolve, because silver was dbt *views* over Parquet. Two review
findings killed that design outright:

* ``allowed_directories`` confers **write as well as read**, and DuckDB has no
  read-only variant (``allowed_paths`` blocks writes but also blocks the
  directory listing a glob needs). So ``COPY ... TO '<bronze>/x.parquet'``
  succeeded, and since the views globbed that directory the injected rows
  appeared on the very next query with no ``pf transform`` — a live
  ledger-injection path, and a mismatched schema broke every transaction-level
  query permanently.
* Scoping the grant per connection does not help: both settings are **GLOBAL**
  to the shared DuckDB instance, so a grant made for one connection leaks to
  every other one open at the same time, and MCP hosts routinely call tools in
  parallel.

The fix was to remove the need for the grant rather than to police it: the
silver layer is now materialized as tables (``transform/dbt_project.yml``), so
nothing in the warehouse reads from disk. Every connection here runs with no
filesystem access whatsoever, which is both airtight and simpler — and it
leaves the *whole* warehouse queryable, silver included.

That combination is what makes :func:`run_sql` defensible. Open-ended SQL is
the difference between "an agent that reads the same tables the dashboard
shows" and "an agent that can actually analyse", and the curated tools below
cannot anticipate every question worth asking.

**Tool output is untrusted input.** Merchant names and descriptions originate
in ingested bank exports, so a transaction description is attacker-controlled
text that lands directly in a model's context. The mitigation here is
structural rather than textual: there is no write path to reach, so the worst
a successful injection achieves is a wrong answer rather than a changed
ledger.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from contextlib import contextmanager
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import duckdb
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from personal_finance.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

_GOLD = "main_gold"
_SILVER = "main_silver"

# Schemas an agent may inspect and query. `main` (the app's own entity tables)
# is included deliberately: labels, budgets and forecasts are part of the story
# a financial question needs, and the connection is read-only regardless.
_VISIBLE_SCHEMAS = ("main", _SILVER, _GOLD)


@contextmanager
def readonly_connection() -> Iterator[duckdb.DuckDBPyConnection]:
    """Open the warehouse read-only, with local-file access disabled.

    Short-lived and per-call, matching the pattern the REST API and CLI
    already use — this is a single-user local app with no writer contention.

    No directory is allow-listed: the silver layer is materialized, so nothing
    in the warehouse needs to read a file. See the module docstring for why
    that matters — the allowlist was a write path, and per-connection scoping
    could not contain it because the settings are global to the instance.
    """
    warehouse = get_settings().data.warehouse_path
    if not warehouse.exists():
        message = (
            f"The warehouse {warehouse} does not exist yet. "
            "Run `pf init-db` and `pf transform` first."
        )
        raise ToolError(message)
    try:
        conn = duckdb.connect(str(warehouse), read_only=True)
    except duckdb.ConnectionException as exc:
        # DuckDB refuses a read-only connection while the same *process* holds
        # a read-write one, and its own message ("different configuration than
        # existing connections") does not hint at the cause. This is reachable
        # whenever the server shares a process with something that writes —
        # dbt-duckdb keeps its connection open after a build, for instance.
        message = (
            f"Cannot open {warehouse} read-only because this process already has it "
            "open for writing. Run `pf mcp` as its own process, and make sure no "
            "`pf transform` is in flight."
        )
        raise ToolError(message) from exc
    try:
        conn.execute("SET enable_external_access=false")
        yield conn
    finally:
        conn.close()


def _require_tables(conn: duckdb.DuckDBPyConnection, *qualified: str) -> None:
    """Check EVERY table a tool touches, not just the headline one.

    Checking one table per tool leaves the joins unguarded, so a
    half-transformed warehouse produces a raw CatalogException naming a table
    the agent never asked about — which is the failure this function exists to
    prevent.
    """
    for name in qualified:
        schema, _, table = name.partition(".")
        _require_table(conn, schema, table)


def _require_table(conn: duckdb.DuckDBPyConnection, schema: str, table: str) -> None:
    """Fail with something the agent can act on, not a raw CatalogException."""
    found = conn.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = $schema AND table_name = $table",
        {"schema": schema, "table": table},
    ).fetchone()
    if not found or not found[0]:
        message = (
            f"{schema}.{table} has not been built yet. Run `pf transform` "
            "(and `pf forecast` first, for forecast-derived tables)."
        )
        raise ToolError(message)


def _qualified_tables(conn: duckdb.DuckDBPyConnection) -> list[str]:
    """Every queryable table name, for putting real options in an error message."""
    rows = conn.execute(
        "SELECT table_schema || '.' || table_name FROM information_schema.tables "
        "WHERE table_schema IN ($a, $b, $c) ORDER BY table_schema, table_name",
        dict(zip("abc", _VISIBLE_SCHEMAS, strict=True)),
    ).fetchall()
    return [name for (name,) in rows]


def _schema_hint(conn: duckdb.DuckDBPyConnection, query: str) -> str:
    """Describe whatever the failing query was reaching for.

    If the query names real tables, their columns are the useful reply — a
    wrong column name is otherwise a whole round trip through
    ``describe_table``. Matching is a plain substring test on the qualified
    name rather than SQL parsing: the goal is a better error message, and a
    parser that could be fooled by a name inside a string literal would only
    add a way to be wrong. If nothing matches, the table list is the right
    answer instead, because the model is not yet aiming at anything real.
    """
    try:
        return _describe_mentioned(conn, query)
    except duckdb.Error:
        # This runs inside an exception handler, so anything raised here would
        # replace the model's actual SQL error with an unrelated one — losing
        # the only message that says what went wrong. The hint is a nicety; the
        # original error is not. The timeout timer is also still armed at this
        # point (it is cancelled in the caller's `finally`, which runs after
        # this), so an interrupt landing here is reachable, if narrowly.
        logger.warning("Could not build a schema hint for a failed query", exc_info=True)
        return "Call list_tables and describe_table for the real names before retrying."


def _describe_mentioned(conn: duckdb.DuckDBPyConnection, query: str) -> str:
    lowered = query.lower()
    described = []
    for qualified in _qualified_tables(conn):
        if qualified.lower() not in lowered:
            continue
        schema, _, table = qualified.partition(".")
        columns = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = $schema AND table_name = $table ORDER BY ordinal_position",
            {"schema": schema, "table": table},
        ).fetchall()
        described.append(f"{qualified} has columns: {', '.join(name for (name,) in columns)}")
    if described:
        return " ".join(described)
    return (
        f"The queryable tables are: {', '.join(_qualified_tables(conn))}. "
        "Call describe_table on the one you want for its column names."
    )


def _jsonable(value: Any) -> Any:
    """Make a DuckDB scalar safe to serialize.

    Decimals become floats: money is stored as DECIMAL(18, 2) for exactness,
    but JSON has no decimal type and a model reasoning about "$1,827.48" gains
    nothing from the distinction.
    """
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _records(cursor: duckdb.DuckDBPyConnection, limit: int) -> list[dict[str, Any]]:
    """Materialize at most ``limit`` rows as dicts.

    Capped by fetching fewer rows rather than by wrapping the caller's SQL in
    a LIMIT: rewriting a model's query is fragile (CTEs, existing LIMIT,
    trailing semicolons, comments) and a rewrite that silently changes results
    is worse than one that refuses.
    """
    columns = _unique_columns([description[0] for description in cursor.description or []])
    rows = cursor.fetchmany(limit)
    return [
        {column: _jsonable(value) for column, value in zip(columns, row, strict=True)}
        for row in rows
    ]


def _unique_columns(names: list[str]) -> list[str]:
    """Suffix repeated column names so none are lost to dict collision.

    `SELECT a.*, b.*` across two marts yields duplicate names, and building a
    dict straight from them silently drops every repeat — the row still looks
    well-formed and `row_count` is unaffected, so a model would reason over
    half the columns it asked for and never know. `run_sql`'s own docstring
    recommends exactly the cross-mart joins that trigger it.
    """
    seen: dict[str, int] = {}
    unique = []
    for name in names:
        count = seen.get(name, 0)
        seen[name] = count + 1
        unique.append(name if count == 0 else f"{name}_{count + 1}")
    return unique


def _query(sql: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a curated tool's own SQL and return a capped result envelope.

    Every tabular tool returns the same shape as :func:`run_sql` — one thing
    for a model to learn, and, more importantly, a `truncated` flag on all of
    them. Returning a bare list would let a capped page read as the complete
    answer, which is the same silent-wrongness the row cap exists to avoid.

    """
    limit = get_settings().mcp.max_rows
    with readonly_connection() as conn:
        cursor = conn.execute(sql, params or {})
        rows = _records(cursor, limit)
        return {
            "row_count": len(rows),
            "truncated": len(cursor.fetchmany(1)) > 0,
            "row_limit": limit,
            "rows": rows,
        }


def build_server() -> FastMCP:
    """Construct the MCP server.

    A factory rather than a module-level singleton so tests can build a server
    against a temporary warehouse without the import itself touching the
    filesystem.
    """
    mcp: FastMCP = FastMCP(
        name="personal-finance",
        instructions=(
            "Read-only access to a personal-finance warehouse built with dbt.\n\n"
            "Prefer the curated tools — they encode this project's conventions "
            "(transfers between the user's own accounts are excluded from spend; "
            "outflows are reported as positive magnitudes). Reach for `run_sql` "
            "when a question needs a join, filter or aggregation the curated "
            "tools do not cover; call `describe_table` first so you are writing "
            "SQL against real column names.\n\n"
            "Amounts are dollars. Months are the first day of the month. "
            "Nothing here can modify data."
        ),
    )

    # ── Schema discovery ────────────────────────────────────
    # Exposed as both resources (for hosts that browse them) and tools,
    # because an agent driving `run_sql` needs to *call* for the schema
    # mid-conversation, and many clients surface resources to the user rather
    # than to the model.

    @mcp.tool
    def list_tables() -> list[dict[str, Any]]:
        """List every queryable table, with its schema and row count.

        Start here when writing SQL. `main_gold` holds the query-ready marts
        and is almost always what you want; `main_silver` is cleaned
        transaction-level detail; `main` holds the app's own entity tables.
        """
        with readonly_connection() as conn:
            rows = conn.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema IN ($a, $b, $c) ORDER BY table_schema, table_name",
                dict(zip("abc", _VISIBLE_SCHEMAS, strict=True)),
            ).fetchall()
            tables = []
            for schema, table in rows:
                # Identifiers come from information_schema, not from the model.
                count = conn.execute(f'SELECT count(*) FROM "{schema}"."{table}"').fetchone()
                tables.append(
                    {
                        "schema": schema,
                        "table": table,
                        "qualified_name": f"{schema}.{table}",
                        "row_count": count[0] if count else 0,
                    }
                )
        return tables

    @mcp.tool
    def describe_table(qualified_name: str) -> dict[str, Any]:
        """Show the columns and types of one table, e.g. `main_gold.gold_monthly_flow`.

        Call this before writing `run_sql` against a table you have not used
        yet — guessing column names wastes a round trip.
        """
        schema, _, table = qualified_name.rpartition(".")
        if not schema:
            schema = _GOLD
        if schema not in _VISIBLE_SCHEMAS:
            message = f"Unknown schema {schema!r}. Use one of: {', '.join(_VISIBLE_SCHEMAS)}."
            raise ToolError(message)
        columns = _query(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = $schema AND table_name = $table ORDER BY ordinal_position",
            {"schema": schema, "table": table},
        )
        if not columns["rows"]:
            message = f"No table named {qualified_name!r}. Call list_tables to see what exists."
            raise ToolError(message)
        return columns

    @mcp.resource("schema://tables")
    def tables_resource() -> list[dict[str, Any]]:
        """Every queryable table in the warehouse."""
        return list_tables()

    @mcp.resource("schema://{qualified_name}")
    def table_resource(qualified_name: str) -> dict[str, Any]:
        """Columns and types for one table."""
        return describe_table(qualified_name)

    # ── Curated tools ───────────────────────────────────────

    @mcp.tool
    def list_categories() -> dict[str, Any]:
        """List the category taxonomy as full paths, e.g. `essentials/groceries`.

        Use these paths verbatim when filtering by category — they are the
        vocabulary the rest of the tools expect.
        """
        with readonly_connection() as conn:
            _require_tables(conn, f"{_GOLD}.gold_category_paths")
        return _query(f"SELECT path, depth FROM {_GOLD}.gold_category_paths ORDER BY path")

    @mcp.tool
    def monthly_flow(
        start_month: str | None = None, end_month: str | None = None
    ) -> dict[str, Any]:
        """Total income, spend and net flow per calendar month.

        Months are ISO dates on the first of the month ('2026-01-01'), and both
        bounds are inclusive. Transfers between the user's own accounts are
        excluded, so this is real money in and out.
        """
        with readonly_connection() as conn:
            _require_tables(conn, f"{_GOLD}.gold_monthly_flow")
        return _query(
            "SELECT month, total_inflow, total_outflow, net_amount, transaction_count "
            f"FROM {_GOLD}.gold_monthly_flow "
            "WHERE ($start IS NULL OR month >= $start::DATE) "
            "AND ($end IS NULL OR month <= $end::DATE) ORDER BY month",
            {"start": start_month, "end": end_month},
        )

    @mcp.tool
    def spend_by_category(
        category_path: str | None = None,
        start_month: str | None = None,
        end_month: str | None = None,
    ) -> dict[str, Any]:
        """Spend rolled up by category, including all descendants.

        `category_path` filters to one subtree (e.g. 'essentials' also covers
        'essentials/groceries'); omit it for every category. Outflow is a
        positive magnitude. Bounding by month re-rolls from line items, so the
        result covers only categories with activity in the window; unbounded,
        every category appears, zeroed if nothing rolled up to it.
        """
        with readonly_connection() as conn:
            if start_month or end_month:
                _require_tables(
                    conn,
                    f"{_GOLD}.gold_line_items",
                    f"{_GOLD}.gold_category_ancestors",
                    f"{_GOLD}.gold_category_paths",
                )
            else:
                _require_tables(conn, f"{_GOLD}.gold_category_rollups")
        if start_month or end_month:
            # The rollup mart is all-time, so a time-bounded question has to go
            # back to line items and re-roll through the closure table.
            return _query(
                "SELECT anc.path AS path, sum(-li.amount) AS total_outflow, "
                "count(*) AS transaction_count "
                f"FROM {_GOLD}.gold_line_items AS li "
                f"JOIN {_GOLD}.gold_category_ancestors AS a ON a.category_id = li.category_id "
                f"JOIN {_GOLD}.gold_category_paths AS anc ON anc.id = a.ancestor_id "
                "WHERE li.amount < 0 "
                "AND ($start IS NULL OR li.posted_on >= $start::DATE) "
                "AND ($end IS NULL OR li.posted_on <= $end::DATE) "
                "AND ($path IS NULL OR anc.path = $path OR anc.path LIKE $path || '/%') "
                "GROUP BY anc.path ORDER BY total_outflow DESC",
                {"start": start_month, "end": end_month, "path": category_path},
            )
        return _query(
            "SELECT path, total_outflow, total_inflow, net_amount, transaction_count "
            f"FROM {_GOLD}.gold_category_rollups "
            "WHERE ($path IS NULL OR path = $path OR path LIKE $path || '/%') "
            "ORDER BY total_outflow DESC",
            {"path": category_path},
        )

    @mcp.tool
    def top_merchants(limit: int = 10) -> dict[str, Any]:
        """The merchants the user spends the most with, highest first."""
        with readonly_connection() as conn:
            _require_tables(conn, f"{_SILVER}.silver_merchants")
        return _query(
            "SELECT merchant_name, transaction_count, total_outflow "
            f"FROM {_SILVER}.silver_merchants ORDER BY total_outflow DESC LIMIT $limit",
            {"limit": max(1, limit)},
        )

    @mcp.tool
    def budget_status() -> dict[str, Any]:
        """Budget versus actual for every configured budget and period.

        `variance` is actual minus budgeted (matching gold_budget_actuals), so
        a POSITIVE value means the period ran over.
        """
        with readonly_connection() as conn:
            _require_tables(conn, f"{_GOLD}.gold_budget_actuals")
        return _query(
            "SELECT name, period, period_start, budgeted_amount, actual_outflow, variance "
            f"FROM {_GOLD}.gold_budget_actuals ORDER BY name, period_start"
        )

    @mcp.tool
    def recurring_flows(flow: str | None = None) -> dict[str, Any]:
        """Detected recurring charges and income (subscriptions, rent, salary).

        `flow` filters to 'inflow' or 'outflow'; omit for both. `cadence` is
        one of weekly/biweekly/monthly/quarterly/yearly, and `amount` is a
        positive magnitude in either direction.
        """
        if flow is not None and flow not in {"inflow", "outflow"}:
            message = f"flow must be 'inflow' or 'outflow', not {flow!r}."
            raise ToolError(message)
        with readonly_connection() as conn:
            _require_tables(conn, f"{_GOLD}.gold_recurring_flows")
        return _query(
            "SELECT merchant_name, flow, amount, cadence, occurrence_count, "
            f"first_seen_on, last_seen_on FROM {_GOLD}.gold_recurring_flows "
            "WHERE ($flow IS NULL OR flow = $flow) ORDER BY amount DESC",
            {"flow": flow},
        )

    @mcp.tool
    def forecast() -> dict[str, Any]:
        """Spend and income forecasts for the coming months.

        Each row splits `predicted_amount` into a `committed_amount` (recurring
        charges and income, known rather than estimated) and a
        `variable_amount` (statistically modelled). The interval covers the
        variable part only. Empty until `pf forecast` has been run.
        """
        with readonly_connection() as conn:
            _require_tables(conn, f"{_GOLD}.gold_forecasts")
        return _query(
            "SELECT series_kind, series_label, period_start, horizon, committed_amount, "
            "variable_amount, predicted_amount, lower_bound, upper_bound, trend, model_name "
            f"FROM {_GOLD}.gold_forecasts ORDER BY series_label, horizon"
        )

    @mcp.tool
    def search_transactions(
        merchant: str | None = None,
        category_path: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        min_amount: float | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Find individual transactions matching any combination of filters.

        One row per transaction, newest first. `merchant` is a case-insensitive
        substring match; `min_amount` is a magnitude, so 100 finds transactions
        of $100 or more in either direction. `category_paths` is a list because
        a split transaction (an Amazon order, say) can span several categories.
        """
        with readonly_connection() as conn:
            _require_tables(
                conn,
                f"{_SILVER}.silver_transactions",
                f"{_GOLD}.gold_line_items",
                f"{_GOLD}.gold_category_paths",
            )
        # One row per TRANSACTION. Joining gold_line_items directly fans a split
        # transaction (an Amazon order decomposed into N products) into N rows,
        # each repeating the transaction's FULL amount — so a model summing the
        # result over-reports that spend N-fold, and `limit` starts counting
        # duplicates. Categories are collapsed into a list instead.
        return _query(
            "WITH matched AS ("
            "SELECT t.transaction_id, t.posted_on, t.merchant_name, t.amount, t.flow, "
            "t.is_transfer, p.path AS category_path "
            f"FROM {_SILVER}.silver_transactions AS t "
            f"LEFT JOIN {_GOLD}.gold_line_items AS li ON li.transaction_id = t.transaction_id "
            f"LEFT JOIN {_GOLD}.gold_category_paths AS p ON p.id = li.category_id "
            "WHERE ($merchant IS NULL OR lower(t.merchant_name) LIKE '%' || lower($merchant) || '%') "
            "AND ($start IS NULL OR t.posted_on >= $start::DATE) "
            "AND ($end IS NULL OR t.posted_on <= $end::DATE) "
            "AND ($min IS NULL OR abs(t.amount) >= $min)) "
            "SELECT posted_on, merchant_name, amount, flow, is_transfer, "
            "list_sort(list_distinct(list(category_path))) AS category_paths "
            "FROM matched "
            "WHERE ($path IS NULL OR transaction_id IN (SELECT transaction_id FROM matched "
            "WHERE category_path = $path OR category_path LIKE $path || '/%')) "
            "GROUP BY transaction_id, posted_on, merchant_name, amount, flow, is_transfer "
            "ORDER BY posted_on DESC LIMIT $limit",
            {
                "merchant": merchant,
                "path": category_path,
                "start": start_date,
                "end": end_date,
                "min": min_amount,
                "limit": max(1, limit),
            },
        )

    @mcp.tool
    def callouts(limit: int = 10) -> list[dict[str, Any]]:
        """Notable observations: spending spikes, trends, and budgets at risk.

        Computed on demand. Trend and budget-risk items need `pf forecast` to
        have been run; spike and dip items do not.
        """
        # Imported lazily: personal_finance.callouts pulls in statsmodels via
        # the forecaster, which is a slow import to pay on every server start
        # when most sessions never call this tool.
        from personal_finance.callouts import detect_callouts

        with readonly_connection() as conn:
            # detect_callouts reaches silver_transactions via load_series, so
            # checking only the recurring mart would leave the agent staring at
            # a CatalogException for a table it never named.
            _require_tables(
                conn,
                f"{_GOLD}.gold_recurring_flows",
                f"{_GOLD}.gold_line_items",
                f"{_SILVER}.silver_transactions",
            )
            feed = detect_callouts(conn, limit=max(1, limit))
        return [
            {
                "kind": callout.kind.value,
                "level": callout.level.value,
                "title": callout.title,
                "detail": callout.detail,
                "series_label": callout.series_label,
                "period_start": callout.period_start.isoformat() if callout.period_start else None,
            }
            for callout in feed.callouts
        ]

    # ── Open-ended analysis ─────────────────────────────────

    @mcp.tool
    def run_sql(query: str) -> dict[str, Any]:
        """Run a read-only DuckDB SELECT against the warehouse.

        For questions the curated tools do not cover — joins across marts,
        custom groupings, window functions. Call `list_tables` and
        `describe_table` first so you are writing against real columns.

        Covers the whole warehouse: `main_gold` marts, `main_silver`
        transaction-level detail, and the `main` app tables.

        The connection cannot write and cannot read files, so there is no need
        to be cautious about phrasing; a query that tries either fails rather
        than doing something unintended. Results are capped, and the response
        says so when rows were dropped, so aggregate or add your own LIMIT
        rather than assuming you received everything.
        """
        settings = get_settings().mcp
        limit = settings.max_rows
        with readonly_connection() as conn:
            # Interrupt from a timer thread rather than trying to bound the
            # query in SQL: a cartesian join costs nothing to write and would
            # otherwise hang the agent indefinitely. The connection stays
            # usable afterwards, and it is closed by the context manager.
            # Guarded: timer.cancel() cannot stop a callback already dispatched,
            # and readonly_connection closes the connection immediately after —
            # an unguarded conn.interrupt() would then raise inside the timer
            # thread and print a traceback to stderr.
            def _interrupt() -> None:
                with contextlib.suppress(duckdb.Error):
                    conn.interrupt()

            timer = threading.Timer(settings.query_timeout_seconds, _interrupt)
            timer.start()
            try:
                cursor = conn.execute(query)
                if cursor is None:
                    # DuckDB returns None when the input holds no statement (a
                    # bare comment, or whitespace). Dereferencing .description
                    # would raise AttributeError, which bypasses the
                    # hand-the-model-the-real-message path below.
                    message = "The query contained no SQL statement."
                    raise ToolError(message)
                rows = _records(cursor, limit)
                truncated = len(cursor.fetchmany(1)) > 0
            except duckdb.InterruptException as exc:
                message = (
                    f"Query exceeded the {settings.query_timeout_seconds:g}s time limit. "
                    "Narrow it with a WHERE clause or aggregate before returning rows."
                )
                raise ToolError(message) from exc
            except (duckdb.CatalogException, duckdb.BinderException) as exc:
                # Guessed table and column names are the two commonest ways
                # model-written SQL fails, and the server already knows every
                # real name — so it says them, instead of sending the model
                # away to discover what could have been in the error all along.
                # Observed live, with only DuckDB's own message: a model resent
                # an identical query until the retry budget was gone. DuckDB's
                # hints actively mislead here — "Did you mean" suggests names
                # from attached databases that this tool cannot query, and
                # "Candidate bindings" offered a single unrelated column.
                message = (
                    f"Query failed: {exc}\nDo not resend this query unchanged. "
                    + _schema_hint(conn, query)
                )
                raise ToolError(message) from exc
            except duckdb.Error as exc:
                # The model wrote this SQL and can correct it, so the database's
                # own message is the single most useful thing to hand back.
                message = f"Query failed: {exc}"
                raise ToolError(message) from exc
            finally:
                timer.cancel()
        return {
            "row_count": len(rows),
            "truncated": truncated,
            "row_limit": limit,
            "rows": rows,
        }

    return mcp
