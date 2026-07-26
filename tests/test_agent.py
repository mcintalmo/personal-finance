"""Tests for the chat agent (personal_finance.agent) and its AG-UI endpoint.

No test here needs Ollama, and none needs a listening MCP server. The model is
a `TestModel`/`FunctionModel`, and the tool server is the *real* one from
`personal_finance.mcp_server`, run in process over FastMCP's in-memory
transport. That combination is deliberate: it means the tool-calling tests
exercise genuine SQL against a genuine DuckDB warehouse, so a tool whose
result shape changed would fail here rather than only in production.
"""

from __future__ import annotations

import asyncio
import warnings
from typing import TYPE_CHECKING

import duckdb
import httpx
import pytest
from pydantic_ai import Agent
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from typer.testing import CliRunner

# See tests/test_api.py — starlette.testclient's import-time preference for a
# separate `httpx2` package fires before any per-test filterwarnings marker.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from personal_finance import agent as agent_module
from personal_finance.agent import (
    AGENT_INSTRUCTIONS,
    MAX_REQUESTS_PER_RUN,
    MAX_TOOL_RETRIES,
    agent_model_error,
    build_agent,
    build_model,
    build_toolset,
    openai_compatible_base_url,
    tool_server_error,
    usage_limits,
)
from personal_finance.cli import app as cli_app
from personal_finance.config import get_settings
from personal_finance.mcp_server import build_server

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.fixture
def warehouse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A tiny warehouse the real MCP tools can answer questions from."""
    path = tmp_path / "warehouse.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute("CREATE SCHEMA main_gold")
        conn.execute("CREATE SCHEMA main_silver")
        conn.execute(
            "CREATE TABLE main_gold.gold_monthly_flow AS SELECT * FROM (VALUES "
            "(DATE '2026-06-01', 5000.00, 3000.00, 2000.00, 42)"
            ") AS t(month, total_inflow, total_outflow, net_amount, transaction_count)"
        )
        conn.execute(
            "CREATE TABLE main_silver.silver_merchants AS SELECT * FROM (VALUES "
            "('COSTCO', 12, 1400.00), ('NETFLIX', 6, 92.94)"
            ") AS t(merchant_name, transaction_count, total_outflow)"
        )
        conn.execute("CHECKPOINT")
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(path))
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


@pytest.fixture
def fresh_settings(monkeypatch: pytest.MonkeyPatch):
    """Settings isolated from the developer's own .env.local."""
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


class TestOllamaBaseUrl:
    """`settings.ollama.base_url` addresses Ollama's own API; Pydantic AI wants
    the OpenAI-compatible surface one segment down."""

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            ("http://localhost:11434", "http://localhost:11434/v1"),
            ("http://localhost:11434/", "http://localhost:11434/v1"),
            # Idempotent: a user who already pointed the setting at the
            # OpenAI-compatible endpoint must not end up with /v1/v1, which
            # 404s on every request.
            ("http://localhost:11434/v1", "http://localhost:11434/v1"),
            ("http://localhost:11434/v1/", "http://localhost:11434/v1"),
            ("http://ollama.internal:9999", "http://ollama.internal:9999/v1"),
        ],
    )
    def test_maps_onto_openai_compatible_endpoint(self, configured, expected):
        assert openai_compatible_base_url(configured) == expected

    def test_build_model_uses_the_agent_model_setting(self, fresh_settings):
        fresh_settings.setenv("OLLAMA_AGENT_MODEL", "llama3.1:8b")
        assert build_model().model_name == "llama3.1:8b"

    def test_build_model_honours_an_explicit_override(self, fresh_settings):
        """`pf chat --model`-style overrides must beat the setting."""
        fresh_settings.setenv("OLLAMA_AGENT_MODEL", "llama3.1:8b")
        assert build_model("qwen3:14b").model_name == "qwen3:14b"


