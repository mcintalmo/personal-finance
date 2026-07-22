"""Tests for personal_finance.review (fully offline, in-memory DuckDB)."""

from datetime import date
from decimal import Decimal

import duckdb
import pytest

from personal_finance.ddl import create_schema
from personal_finance.exceptions import NotFoundError
from personal_finance.models import EntityKind
from personal_finance.review import fetch_review_queue, record_label

_CATEGORY_PATHS = {"essentials/groceries": "groceries-id", "non-essentials/dining": "dining-id"}


@pytest.fixture
def conn():
    with duckdb.connect(":memory:") as connection:
        create_schema(connection)
        connection.execute("CREATE SCHEMA main_silver")
        connection.execute(
            """
            CREATE TABLE main_silver.silver_transactions (
                transaction_id TEXT,
                posted_on DATE,
                amount DECIMAL(18, 2),
                merchant_name TEXT,
                description_raw TEXT,
                source TEXT
            )
            """
        )
        connection.execute(
            "CREATE TABLE main_silver.silver_transaction_categories_all (transaction_id TEXT)"
        )
        yield connection


def _insert_txn(conn, transaction_id, posted_on, amount, merchant_name, source="chase_checking"):
    conn.execute(
        "INSERT INTO main_silver.silver_transactions VALUES (?, ?, ?, ?, ?, ?)",
        (transaction_id, posted_on, amount, merchant_name, merchant_name or "", source),
    )


class TestFetchReviewQueue:
    def test_returns_only_uncategorized_transactions(self, conn):
        _insert_txn(conn, "t1", date(2026, 1, 1), Decimal("-10.00"), "ALDI")
        _insert_txn(conn, "t2", date(2026, 1, 2), Decimal("-20.00"), "MYSTERY MERCHANT")
        conn.execute("INSERT INTO main_silver.silver_transaction_categories_all VALUES ('t1')")

        items = fetch_review_queue(conn)

        assert [item.transaction_id for item in items] == ["t2"]

    def test_orders_most_recent_first(self, conn):
        _insert_txn(conn, "t1", date(2026, 1, 1), Decimal("-1.00"), "A")
        _insert_txn(conn, "t2", date(2026, 1, 3), Decimal("-1.00"), "B")
        _insert_txn(conn, "t3", date(2026, 1, 2), Decimal("-1.00"), "C")

        items = fetch_review_queue(conn)

        assert [item.transaction_id for item in items] == ["t2", "t3", "t1"]

    def test_respects_limit(self, conn):
        for i in range(5):
            _insert_txn(conn, f"t{i}", date(2026, 1, 1 + i), Decimal("-1.00"), f"M{i}")

        items = fetch_review_queue(conn, limit=2)

        assert len(items) == 2

    def test_empty_queue_when_everything_categorized(self, conn):
        _insert_txn(conn, "t1", date(2026, 1, 1), Decimal("-1.00"), "ALDI")
        conn.execute("INSERT INTO main_silver.silver_transaction_categories_all VALUES ('t1')")

        assert fetch_review_queue(conn) == []


class TestRecordLabel:
    def test_stores_a_label_for_the_transaction(self, conn):
        _insert_txn(conn, "t1", date(2026, 1, 1), Decimal("-10.00"), "MYSTERY MERCHANT")

        label = record_label(conn, "t1", "essentials/groceries", _CATEGORY_PATHS)

        assert label.subject_kind == EntityKind.TRANSACTION
        assert label.subject_id == "t1"
        assert label.category_id == "groceries-id"
        row = conn.execute(
            "SELECT subject_kind, subject_id, category_id FROM labels WHERE id = $id",
            {"id": label.id},
        ).fetchone()
        assert row == ("transaction", "t1", "groceries-id")

    def test_note_is_stored(self, conn):
        _insert_txn(conn, "t1", date(2026, 1, 1), Decimal("-10.00"), "MYSTERY MERCHANT")

        record_label(conn, "t1", "essentials/groceries", _CATEGORY_PATHS, note="looked it up")

        (note,) = conn.execute("SELECT note FROM labels").fetchone()
        assert note == "looked it up"

    def test_unknown_transaction_raises(self, conn):
        with pytest.raises(NotFoundError, match="No such transaction"):
            record_label(conn, "does-not-exist", "essentials/groceries", _CATEGORY_PATHS)

    def test_unknown_category_path_raises(self, conn):
        _insert_txn(conn, "t1", date(2026, 1, 1), Decimal("-10.00"), "MYSTERY MERCHANT")
        with pytest.raises(NotFoundError, match="Unknown category path"):
            record_label(conn, "t1", "not/a/real/path", _CATEGORY_PATHS)

    def test_relabeling_inserts_a_second_label(self, conn):
        """Both corrections are kept — the dbt model resolves conflicts by
        keeping only the latest (see silver_transaction_categories_human)."""
        _insert_txn(conn, "t1", date(2026, 1, 1), Decimal("-10.00"), "MYSTERY MERCHANT")

        record_label(conn, "t1", "essentials/groceries", _CATEGORY_PATHS)
        record_label(conn, "t1", "non-essentials/dining", _CATEGORY_PATHS)

        (count,) = conn.execute("SELECT count(*) FROM labels WHERE subject_id = 't1'").fetchone()
        assert count == 2
