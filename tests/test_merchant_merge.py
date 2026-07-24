"""Tests for personal_finance.merchant_merge (fully offline, in-memory DuckDB)."""

import duckdb
import pytest

from personal_finance.ddl import create_schema
from personal_finance.exceptions import NotFoundError, ValidationError
from personal_finance.merchant_merge import fetch_merge_candidates, record_merge_decision
from personal_finance.models import MergeStatus

_MODEL = "nomic-embed-text"


@pytest.fixture
def conn():
    with duckdb.connect(":memory:") as connection:
        create_schema(connection)
        connection.execute("CREATE SCHEMA main_silver")
        connection.execute("CREATE TABLE main_silver.silver_transactions (merchant_name TEXT)")
        yield connection


def _insert_txns(conn, merchant_name, count):
    for _ in range(count):
        conn.execute("INSERT INTO main_silver.silver_transactions VALUES (?)", (merchant_name,))


def _insert_embedding(conn, merchant_name, vector, model=_MODEL):
    conn.execute(
        "INSERT INTO merchant_embeddings (id, created_at, merchant_name, model, embedding) "
        "VALUES ($id, now(), $merchant_name, $model, $embedding)",
        {
            "id": f"{merchant_name}-{model}",
            "merchant_name": merchant_name,
            "model": model,
            "embedding": vector,
        },
    )


class TestFetchMergeCandidates:
    def test_similar_pair_above_threshold_is_a_candidate(self, conn):
        _insert_txns(conn, "TARGET T-1234", 1)
        _insert_txns(conn, "TARGET 0099", 3)
        _insert_embedding(conn, "TARGET T-1234", [1.0, 0.0])
        _insert_embedding(conn, "TARGET 0099", [1.0, 0.001])

        (candidate,) = fetch_merge_candidates(conn, model=_MODEL, threshold=0.90)

        assert candidate.merchant_name == "TARGET T-1234"
        assert candidate.canonical_name == "TARGET 0099"
        assert candidate.similarity > 0.99

    def test_canonical_is_the_merchant_with_more_transaction_history(self, conn):
        _insert_txns(conn, "RARE SPELLING", 1)
        _insert_txns(conn, "COMMON SPELLING", 50)
        _insert_embedding(conn, "RARE SPELLING", [1.0, 0.0])
        _insert_embedding(conn, "COMMON SPELLING", [1.0, 0.0001])

        (candidate,) = fetch_merge_candidates(conn, model=_MODEL, threshold=0.90)

        assert candidate.merchant_name == "RARE SPELLING"
        assert candidate.canonical_name == "COMMON SPELLING"

    def test_dissimilar_pair_is_not_a_candidate(self, conn):
        _insert_txns(conn, "ALDI", 1)
        _insert_txns(conn, "SHELL GAS", 1)
        _insert_embedding(conn, "ALDI", [1.0, 0.0])
        _insert_embedding(conn, "SHELL GAS", [0.0, 1.0])

        assert fetch_merge_candidates(conn, model=_MODEL, threshold=0.90) == []

    def test_already_decided_merchant_is_excluded(self, conn):
        _insert_txns(conn, "TARGET T-1234", 1)
        _insert_txns(conn, "TARGET 0099", 3)
        _insert_embedding(conn, "TARGET T-1234", [1.0, 0.0])
        _insert_embedding(conn, "TARGET 0099", [1.0, 0.001])
        record_merge_decision(
            conn, "TARGET T-1234", "TARGET 0099", MergeStatus.REJECTED, similarity=0.99
        )

        assert fetch_merge_candidates(conn, model=_MODEL, threshold=0.90) == []

    def test_different_model_has_no_embeddings_to_compare(self, conn):
        _insert_txns(conn, "TARGET T-1234", 1)
        _insert_txns(conn, "TARGET 0099", 3)
        _insert_embedding(conn, "TARGET T-1234", [1.0, 0.0], model="other-model")
        _insert_embedding(conn, "TARGET 0099", [1.0, 0.001], model="other-model")

        assert fetch_merge_candidates(conn, model=_MODEL, threshold=0.90) == []

    def test_respects_limit_and_orders_by_similarity_desc(self, conn):
        # One-hot base vectors keep each (A_i, B_i) pair isolated — a small
        # perturbation on a different axis varies similarity per pair without
        # creating spurious cross-pair matches (orthogonal base vectors have
        # ~zero cosine similarity with each other).
        for i in range(3):
            name_a, name_b = f"A{i}", f"B{i}"
            _insert_txns(conn, name_a, 1)
            _insert_txns(conn, name_b, 3)
            a_vec = [0.0, 0.0, 0.0]
            a_vec[i] = 1.0
            b_vec = list(a_vec)
            b_vec[(i + 1) % 3] = 0.01 * (i + 1)
            _insert_embedding(conn, name_a, a_vec)
            _insert_embedding(conn, name_b, b_vec)

        candidates = fetch_merge_candidates(conn, model=_MODEL, threshold=0.90, limit=2)

        assert len(candidates) == 2
        assert candidates[0].similarity >= candidates[1].similarity