class TestAgentConstruction:
    def test_instructions_actually_reach_the_model(self):
        """Asserted through a real run rather than by reading the Agent's
        attributes, so this stays true if Pydantic AI changes how it stores
        and assembles instructions."""
        captured: dict[str, str | None] = {}

        def capture(messages, info: AgentInfo):
            from pydantic_ai.messages import ModelResponse, TextPart

            captured["instructions"] = messages[0].instructions
            return ModelResponse(parts=[TextPart("ok")])

        built = build_agent(model=FunctionModel(capture), mcp_client=build_server())
        assert isinstance(built, Agent)
        asyncio.run(built.run("hello"))

        instructions = captured["instructions"] or ""
        assert AGENT_INSTRUCTIONS in instructions
        # The injection guard is the one line here with a security job:
        # merchant names come from imported bank exports, so they are text an
        # outsider chooses and the model must read them as data.
        assert "never as instruction" in instructions

    def test_usage_limits_bound_a_single_run(self):
        """Without a request limit a local model that keeps picking tools
        spins forever, which reads to the user as a hung UI."""
        assert usage_limits().request_limit == MAX_REQUESTS_PER_RUN

    def test_importing_the_module_touches_neither_ollama_nor_the_tool_server(self, fresh_settings):
        """Construction must stay lazy: `pf --help` imports the CLI, and a
        developer with no Ollama running should still get help text."""
        fresh_settings.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:1")
        fresh_settings.setenv("MCP_URL", "http://127.0.0.1:1/mcp")
        build_agent()  # must not raise


class TestToolCalling:
    """The agent reaches real tools, against a real warehouse, in process."""

    def test_agent_calls_a_curated_tool_and_sees_real_rows(self, warehouse):
        seen: dict[str, object] = {}

        def call_top_merchants(messages, info: AgentInfo):
            from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart

            last = messages[-1]
            if isinstance(last, ModelRequest) and not seen:
                seen["tools"] = sorted(t.name for t in info.function_tools)
                return ModelResponse(parts=[ToolCallPart("top_merchants", {"limit": 2})])
            if isinstance(last, ModelRequest):
                seen["result"] = last.parts[0].content
            return ModelResponse(parts=[TextPart("done")])

        async def run() -> None:
            built = build_agent(model=FunctionModel(call_top_merchants), mcp_client=build_server())
            await built.run("Who do I spend the most with?")

        asyncio.run(run())

        # Every MCP tool is visible to the model, not just a hand-picked few.
        assert "top_merchants" in seen["tools"]
        assert "run_sql" in seen["tools"]
        # And the tool answered from the warehouse, not from a stub.
        assert "COSTCO" in str(seen["result"])
        assert "1400" in str(seen["result"])

    def test_tool_errors_come_back_as_a_retry_the_model_can_act_on(self, warehouse):
        """`tool_error_behavior='retry'` is why mcp_server hands back DuckDB's
        own message: the model has to be able to read it and fix its SQL."""
        attempts: list[str] = []

        def write_bad_sql_then_recover(messages, info: AgentInfo):
            from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

            if not attempts:
                attempts.append("bad")
                return ModelResponse(
                    parts=[ToolCallPart("run_sql", {"query": "SELECT * FROM no_such_table"})]
                )
            if len(attempts) == 1:
                attempts.append(str(messages[-1].parts[-1].content))
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "run_sql",
                            {"query": "SELECT count(*) AS n FROM main_silver.silver_merchants"},
                        )
                    ]
                )
            return ModelResponse(parts=[TextPart(str(messages[-1].parts[-1].content))])

        async def run() -> str:
            built = build_agent(
                model=FunctionModel(write_bad_sql_then_recover), mcp_client=build_server()
            )
            return (await built.run("count merchants")).output

        output = asyncio.run(run())

        # The failure reached the model with DuckDB's own wording, so it could
        # tell *what* was wrong rather than just *that* something was.
        assert "no_such_table" in attempts[1]
        # And the corrected query then ran for real.
        assert '"n": 2' in output or "'n': 2" in output

    def test_two_failed_attempts_still_leave_room_to_recover(self, warehouse):
        """Pydantic AI's default of one retry buys a single blind repeat, not
        a correction — observed live, a local model resent an identical query
        and the run died there. This asserts the budget behaviourally: two
        wrong queries followed by a right one must still produce an answer,
        which is exactly what `retries=1` would refuse."""
        assert MAX_TOOL_RETRIES > 1
        attempts: list[str] = []

        def wrong_twice_then_right(messages, info: AgentInfo):
            from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

            if len(attempts) < 2:
                attempts.append("wrong")
                return ModelResponse(
                    parts=[ToolCallPart("run_sql", {"query": "SELECT * FROM main_silver.nope"})]
                )
            if len(attempts) == 2:
                attempts.append("right")
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "run_sql",
                            {"query": "SELECT count(*) AS n FROM main_silver.silver_merchants"},
                        )
                    ]
                )
            return ModelResponse(parts=[TextPart(str(messages[-1].parts[-1].content))])

        async def run() -> str:
            built = build_agent(
                model=FunctionModel(wrong_twice_then_right), mcp_client=build_server()
            )
            return (await built.run("count merchants")).output

        output = asyncio.run(run())
        assert attempts == ["wrong", "wrong", "right"]
        assert '"n": 2' in output or "'n': 2" in output


