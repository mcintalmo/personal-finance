"""DuckDB DDL for the core schema.

Table definitions mirror the Pydantic models in `personal_finance.models`.
Statements use ``CREATE TABLE IF NOT EXISTS`` so `create_schema` is idempotent,
and `TABLES` is ordered parents-before-children.

Referential integrity is deliberately NOT declared as FOREIGN KEY constraints.
DuckDB executes UPDATEs on tables that are referenced by (or hold) a foreign key
as DELETE + INSERT, so updating any column of a referenced row — re-seeding a
parent category's description, backfilling merchant_id on a transaction that has
splits — raises an over-eager constraint violation (see DuckDB's documented FK
limitations). Integrity is instead enforced by dbt relationship tests and at the
application layer. PRIMARY KEY, UNIQUE, and CHECK constraints are unaffected and
remain declared.

This is the *application* schema (silver-layer entity tables). Bronze landings
are Parquet files managed by dlt; gold marts are dbt models (docs/ARCHITECTURE.md).
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

TABLES: tuple[tuple[str, str], ...] = (
    (
        "accounts",
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            name TEXT NOT NULL,
            account_type TEXT NOT NULL,
            institution TEXT,
            currency TEXT NOT NULL DEFAULT 'USD',
            note TEXT
        )
        """,
    ),
    (
        "merchants",
        """
        CREATE TABLE IF NOT EXISTS merchants (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            canonical_name TEXT NOT NULL UNIQUE,
            aliases TEXT[] NOT NULL DEFAULT [],
            note TEXT
        )
        """,
    ),
    (
        "categories",
        """
        CREATE TABLE IF NOT EXISTS categories (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            name TEXT NOT NULL,
            parent_id TEXT,
            description TEXT,
            note TEXT
        )
        """,
    ),
    (
        "rules",
        """
        CREATE TABLE IF NOT EXISTS rules (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            pattern TEXT NOT NULL,
            applies_to TEXT NOT NULL,
            category_id TEXT NOT NULL,
            priority INTEGER NOT NULL,
            note TEXT
        )
        """,
    ),
    (
        "transactions",
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            account_id TEXT NOT NULL,
            posted_on DATE NOT NULL,
            amount DECIMAL(18, 2) NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD',
            description_raw TEXT NOT NULL,
            merchant_id TEXT,
            external_id TEXT,
            source TEXT,
            note TEXT,
            UNIQUE (account_id, external_id)
        )
        """,
    ),
    (
        "transaction_splits",
        """
        CREATE TABLE IF NOT EXISTS transaction_splits (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            transaction_id TEXT NOT NULL,
            amount DECIMAL(18, 2) NOT NULL,
            description TEXT,
            quantity DECIMAL(18, 4),
            unit_price DECIMAL(18, 4),
            category_id TEXT,
            categorization_source TEXT,
            categorization_confidence DOUBLE
                CHECK (categorization_confidence BETWEEN 0 AND 1),
            note TEXT
        )
        """,
    ),
    (
        "documents",
        """
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            doc_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            parsed_payload JSON,
            note TEXT
        )
        """,
    ),
    (
        "links",
        """
        CREATE TABLE IF NOT EXISTS links (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            link_type TEXT NOT NULL,
            from_kind TEXT NOT NULL,
            from_id TEXT NOT NULL,
            to_kind TEXT NOT NULL,
            to_id TEXT NOT NULL,
            confidence DOUBLE CHECK (confidence BETWEEN 0 AND 1),
            note TEXT
        )
        """,
    ),
    (
        "budgets",
        """
        CREATE TABLE IF NOT EXISTS budgets (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            name TEXT NOT NULL,
            category_id TEXT NOT NULL,
            period TEXT NOT NULL,
            amount DECIMAL(18, 2) NOT NULL CHECK (amount > 0),
            starts_on DATE NOT NULL,
            note TEXT
        )
        """,
    ),
    (
        "labels",
        """
        CREATE TABLE IF NOT EXISTS labels (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            subject_kind TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            category_id TEXT NOT NULL,
            note TEXT
        )
        """,
    ),
)


def table_names() -> list[str]:
    """Return the names of all core schema tables, in creation order."""
    return [name for name, _ in TABLES]


def create_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create all core tables on the given connection (idempotent).

    Args:
        conn: An open DuckDB connection (file-backed or in-memory).
    """
    for _, ddl in TABLES:
        conn.execute(ddl)
