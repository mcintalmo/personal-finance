"""Tests for personal_finance.llm_categorize (fully offline via httpx.MockTransport).

A live-Ollama smoke test also runs, but only when a real server with the
configured chat model is reachable — see TestLiveOllama.
"""

import json

import duckdb
import httpx
import pytest

from personal_finance.config import get_settings
from personal_finance.ddl import create_schema
from personal_finance.exceptions import ExternalServiceError
from personal_finance.llm_categorize import (
    LlmCategorizeClient,
    compute_missing_llm_categories,
    fetch_category_paths,
    merchant_llm_category_id,
)

_PATHS = ["essentials/groceries", "non-essentials/dining"]


def _chat_response(category: str, confidence: float) -> dict:
    return {
        "message": {
            "role": "assistant",
            "content": json.dumps({"category": category, "confidence": confidence}),
        }
    }


def client_with_handler(handler) -> LlmCategorizeClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="http://fake-ollama", transport=transport)
    return LlmCategorizeClient("http://fake-ollama", "qwen2.5:3b", client=http_client)


class TestClassify:
    def test_sends_categories_in_prompt_and_parses_result(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_chat_response("essentials/groceries", 0.75))

        with client_with_handler(handler) as client:
            result = client.classify("KROGER", _PATHS)

        assert captured["body"]["model"] == "qwen2.5:3b"
        prompt = captured["body"]["messages"][0]["content"]
        assert "KROGER" in prompt
        assert "essentials/groceries" in prompt
        assert "non-essentials/dining" in prompt
        assert result == ("essentials/groceries", 0.75)

    def test_confidence_is_clamped_to_zero_one(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_chat_response("essentials/groceries", 1.5))

        with client_with_handler(handler) as client:
            result = client.classify("KROGER", _PATHS)
        assert result == ("essentials/groceries", 1.0)

    def test_category_outside_the_given_list_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_chat_response("made-up/category", 0.9))

        with client_with_handler(handler) as client:
            assert client.classify("KROGER", _PATHS) is None

    def test_malformed_json_content_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"message": {"role": "assistant", "content": "not json"}}
            )

        with client_with_handler(handler) as client:
            assert client.classify("KROGER", _PATHS) is None

    def test_missing_confidence_key_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({"category": "essentials/groceries"}),
                    }
                },
            )

        with client_with_handler(handler) as client:
            assert client.classify("KROGER", _PATHS) is None

    def test_http_error_status_raises_external_service_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": 'model "x" not found'})

        with (
            client_with_handler(handler) as client,
            pytest.raises(ExternalServiceError, match="404"),
        ):
            client.classify("KROGER", _PATHS)

    def test_connection_failure_raises_external_service_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        with (
            client_with_handler(handler) as client,
            pytest.raises(ExternalServiceError, match="is it running"),
        ):
            client.classify("KROGER", _PATHS)

    def test_context_manager_closes_underlying_client(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_chat_response("essentials/groceries", 0.5))

        transport = httpx.MockTransport(handler)
        http_client = httpx.Client(base_url="http://fake-ollama", transport=transport)
        with LlmCategorizeClient("http://fake-ollama", "m", client=http_client):
            assert not http_client.is_closed
        assert http_client.is_closed


