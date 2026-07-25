"""FastAPI read/write layer over the gold marts (Phase 6 — Serving).

This is a thin query layer, not a business-logic layer: every response
either projects a gold/silver dbt model directly, or reuses an existing
module (`personal_finance.review`, `personal_finance.user_config`) that
already encapsulates the relevant logic for the CLI. The Streamlit app
(`personal_finance.webapp`) is the only intended consumer, but the contract
is a plain HTTP API so any client (a future React UI, per
docs/ARCHITECTURE.md) can use it unchanged.

Every endpoint opens a short-lived DuckDB connection (the same pattern the
`pf` CLI already uses) and closes it before returning — this is a local,
single-user app with no concurrent-writer contention to manage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

import duckdb
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from personal_finance.callouts import CalloutFeed, detect_callouts
from personal_finance.config import get_settings
from personal_finance.exceptions import ConfigurationError, NotFoundError
from personal_finance.llm_categorize import fetch_category_paths
from personal_finance.models import EntityKind
from personal_finance.review import (
    fetch_review_queue,
    fetch_split_review_queue,
    record_label,
)
from personal_finance.user_config import (
    config_file_names,
    read_config_file,
    write_config_file,
)

if TYPE_CHECKING:
    from collections.abc import Generator

app = FastAPI(title="personal-finance API", version="0.1.0")

_REVIEW_KINDS: dict[str, EntityKind] = {
    "transaction": EntityKind.TRANSACTION,
    "split": EntityKind.SPLIT,
}


def get_conn() -> Generator[duckdb.DuckDBPyConnection]:
    warehouse = get_settings().data.warehouse_path
    if not warehouse.exists():
        raise HTTPException(
            status_code=503,
            detail=f"{warehouse} does not exist yet — run `pf init-db` and `pf transform` first.",
        )
    conn = duckdb.connect(str(warehouse))
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[duckdb.DuckDBPyConnection, Depends(get_conn)]


def _require_table_built(conn: duckdb.DuckDBPyConnection, schema: str, table: str) -> None:
    result = conn.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = $schema AND table_name = $table",
        {"schema": schema, "table": table},
    ).fetchone()
    if not result or not result[0]:
        # Name the table: a user whose `pf transform` partially failed has
        # already run the suggested command, and "run it again" tells them
        # nothing about which model is actually missing.
        raise HTTPException(
            status_code=503,
            detail=f"{schema}.{table} has not been built yet — run `pf transform` first.",
        )


def _require_gold_built(conn: duckdb.DuckDBPyConnection) -> None:
    _require_table_built(conn, "main_gold", "gold_category_rollups")


def _require_silver_built(conn: duckdb.DuckDBPyConnection) -> None:
    _require_table_built(conn, "main_silver", "silver_transactions")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


class OverviewMonth(BaseModel):
    month: str
    total_outflow: float
    total_inflow: float
    net_amount: float
    transaction_count: int


class Overview(BaseModel):
    total_outflow: float
    total_inflow: float
    net_amount: float
    months: list[OverviewMonth]


@app.get("/overview")
def overview(conn: Conn) -> Overview:
    _require_gold_built(conn)
    rows = conn.execute(
        "SELECT month, total_outflow, total_inflow, net_amount, transaction_count "
        "FROM main_gold.gold_monthly_flow ORDER BY month"
    ).fetchall()
    months = [
        OverviewMonth(
            month=month.date().isoformat(),
            total_outflow=float(total_outflow),
            total_inflow=float(total_inflow),
            net_amount=float(net_amount),
            transaction_count=count,
        )
        for month, total_outflow, total_inflow, net_amount, count in rows
    ]
    return Overview(
        total_outflow=sum(m.total_outflow for m in months),
        total_inflow=sum(m.total_inflow for m in months),
        net_amount=sum(m.net_amount for m in months),
        months=months,
    )


class TopMerchant(BaseModel):
    merchant_name: str
    transaction_count: int
    total_outflow: float


@app.get("/merchants/top")
def top_merchants(conn: Conn, limit: int = 10) -> list[TopMerchant]:
    _require_silver_built(conn)
    rows = conn.execute(
        "SELECT merchant_name, transaction_count, total_outflow "
        "FROM main_silver.silver_merchants ORDER BY total_outflow DESC LIMIT $limit",
        {"limit": limit},
    ).fetchall()
    return [
        TopMerchant(merchant_name=name, transaction_count=count, total_outflow=float(outflow))
        for name, count, outflow in rows
    ]


class CategoryRollup(BaseModel):
    category_id: str
    parent_id: str | None
    name: str
    path: str
    depth: int
    transaction_count: int
    total_outflow: float
    total_inflow: float
    net_amount: float


@app.get("/categories/sunburst")
def sunburst(conn: Conn) -> list[CategoryRollup]:
    """Every taxonomy category with its rollup — Plotly's sunburst wants
    ids/parents/values, which this maps to 1:1 (category_id/parent_id/total_outflow)."""
    _require_gold_built(conn)
    rows = conn.execute(
        "SELECT category_id, parent_id, name, path, depth, transaction_count, "
        "total_outflow, total_inflow, net_amount FROM main_gold.gold_category_rollups"
    ).fetchall()
    return [
        CategoryRollup(
            category_id=row[0],
            parent_id=row[1],
            name=row[2],
            path=row[3],
            depth=row[4],
            transaction_count=row[5],
            total_outflow=float(row[6]),
            total_inflow=float(row[7]),
            net_amount=float(row[8]),
        )
        for row in rows
    ]


class SankeyEdge(BaseModel):
    stage: Literal["income", "spend"]
    source_node: str
    target_node: str
    value: float


@app.get("/sankey")
def sankey(conn: Conn) -> list[SankeyEdge]:
    _require_gold_built(conn)
    rows = conn.execute(
        "SELECT stage, source_node, target_node, value FROM main_gold.gold_sankey_flow"
    ).fetchall()
    return [
        SankeyEdge(stage=s, source_node=src, target_node=tgt, value=float(v))
        for s, src, tgt, v in rows
    ]


class BudgetActual(BaseModel):
    budget_id: str
    name: str
    category_id: str
    period: str
    budgeted_amount: float
    period_start: str
    actual_outflow: float
    variance: float


@app.get("/budgets")
def budgets(conn: Conn) -> list[BudgetActual]:
    _require_gold_built(conn)
    rows = conn.execute(
        "SELECT budget_id, name, category_id, period, budgeted_amount, period_start, "
        "actual_outflow, variance FROM main_gold.gold_budget_actuals "
        "ORDER BY name, period_start"
    ).fetchall()
    return [
        BudgetActual(
            budget_id=row[0],
            name=row[1],
            category_id=row[2],
            period=row[3],
            budgeted_amount=float(row[4]),
            period_start=row[5].date().isoformat(),
            actual_outflow=float(row[6]),
            variance=float(row[7]),
        )
        for row in rows
    ]


@app.get("/callouts")
def callouts(conn: Conn, limit: int | None = None) -> CalloutFeed:
    """Ranked trend/anomaly observations, computed on demand.

    Needs the same gold models the forecaster reads, since it reuses
    `forecast.load_series` for the monthly histories rather than
    re-deriving them.
    """
    _require_gold_built(conn)
    _require_table_built(conn, "main_gold", "gold_recurring_flows")
    _require_table_built(conn, "main_gold", "gold_line_items")
    _require_table_built(conn, "main_gold", "gold_category_ancestors")
    return detect_callouts(conn, limit=limit)


class TransactionReviewItem(BaseModel):
    kind: Literal["transaction"] = "transaction"
    subject_id: str
    posted_on: str
    amount: float
    merchant_name: str | None
    description_raw: str | None
    source: str


class SplitReviewItem(BaseModel):
    kind: Literal["split"] = "split"
    subject_id: str
    transaction_id: str
    asin: str
    product_name: str
    amount: float
    quantity: int


@app.get("/review/queue")
def review_queue(
    conn: Conn, kind: Literal["transaction", "split"] = "transaction", limit: int = 20
) -> list[TransactionReviewItem] | list[SplitReviewItem]:
    _require_silver_built(conn)
    if kind == "split":
        items = fetch_split_review_queue(conn, limit=limit)
        return [
            SplitReviewItem(
                subject_id=item.split_id,
                transaction_id=item.transaction_id,
                asin=item.asin,
                product_name=item.product_name,
                amount=float(item.amount),
                quantity=item.quantity,
            )
            for item in items
        ]
    items = fetch_review_queue(conn, limit=limit)
    return [
        TransactionReviewItem(
            subject_id=item.transaction_id,
            posted_on=item.posted_on.isoformat(),
            amount=float(item.amount),
            merchant_name=item.merchant_name,
            description_raw=item.description_raw,
            source=item.source,
        )
        for item in items
    ]


class LabelRequest(BaseModel):
    kind: Literal["transaction", "split"] = "transaction"
    subject_id: str
    category_path: str
    note: str | None = None


class LabelResponse(BaseModel):
    id: str
    subject_id: str
    category_id: str


@app.post("/review/label")
def review_label(request: LabelRequest, conn: Conn) -> LabelResponse:
    category_paths = fetch_category_paths(conn)
    try:
        label = record_label(
            conn,
            request.subject_id,
            request.category_path,
            category_paths,
            subject_kind=_REVIEW_KINDS[request.kind],
            note=request.note,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return LabelResponse(id=label.id, subject_id=label.subject_id, category_id=label.category_id)


class ConfigFile(BaseModel):
    name: str
    content: str


@app.get("/config")
def list_config_files() -> list[str]:
    return sorted(config_file_names())


@app.get("/config/{name}")
def get_config_file(name: str) -> ConfigFile:
    if name not in config_file_names():
        raise HTTPException(status_code=404, detail=f"Unknown config file {name!r}")
    return ConfigFile(name=name, content=read_config_file(name))


@app.put("/config/{name}")
def put_config_file(name: str, request: ConfigFile) -> ConfigFile:
    if name not in config_file_names():
        raise HTTPException(status_code=404, detail=f"Unknown config file {name!r}")
    try:
        write_config_file(name, request.content)
    except ConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ConfigFile(name=name, content=request.content)
