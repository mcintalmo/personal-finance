"""Tests for personal_finance.embed (fully offline via httpx.MockTransport).

A live-Ollama smoke test also runs, but only when a real server with the
configured model is reachable — see TestLiveOllama.
"""

import json

import duckdb
import httpx
import pytest

from personal_finance.config import get_settings
from personal_finance.ddl import create_schema
from personal_finance.embed import (
    EmbeddingClient,
    compute_missing_embeddings,
    merchant_embedding_id,
)
from personal_finance.exceptions import ExternalServiceError


def client_with_handler(handler) -> EmbeddingClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="http://fake-ollama", transport=transport)
    return EmbeddingClient("http://fake-ollama", "nomic-embed-text", client=http_client)


class TestEmbed:
    def test_sends_model_and_input_returns_embeddings(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]})

        with client_with_handler(handler) as client:
            result = client.embed(["coffee shop", "gas station"])

        assert captured["body"] == {
            "model": "nomic-embed-text",
            "input": ["coffee shop", "gas station"],
        }
        assert result == [[0.1, 0.2], [0.3, 0.4]]

    def test_empty_input_short_circuits_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("embed([]) must not make a request")

        with client_with_handler(handler) as client:
            assert client.embed([]) == []

    def test_http_error_status_raises_external_service_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": 'model "x" not found'})

        with (
            client_with_handler(handler) as client,
            pytest.raises(ExternalServiceError, match="404"),
        ):
            client.embed(["x"])

    def test_connection_failure_raises_external_service_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        with (
            client_with_handler(handler) as client,
            pytest.raises(ExternalServiceError, match="is it running"),
        ):
            client.embed(["x"])

    def test_malformed_response_missing_embeddings_key(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"model": "nomic-embed-text"})

        with (
            client_with_handler(handler) as client,
            pytest.raises(ExternalServiceError, match="Unexpected response shape"),
        ):
            client.embed(["x"])

    def test_malformed_response_wrong_length(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})

        with (
            client_with_handler(handler) as client,
            pytest.raises(ExternalServiceError, match="Unexpected response shape"),
        ):
            client.embed(["x", "y"])

    def test_context_manager_closes_underlying_client(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"embeddings": []})

        transport = httpx.MockTransport(handler)
        http_client = httpx.Client(base_url="http://fake-ollama", transport=transport)
        with EmbeddingClient("http://fake-ollama", "m", client=http_client):
            assert not http_client.is_closed
        assert http_client.is_closed


def _fake_client(calls: list) -> EmbeddingClient:
    """A client whose fake vectors are just [len(text)] — irrelevant to what
    these tests check (caching/dedup behavior), so a trivial mapping is fine.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["input"])
        return httpx.Response(200, json={"embeddings": [[float(len(t))] for t in body["input"]]})

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="http://fake-ollama", transport=transport)
    return EmbeddingClient("http://fake-ollama", "m", client=http_client)


class TestComputeMissingEmbeddings:
    @pytest.fixture
    def conn(self):
        with duckdb.connect(":memory:") as connection:
            create_schema(connection)
            connection.execute("CREATE SCHEMA main_silver")
            connection.execute("CREATE TABLE main_silver.silver_transactions (merchant_name TEXT)")
            yield connection

    def test_embeds_every_distinct_nonnull_merchant(self, conn):
        conn.executemany(
            "INSERT INTO main_silver.silver_transactions VALUES (?)",
            [("ALDI",), ("ALDI",), ("KROGER",), (None,)],
        )
        calls: list = []
        with _fake_client(calls) as client:
            count = compute_missing_embeddings(conn, client, "m")
        assert count == 2  # distinct, non-null merchants
        assert calls == [["ALDI", "KROGER"]]  # one batch call, alphabetical order
        rows = dict(
            conn.execute("SELECT merchant_name, embedding FROM merchant_embeddings").fetchall()
        )
        assert set(rows) == {"ALDI", "KROGER"}

    def test_already_embedded_merchants_are_skipped(self, conn):
        conn.executemany(
            "INSERT INTO main_silver.silver_transactions VALUES (?)", [("ALDI",), ("KROGER",)]
        )
        calls: list = []
        with _fake_client(calls) as client:
            first = compute_missing_embeddings(conn, client, "m")
            second = compute_missing_embeddings(conn, client, "m")
        assert first == 2
        assert second == 0
        assert len(calls) == 1  # no HTTP call at all on the second run

    def test_different_model_gets_its_own_embedding(self, conn):
        conn.execute("INSERT INTO main_silver.silver_transactions VALUES ('ALDI')")
        calls: list = []
        with _fake_client(calls) as client:
            compute_missing_embeddings(conn, client, "model-a")
            count = compute_missing_embeddings(conn, client, "model-b")
        assert count == 1  # not skipped — a different model needs its own vector
        (total,) = conn.execute("SELECT count(*) FROM merchant_embeddings").fetchone()
        assert total == 2

    def test_chunking_splits_into_multiple_requests(self, conn):
        names = [(f"MERCHANT_{i}",) for i in range(5)]
        conn.executemany("INSERT INTO main_silver.silver_transactions VALUES (?)", names)
        calls: list = []
        with _fake_client(calls) as client:
            count = compute_missing_embeddings(conn, client, "m", chunk_size=2)
        assert count == 5
        assert [len(c) for c in calls] == [2, 2, 1]

    def test_ids_are_deterministic_and_stable(self, conn):
        conn.execute("INSERT INTO main_silver.silver_transactions VALUES ('ALDI')")
        with _fake_client([]) as client:
            compute_missing_embeddings(conn, client, "m")
        (id_,) = conn.execute("SELECT id FROM merchant_embeddings").fetchone()
        assert id_ == merchant_embedding_id("ALDI", "m")


class TestLiveOllama:
    """Real smoke test against a live Ollama — self-skips when unavailable."""

    def test_embeds_against_real_ollama(self):
        settings = get_settings().ollama
        try:
            tags = httpx.get(f"{settings.base_url}/api/tags", timeout=2.0).json()
        except httpx.HTTPError:
            pytest.skip(f"no Ollama server reachable at {settings.base_url}")
        names = {m["name"] for m in tags.get("models", [])}
        if not any(n.startswith(settings.embedding_model) for n in names):
            pytest.skip(f"{settings.embedding_model!r} not pulled in local Ollama")

        with EmbeddingClient(settings.base_url, settings.embedding_model) as client:
            vectors = client.embed(["coffee shop", "gas station"])

        assert len(vectors) == 2
        assert len(vectors[0]) > 0
        assert vectors[0] != vectors[1]