def _fake_client(responses: dict[str, tuple[str, float] | None]) -> LlmCategorizeClient:
    """A client returning a canned (category, confidence) per merchant name."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"]
        merchant = next(name for name in responses if name in prompt)
        result = responses[merchant]
        if result is None:
            return httpx.Response(200, json=_chat_response("made-up/category", 0.5))
        return httpx.Response(200, json=_chat_response(*result))

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="http://fake-ollama", transport=transport)
    return LlmCategorizeClient("http://fake-ollama", "m", client=http_client)


class TestComputeMissingLlmCategories:
    @pytest.fixture
    def conn(self):
        with duckdb.connect(":memory:") as connection:
            create_schema(connection)
            connection.execute(
                "INSERT INTO categories (id, created_at, name, parent_id) "
                "VALUES ('essentials', now(), 'essentials', NULL), "
                "('groceries', now(), 'groceries', 'essentials'), "
                "('dining', now(), 'dining', NULL)"
            )
            connection.execute("CREATE SCHEMA main_silver")
            connection.execute(
                "CREATE TABLE main_silver.silver_transactions "
                "(transaction_id TEXT, merchant_name TEXT)"
            )
            connection.execute(
                "CREATE TABLE main_silver.silver_transaction_categories (transaction_id TEXT)"
            )
            connection.execute(
                "CREATE TABLE main_silver.silver_transaction_categories_embedding "
                "(transaction_id TEXT)"
            )
            yield connection

    def test_category_paths_are_slash_joined_from_the_categories_table(self, conn):
        paths = fetch_category_paths(conn)
        assert paths == {
            "essentials": "essentials",
            "essentials/groceries": "groceries",
            "dining": "dining",
        }

    def test_classifies_only_uncategorized_merchants(self, conn):
        conn.executemany(
            "INSERT INTO main_silver.silver_transactions VALUES (?, ?)",
            [("t1", "KROGER"), ("t2", "CHIPOTLE"), ("t3", "ALDI")],
        )
        conn.execute("INSERT INTO main_silver.silver_transaction_categories VALUES ('t3')")
        client = _fake_client(
            {"CHIPOTLE": ("dining", 0.8), "KROGER": ("essentials/groceries", 0.7)}
        )
        with client:
            count = compute_missing_llm_categories(conn, client, "m")
        assert count == 2  # ALDI excluded — already categorized by stage 1
        rows = dict(
            conn.execute(
                "SELECT merchant_name, category_id FROM merchant_llm_categories"
            ).fetchall()
        )
        assert rows == {"KROGER": "groceries", "CHIPOTLE": "dining"}

    def test_excludes_merchants_stage2_already_matched(self, conn):
        conn.execute("INSERT INTO main_silver.silver_transactions VALUES ('t1', 'STARBUCKS')")
        conn.execute(
            "INSERT INTO main_silver.silver_transaction_categories_embedding VALUES ('t1')"
        )
        client = _fake_client({})
        with client:
            count = compute_missing_llm_categories(conn, client, "m")
        assert count == 0

    def test_unconfident_classification_is_not_cached(self, conn):
        conn.execute("INSERT INTO main_silver.silver_transactions VALUES ('t1', 'CHIPOTLE')")
        client = _fake_client({"CHIPOTLE": None})
        with client:
            count = compute_missing_llm_categories(conn, client, "m")
        assert count == 0
        (total,) = conn.execute("SELECT count(*) FROM merchant_llm_categories").fetchone()
        assert total == 0

    def test_already_classified_merchants_are_skipped(self, conn):
        conn.execute("INSERT INTO main_silver.silver_transactions VALUES ('t1', 'CHIPOTLE')")
        client = _fake_client({"CHIPOTLE": ("dining", 0.8)})
        with client:
            first = compute_missing_llm_categories(conn, client, "m")
            second = compute_missing_llm_categories(conn, client, "m")
        assert first == 1
        assert second == 0

    def test_ids_are_deterministic_and_stable(self, conn):
        conn.execute("INSERT INTO main_silver.silver_transactions VALUES ('t1', 'CHIPOTLE')")
        client = _fake_client({"CHIPOTLE": ("dining", 0.8)})
        with client:
            compute_missing_llm_categories(conn, client, "m")
        (id_,) = conn.execute("SELECT id FROM merchant_llm_categories").fetchone()
        assert id_ == merchant_llm_category_id("CHIPOTLE", "m")


class TestLiveOllama:
    """Real smoke test against a live Ollama — self-skips when unavailable."""

    def test_classifies_against_real_ollama(self):
        settings = get_settings().ollama
        try:
            tags = httpx.get(f"{settings.base_url}/api/tags", timeout=2.0).json()
        except httpx.HTTPError:
            pytest.skip(f"no Ollama server reachable at {settings.base_url}")
        names = {m["name"] for m in tags.get("models", [])}
        if not any(n.startswith(settings.chat_model) for n in names):
            pytest.skip(f"{settings.chat_model!r} not pulled in local Ollama")

        # A real small local model may not respect the given option list — that's
        # classify() declining to guess (returns None), not a plumbing failure, so
        # this only asserts the round trip completes and any result is well-formed.
        with LlmCategorizeClient(settings.base_url, settings.chat_model) as client:
            result = client.classify("STARBUCKS", _PATHS)

        if result is not None:
            category, confidence = result
            assert category in _PATHS
            assert 0.0 <= confidence <= 1.0
