"""Tests for personal_finance.ddl against an in-memory DuckDB."""

import json
from datetime import date
from decimal import Decimal

import duckdb
import pytest

from personal_finance.ddl import create_schema, table_names
from personal_finance.models import (
    Account,
    AccountType,
    Budget,
    BudgetPeriod,
    Category,
    Document,
    DocumentType,
    EntityKind,
    Label,
    Link,
    LinkType,
    Merchant,
    Rule,
    Transaction,
    TransactionSplit,
)

EXPECTED_TABLES = {
    "accounts",
    "merchants",
    "categories",
    "rules",
    "transactions",
    "transaction_splits",
    "documents",
    "links",
    "budgets",
    "labels",
}


@pytest.fixture
def conn():
    with duckdb.connect(":memory:") as connection:
        create_schema(connection)
        yield connection


def insert(conn, table, model, **field_overrides):
    """Insert a Pydantic model instance into `table` via named placeholders."""
    row = {**model.model_dump(), **field_overrides}
    columns = ", ".join(row)
    placeholders = ", ".join(f"${name}" for name in row)
    conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", row)


def existing_tables(conn):
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()
    return {name for (name,) in rows}


class TestCreateSchema:
    def test_creates_all_core_tables(self, conn):
        assert existing_tables(conn) == EXPECTED_TABLES

    def test_is_idempotent(self, conn):
        create_schema(conn)  # second run must not raise
        assert existing_tables(conn) == EXPECTED_TABLES

    def test_table_names_matches_ddl(self):
        assert set(table_names()) == EXPECTED_TABLES


class TestRoundTrip:
    """Every model instance can be persisted into its table."""

    def test_full_entity_graph_inserts(self, conn):
        account = Account(name="Checking", account_type=AccountType.CHECKING)
        merchant = Merchant(canonical_name="Trader Joe's", aliases=["TRADER JOES #123"])
        root = Category(name="essentials")
        groceries = Category(name="groceries", parent_id=root.id)
        rule = Rule(
            pattern="(?i)trader joe",
            applies_to="merchant_name",
            category_id=groceries.id,
            priority=0,
        )
        txn = Transaction(
            account_id=account.id,
            posted_on=date(2026, 7, 1),
            amount=Decimal("-42.50"),
            description_raw="TRADER JOES #123",
            merchant_id=merchant.id,
            external_id="FITID-001",
            source="chase_checking.ofx",
        )
        split = TransactionSplit(
            transaction_id=txn.id,
            amount=Decimal("-3.99"),
            description="HONEYCRISP APPLES",
            category_id=groceries.id,
            categorization_source="embedding",
            categorization_confidence=0.92,
        )
        doc = Document(doc_type=DocumentType.RECEIPT, file_path="receipts/img001.jpg")
        link = Link(
            link_type=LinkType.RECEIPT_MATCH,
            from_kind=EntityKind.DOCUMENT,
            from_id=doc.id,
            to_kind=EntityKind.TRANSACTION,
            to_id=txn.id,
            confidence=0.97,
        )
        budget = Budget(
            name="Groceries",
            category_id=groceries.id,
            period=BudgetPeriod.MONTHLY,
            amount=Decimal("500.00"),
            starts_on=date(2026, 1, 1),
        )
        label = Label(subject_kind=EntityKind.SPLIT, subject_id=split.id, category_id=root.id)

        insert(conn, "accounts", account)
        insert(conn, "merchants", merchant)
        insert(conn, "categories", root)
        insert(conn, "categories", groceries)
        insert(conn, "rules", rule)
        insert(conn, "transactions", txn)
        insert(conn, "transaction_splits", split)
        insert(conn, "documents", doc, parsed_payload=json.dumps({"total": "42.50"}))
        insert(conn, "links", link)
        insert(conn, "budgets", budget)
        insert(conn, "labels", label)

        for table in EXPECTED_TABLES:
            count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            assert count >= 1, f"no rows in {table}"

    def test_note_survives_round_trip(self, conn):
        account = Account(
            name="Checking",
            account_type=AccountType.CHECKING,
            note="joint account with partner",
        )
        insert(conn, "accounts", account)
        (note,) = conn.execute(
            "SELECT note FROM accounts WHERE id = $id", {"id": account.id}
        ).fetchone()
        assert note == "joint account with partner"

    def test_amount_precision_survives_round_trip(self, conn):
        account = Account(name="CC", account_type=AccountType.CREDIT_CARD)
        txn = Transaction(
            account_id=account.id,
            posted_on=date(2026, 7, 1),
            amount=Decimal("-1234567.89"),
            description_raw="BIG PURCHASE",
        )
        insert(conn, "accounts", account)
        insert(conn, "transactions", txn)
        (amount,) = conn.execute(
            "SELECT amount FROM transactions WHERE id = $id", {"id": txn.id}
        ).fetchone()
        assert amount == Decimal("-1234567.89")


class TestConstraints:
    # NB: referential integrity (e.g. transaction -> account) is intentionally not
    # a declared FK — see the ddl module docstring. It is checked by dbt
    # relationship tests, not here.

    def test_duplicate_external_id_per_account_rejected(self, conn):
        account = Account(name="Checking", account_type=AccountType.CHECKING)
        insert(conn, "accounts", account)
        first = Transaction(
            account_id=account.id,
            posted_on=date(2026, 7, 1),
            amount=Decimal("-1.00"),
            description_raw="A",
            external_id="FITID-1",
        )
        duplicate = Transaction(
            account_id=account.id,
            posted_on=date(2026, 7, 2),
            amount=Decimal("-2.00"),
            description_raw="B",
            external_id="FITID-1",
        )
        insert(conn, "transactions", first)
        with pytest.raises(duckdb.ConstraintException):
            insert(conn, "transactions", duplicate)

    def test_split_confidence_check_constraint(self, conn):
        account = Account(name="Checking", account_type=AccountType.CHECKING)
        txn = Transaction(
            account_id=account.id,
            posted_on=date(2026, 7, 1),
            amount=Decimal("-1.00"),
            description_raw="A",
        )
        insert(conn, "accounts", account)
        insert(conn, "transactions", txn)
        split = TransactionSplit(transaction_id=txn.id, amount=Decimal("-1.00"))
        with pytest.raises(duckdb.ConstraintException):
            insert(conn, "transaction_splits", split, categorization_confidence=1.5)

    def test_budget_amount_must_be_positive(self, conn):
        category = Category(name="groceries")
        insert(conn, "categories", category)
        budget = Budget(
            name="Groceries",
            category_id=category.id,
            period=BudgetPeriod.MONTHLY,
            amount=Decimal("100.00"),
            starts_on=date(2026, 1, 1),
        )
        with pytest.raises(duckdb.ConstraintException):
            insert(conn, "budgets", budget, amount=Decimal("-100.00"))