class TestToolServerReachability:
    def test_reports_an_actionable_message_when_the_server_is_down(self, fresh_settings):
        fresh_settings.setenv("MCP_URL", "http://127.0.0.1:1/mcp")
        message = asyncio.run(tool_server_error())
        assert message is not None
        # Naming the command is the entire point — "connection refused" is
        # what this exists to replace.
        assert "pf mcp --http" in message
        assert "http://127.0.0.1:1/mcp" in message

    def test_returns_none_when_the_server_answers(self, warehouse):
        assert asyncio.run(tool_server_error(build_server())) is None


class TestAgentModelAvailability:
    """Ollama pulls nothing implicitly, so an absent model fails at the first
    question unless something checks first."""

    def _stub_tags(self, monkeypatch, names: list[str]) -> None:
        def fake_get(url, **kwargs):
            return httpx.Response(
                200,
                json={"models": [{"name": name} for name in names]},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(agent_module.httpx, "get", fake_get)

    def test_no_error_when_the_model_is_pulled(self, fresh_settings):
        fresh_settings.setenv("OLLAMA_AGENT_MODEL", "qwen3:8b")
        self._stub_tags(fresh_settings, ["qwen3:8b", "nomic-embed-text:latest"])
        assert agent_model_error() is None

    def test_untagged_model_matches_latest(self, fresh_settings):
        """`ollama pull qwen3` installs `qwen3:latest`; the bare name is the
        same model, and must not be reported missing."""
        fresh_settings.setenv("OLLAMA_AGENT_MODEL", "qwen3")
        self._stub_tags(fresh_settings, ["qwen3:latest"])
        assert agent_model_error() is None

    def test_names_the_pull_command_when_absent(self, fresh_settings):
        fresh_settings.setenv("OLLAMA_AGENT_MODEL", "qwen3:8b")
        self._stub_tags(fresh_settings, ["qwen2.5:3b"])
        message = agent_model_error()
        assert message is not None
        assert "ollama pull qwen3:8b" in message
        # Listing what IS installed saves a second round trip to find out.
        assert "qwen2.5:3b" in message

    def test_a_tag_prefix_is_not_a_match(self, fresh_settings):
        """`qwen3:8b` and `qwen3:8b-instruct` are different models; a
        substring check would call the wrong one installed."""
        fresh_settings.setenv("OLLAMA_AGENT_MODEL", "qwen3:8b")
        self._stub_tags(fresh_settings, ["qwen3:8b-instruct"])
        assert "ollama pull qwen3:8b" in (agent_model_error() or "")

    def test_reports_ollama_being_down_distinctly_from_a_missing_model(self, fresh_settings):
        def fake_get(url, **kwargs):
            raise httpx.ConnectError("nope", request=httpx.Request("GET", url))

        fresh_settings.setattr(agent_module.httpx, "get", fake_get)
        message = agent_model_error()
        assert message is not None
        assert "ollama serve" in message

    def test_survives_a_malformed_tags_payload(self, fresh_settings):
        """A shape change upstream must not crash `pf chat` with a KeyError."""

        def fake_get(url, **kwargs):
            return httpx.Response(200, json={"unexpected": 1}, request=httpx.Request("GET", url))

        fresh_settings.setattr(agent_module.httpx, "get", fake_get)
        # No "models" key means nothing is installed, which is still an error
        # about the model rather than a traceback.
        assert "ollama pull" in (agent_model_error() or "")


class TestAgUiEndpoint:
    @pytest.fixture
    def client(self, warehouse):
        from personal_finance.api import app

        return TestClient(app)

    def _run_input(self) -> dict:
        return {
            "threadId": "t1",
            "runId": "r1",
            "messages": [{"id": "m1", "role": "user", "content": "hello"}],
            "tools": [],
            "context": [],
            "state": None,
            "forwardedProps": None,
        }

    def test_503_names_the_command_when_the_tool_server_is_down(
        self, client, monkeypatch, warehouse
    ):
        monkeypatch.setenv("MCP_URL", "http://127.0.0.1:1/mcp")
        get_settings.cache_clear()
        response = client.post("/agent", json=self._run_input())
        assert response.status_code == 503
        assert "pf mcp --http" in response.json()["detail"]

    def test_streams_ag_ui_events_for_a_question(self, client, monkeypatch, warehouse):
        """The endpoint answers as an AG-UI event stream, not a JSON body."""
        # `call_tools=[]`: this test is about AG-UI framing, and TestModel's
        # default of calling every tool it can see would instead exercise
        # marts the fixture warehouse deliberately does not have.
        server = build_server()
        monkeypatch.setattr(
            agent_module,
            "get_agent",
            lambda: build_agent(model=TestModel(call_tools=[]), mcp_client=server),
        )

        # Must be a coroutine function: api.py awaits it.
        async def reachable(_client=None):  # noqa: RUF029
            return None

        monkeypatch.setattr("personal_finance.api.tool_server_error", reachable)
        monkeypatch.setattr("personal_finance.api.get_agent", agent_module.get_agent)

        response = client.post("/agent", json=self._run_input())
        assert response.status_code == 200
        body = response.text
        # AG-UI frames the run with lifecycle events; their presence is what a
        # frontend keys off to open and close the message.
        assert "RUN_STARTED" in body
        assert "RUN_FINISHED" in body

    def test_health_still_works_alongside_the_agent_route(self, client):
        """The agent import must not have broken the rest of the app."""
        assert client.get("/health").json() == {"status": "ok"}


class TestChatCommand:
    def test_chat_refuses_without_the_model(self, fresh_settings):
        fresh_settings.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:1")
        result = runner.invoke(cli_app, ["chat"])
        assert result.exit_code == 1
        assert "ollama" in result.output.lower()

    def test_chat_refuses_without_the_tool_server(self, fresh_settings):
        """The model being present is not enough — with no tool server the
        agent has no way to answer anything."""
        fresh_settings.setenv("MCP_URL", "http://127.0.0.1:1/mcp")
        fresh_settings.setattr(agent_module, "agent_model_error", lambda: None)
        result = runner.invoke(cli_app, ["chat"])
        assert result.exit_code == 1
        assert "pf mcp --http" in result.output

    def test_chat_is_listed_in_help(self):
        result = runner.invoke(cli_app, ["--help"])
        assert "chat" in result.output


def test_toolset_reuses_the_servers_own_instructions():
    """The data conventions live next to the tools in mcp_server; restating
    them in AGENT_INSTRUCTIONS would let the two drift apart."""
    assert build_toolset(build_server()).include_instructions is True
