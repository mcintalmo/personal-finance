"""Tests for personal_finance.seed."""

import duckdb
import pytest

from personal_finance.ddl import create_schema
from personal_finance.seed import seed_categories
from personal_finance.user_config import TaxonomyNode, category_id_for_path


@pytest.fixture
def conn():
    with duckdb.connect(":memory:") as connection:
        create_schema(connection)
        yield connection


TAXONOMY = [
    TaxonomyNode(
        name="essentials",
        description="Necessary spending",
        children=[
            TaxonomyNode(name="groceries", children=[TaxonomyNode(name="apples")]),
        ],
    ),
    TaxonomyNode(name="income"),
]


def count_categories(conn):
    return conn.execute("SELECT count(*) FROM categories").fetchone()[0]


class TestSeedCategories:
    def test_seeds_all_categories_with_parent_links(self, conn):
        seeded = seed_categories(conn, TAXONOMY)
        assert count_categories(conn) == 4

        rows = dict(conn.execute("SELECT id, parent_id FROM categories").fetchall())
        apples_id = category_id_for_path("essentials/groceries/apples")
        groceries_id = category_id_for_path("essentials/groceries")
        assert rows[apples_id] == groceries_id
        assert rows[category_id_for_path("essentials")] is None
        assert set(seeded) == {
            "essentials",
            "essentials/groceries",
            "essentials/groceries/apples",
            "income",
        }

    def test_reseeding_is_idempotent(self, conn):
        seed_categories(conn, TAXONOMY)
        seed_categories(conn, TAXONOMY)
        assert count_categories(conn) == 4

    def test_ids_are_stable_across_runs(self, conn):
        first = seed_categories(conn, TAXONOMY)
        second = seed_categories(conn, TAXONOMY)
        assert {p: c.id for p, c in first.items()} == {p: c.id for p, c in second.items()}

    def test_description_updates_in_place(self, conn):
        seed_categories(conn, TAXONOMY)
        updated = [
            TaxonomyNode(name="essentials", description="Must-have spending"),
            TaxonomyNode(name="income"),
        ]
        seed_categories(conn, updated)
        (description,) = conn.execute(
            "SELECT description FROM categories WHERE id = $id",
            {"id": category_id_for_path("essentials")},
        ).fetchone()
        assert description == "Must-have spending"

    def test_new_category_added_on_reseed(self, conn):
        seed_categories(conn, TAXONOMY)
        grown = [*TAXONOMY, TaxonomyNode(name="transfers")]
        seed_categories(conn, grown)
        assert count_categories(conn) == 5

    def test_removed_category_is_not_deleted(self, conn):
        seed_categories(conn, TAXONOMY)
        seed_categories(conn, [TaxonomyNode(name="income")])
        assert count_categories(conn) == 4

    def test_user_note_survives_reseed(self, conn):
        seed_categories(conn, TAXONOMY)
        conn.execute(
            "UPDATE categories SET note = 'my note' WHERE id = $id",
            {"id": category_id_for_path("income")},
        )
        seed_categories(conn, TAXONOMY)
        (note,) = conn.execute(
            "SELECT note FROM categories WHERE id = $id",
            {"id": category_id_for_path("income")},
        ).fetchone()
        assert note == "my note"