class TestRecordMergeDecision:
    def test_stores_an_accepted_merge(self, conn):
        _insert_txns(conn, "TARGET T-1234", 1)
        _insert_txns(conn, "TARGET 0099", 3)

        merge = record_merge_decision(
            conn, "TARGET T-1234", "TARGET 0099", MergeStatus.ACCEPTED, similarity=0.97
        )

        assert merge.status == MergeStatus.ACCEPTED
        row = conn.execute(
            "SELECT merchant_name, canonical_name, similarity, status FROM merchant_merges "
            "WHERE id = $id",
            {"id": merge.id},
        ).fetchone()
        assert row == ("TARGET T-1234", "TARGET 0099", 0.97, "accepted")

    def test_stores_a_rejected_merge(self, conn):
        _insert_txns(conn, "TARGET T-1234", 1)
        _insert_txns(conn, "TARGET 0099", 3)

        merge = record_merge_decision(conn, "TARGET T-1234", "TARGET 0099", MergeStatus.REJECTED)

        assert merge.status == MergeStatus.REJECTED

    def test_self_merge_raises(self, conn):
        _insert_txns(conn, "TARGET T-1234", 1)
        with pytest.raises(ValidationError, match="itself"):
            record_merge_decision(conn, "TARGET T-1234", "TARGET T-1234", MergeStatus.ACCEPTED)

    def test_unknown_merchant_name_raises(self, conn):
        _insert_txns(conn, "TARGET 0099", 1)
        with pytest.raises(NotFoundError, match="No such merchant"):
            record_merge_decision(conn, "DOES NOT EXIST", "TARGET 0099", MergeStatus.ACCEPTED)

    def test_unknown_canonical_name_raises(self, conn):
        _insert_txns(conn, "TARGET T-1234", 1)
        with pytest.raises(NotFoundError, match="No such merchant"):
            record_merge_decision(conn, "TARGET T-1234", "DOES NOT EXIST", MergeStatus.ACCEPTED)

    def test_note_is_stored(self, conn):
        _insert_txns(conn, "TARGET T-1234", 1)
        _insert_txns(conn, "TARGET 0099", 3)

        merge = record_merge_decision(
            conn, "TARGET T-1234", "TARGET 0099", MergeStatus.ACCEPTED, note="same store, new sign"
        )

        assert merge.note == "same store, new sign"

    def test_redeciding_inserts_a_second_row(self, conn):
        """Both decisions are kept — the dbt model resolves conflicts by
        keeping only the latest (see silver_transactions.sql's `merges` CTE)."""
        _insert_txns(conn, "TARGET T-1234", 1)
        _insert_txns(conn, "TARGET 0099", 3)

        record_merge_decision(conn, "TARGET T-1234", "TARGET 0099", MergeStatus.REJECTED)
        record_merge_decision(conn, "TARGET T-1234", "TARGET 0099", MergeStatus.ACCEPTED)

        (count,) = conn.execute(
            "SELECT count(*) FROM merchant_merges WHERE merchant_name = 'TARGET T-1234'"
        ).fetchone()
        assert count == 2
