"""Tests for the agent eval suite (personal_finance.evals).

Two layers, and the split is the point of the file.

Everything up to `TestIntegrationGate` tests the *harness* — figure matching,
the evaluators, ground-truth derivation, tool capture — and needs neither
Ollama nor a listening MCP server, so it runs in CI like anything else. A
scoring harness that is itself wrong is worse than none, because it reports
confident numbers about a thing it measured incorrectly.

`TestIntegrationGate` is the eval proper: a real model against real tools. It
is marked `integration` and deselected by default (see pyproject.toml), since
CI has no Ollama.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import duckdb
import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_evals import Case, Dataset

from personal_finance.agent import FinanceAgent
from personal_finance.config import get_settings
from personal_finance.evals import (
    MIN_PASS_RATE,
    AgentAnswer,
    AgentUnderTest,
    Expectation,
    ReportsExpectedFigures,
    UsedAnExpectedTool,
    WithinToolBudget,
    build_plan,
    mentions_amount,
    mentions_count,
    pass_rate,
    tool_names,
)
from personal_finance.mcp_server import build_server


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    """A warehouse holding every mart the cases derive ground truth from."""
    path = tmp_path / "warehouse.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute("CREATE SCHEMA main_gold")
        conn.execute("CREATE SCHEMA main_silver")
        conn.execute(
            "CREATE TABLE main_gold.gold_category_rollups AS SELECT * FROM (VALUES "
            "('essentials/groceries', 1234.56, 42), ('lifestyle/books', 90.00, 3)"
            ") AS t(path, total_outflow, transaction_count)"
        )
        conn.execute(
            "CREATE TABLE main_silver.silver_merchants AS SELECT * FROM (VALUES "
            "('COSTCO', 12, 1400.00), ('NETFLIX', 30, 92.94)"
            ") AS t(merchant_name, transaction_count, total_outflow)"
        )
        conn.execute(
            "CREATE TABLE main_gold.gold_monthly_flow AS SELECT * FROM (VALUES "
            "(DATE '2026-06-01', 5000.00, 3000.00, 2000.00, 42),"
            "(DATE '2026-07-01', 5000.00, 4900.00, 100.00, 40)"
            ") AS t(month, total_inflow, total_outflow, net_amount, transaction_count)"
        )
        conn.execute(
            "CREATE TABLE main_silver.silver_transactions AS SELECT * FROM (VALUES "
            "('t1', DATE '2026-06-02', 'CITYLINE RENT', -1800.00, 'outflow', false),"
            "('t2', DATE '2026-06-03', 'CAFE', -5.00, 'outflow', false),"
            "('t3', DATE '2026-06-04', 'TRANSFER OUT', -9999.00, 'outflow', true)"
            ") AS t(transaction_id, posted_on, merchant_name, amount, flow, is_transfer)"
        )
        conn.execute("CHECKPOINT")
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(path))
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


def score(answer: AgentAnswer, expected: Expectation) -> dict[str, bool]:
    """Run the real evaluators through the real framework, on one answer."""
    dataset = Dataset(
        name="probe",
        cases=[Case(name="c", inputs="q", metadata=expected)],
        evaluators=[ReportsExpectedFigures(), UsedAnExpectedTool(), WithinToolBudget()],
    )
    report = dataset.evaluate_sync(lambda _question: answer, progress=False)
    return {name: result.value for name, result in report.cases[0].assertions.items()}


class TestMentionsAmount:
    """Money formatting must not be mistaken for money accuracy."""

    @pytest.mark.parametrize(
        "text",
        [
            "You spent $1,234.56 on groceries.",
            "You spent 1234.56 on groceries.",
            "Total: $1234.56",
            # Whole dollars: right answer, rounder phrasing.
            "About $1,235 on groceries.",
            "roughly 1234 dollars",
        ],
    )
    def test_accepts_every_way_a_model_writes_the_right_number(self, text):
        assert mentions_amount(text, Decimal("1234.56"))

    @pytest.mark.parametrize(
        "text",
        [
            "You spent $1,432.56 on groceries.",
            "You spent nothing on groceries.",
            "Total: $12.34",
        ],
    )
    def test_rejects_the_wrong_number(self, text):
        assert not mentions_amount(text, Decimal("1234.56"))

    def test_sign_is_ignored_because_outflows_are_reported_as_magnitudes(self):
        """The marts store outflows negative; the tools and the agent both
        report positive magnitudes, so a leading minus must not fail a case."""
        assert mentions_amount("The largest was $1,800.00 at CITYLINE", Decimal("-1800.00"))

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # A bare substring test scored every one of these as CORRECT. They
            # are the reason this matcher is anchored: for a harness whose only
            # job is catching wrong figures, accepting a wrong figure is the
            # one mistake that makes every number it reports meaningless.
            ("You spent $91,234.56 on groceries.", "1234.56"),  # off by $90,000
            ("You spent $1,234.567 on groceries.", "1234.56"),  # suffix
            ("You spent $1,234.00 on groceries.", "1234.56"),  # whole-dollar collision
            ("You spent $50.00 at CAFE.", "5.00"),  # 10x, via the 1-digit rendering
            ("You spent $3,500.25 in total.", "5.00"),  # an unrelated 5
            ("In 1990 you spent nothing there.", "90.00"),  # a year
        ],
    )
    def test_a_different_number_containing_these_digits_is_not_a_match(self, text, expected):
        assert not mentions_amount(text, Decimal(expected))

    def test_small_amounts_do_not_degrade_to_single_digit_matching(self):
        """Under $10 the whole-dollar renderings are one character, so an
        unanchored matcher becomes "does the answer contain a 5 anywhere" —
        and the synth data has a $5.00 CAFE row."""
        assert not mentions_amount("You spent $1,275.00 in total.", Decimal("5.00"))
        assert mentions_amount("You spent $5.00 at CAFE.", Decimal("5.00"))


class TestSignedAmounts:
    """A net of +$2,000 and one of -$2,000 are opposite answers."""

    @pytest.mark.parametrize(
        "text",
        ["Your net was -$2,000.00.", "You ran a deficit of $2,000.00.", "Net: ($2,000.00)"],
    )
    def test_a_negative_net_is_recognised_however_it_is_written(self, text):
        assert mentions_amount(text, Decimal("-2000.00"), signed=True)

    def test_a_surplus_does_not_satisfy_an_expected_deficit(self):
        """The failure magnitude matching cannot see: same number, opposite
        claim. `month_net` picks the most extreme month, which is frequently
        the negative one."""
        assert not mentions_amount(
            "Your net was +$2,000.00, a surplus.", Decimal("-2000.00"), signed=True
        )

    def test_a_deficit_does_not_satisfy_an_expected_surplus(self):
        assert not mentions_amount("Your net was -$2,000.00.", Decimal("2000.00"), signed=True)


class TestMentionsCount:
    """Counts collide with things money does not — dates, above all."""

    def test_a_date_is_not_a_transaction_count(self):
        assert not mentions_count("NETFLIX, last seen 2026-06-30, 7 transactions.", 30)

    def test_a_larger_count_is_not_a_match(self):
        assert not mentions_count("You have 130 transactions with NETFLIX.", 30)

    def test_the_real_count_matches(self):
        assert mentions_count("You have 30 transactions with NETFLIX.", 30)


class TestEvaluators:
    def test_a_correct_and_efficient_answer_passes_everything(self):
        assert score(
            AgentAnswer(text="You spent $1,234.56 at COSTCO.", tools_called=("top_merchants",)),
            Expectation(
                amounts=(Decimal("1234.56"),),
                phrases=("COSTCO",),
                tools=frozenset({"top_merchants"}),
            ),
        ) == {"ReportsExpectedFigures": True, "UsedAnExpectedTool": True, "WithinToolBudget": True}

    def test_a_wrong_figure_fails_even_though_the_route_was_right(self):
        """The assertion that matters most: a confidently wrong number."""
        results = score(
            AgentAnswer(text="You spent $9,999.99 at COSTCO.", tools_called=("top_merchants",)),
            Expectation(amounts=(Decimal("1234.56"),), tools=frozenset({"top_merchants"})),
        )
        assert results["ReportsExpectedFigures"] is False
        assert results["UsedAnExpectedTool"] is True

    def test_a_right_answer_with_no_tool_call_fails(self):
        """A model reciting a plausible number from its own weights is the
        failure this application most needs to catch, however good it looks."""
        results = score(
            AgentAnswer(text="You spent $1,234.56.", tools_called=()),
            Expectation(amounts=(Decimal("1234.56"),), tools=frozenset({"spend_by_category"})),
        )
        assert results["ReportsExpectedFigures"] is True
        assert results["UsedAnExpectedTool"] is False

    def test_flailing_fails_the_budget_but_not_correctness(self):
        """Kept as separate assertions on purpose — a right figure reached on
        the twelfth try is a passing answer and a failing agent, and merging
        them would hide the signal that the instructions need work."""
        results = score(
            AgentAnswer(text="$1,234.56", tools_called=("run_sql",) * 12),
            Expectation(amounts=(Decimal("1234.56"),), tools=frozenset({"run_sql"}), tool_budget=5),
        )
        assert results["ReportsExpectedFigures"] is True
        assert results["UsedAnExpectedTool"] is True
        assert results["WithinToolBudget"] is False

    def test_any_expected_tool_counts_not_a_specific_one(self):
        """Several questions are legitimately answerable through a curated
        tool OR run_sql; failing the second would assert a preference."""
        results = score(
            AgentAnswer(text="$1,234.56", tools_called=("run_sql",)),
            Expectation(
                amounts=(Decimal("1234.56"),), tools=frozenset({"spend_by_category", "run_sql"})
            ),
        )
        assert results["UsedAnExpectedTool"] is True

    def test_a_crashed_run_fails_every_assertion_not_just_correctness(self):
        """`capture_run_messages` still records the calls made before a crash,
        so a run that picked the right tool and then died would otherwise stay
        within budget and route correctly — scoring a total failure as 67%,
        the direction that hides problems."""
        results = score(
            AgentAnswer(
                text="<run failed: RuntimeError: model died>",
                tools_called=("spend_by_category",),
                failed=True,
            ),
            Expectation(amounts=(Decimal("1234.56"),), tools=frozenset({"spend_by_category"})),
        )
        assert results == {
            "ReportsExpectedFigures": False,
            "UsedAnExpectedTool": False,
            "WithinToolBudget": False,
        }

    def test_an_expectation_asserting_nothing_is_refused_at_construction(self):
        """`all([])` is True, so an expectation with nothing to check would
        pass any answer including gibberish — a free 100% for a case whose
        ground truth silently came out empty."""
        with pytest.raises(ValueError, match="at least one amount"):
            Expectation(tools=frozenset({"run_sql"}))

    def test_a_case_with_no_expectation_raises_rather_than_scoring_zero(self):
        """A missing expectation is a harness bug. Scoring it zero makes it
        indistinguishable from a model that answered nothing correctly — the
        same signature as the un-awaited-coroutine bug, where a broken harness
        read as a total agent failure."""
        dataset = Dataset(
            name="probe",
            cases=[Case(name="no-metadata", inputs="q")],
            evaluators=[ReportsExpectedFigures()],
        )
        report = dataset.evaluate_sync(
            lambda _q: AgentAnswer(text="anything", tools_called=()), progress=False
        )
        assert report.cases[0].evaluator_failures, "a misconfigured case scored silently"

    def test_a_missing_phrase_fails(self):
        results = score(
            AgentAnswer(text="You spent $1,234.56 somewhere.", tools_called=("top_merchants",)),
            Expectation(amounts=(Decimal("1234.56"),), phrases=("COSTCO",)),
        )
        assert results["ReportsExpectedFigures"] is False


class TestToolNames:
    def test_records_repeats_in_order(self):
        """Repeats ARE the finding — "called run_sql four times" is what a
        flailing agent looks like, so deduplicating would erase the signal."""
        messages = [
            ModelResponse(parts=[ToolCallPart("run_sql", {})]),
            ModelResponse(parts=[ToolCallPart("run_sql", {})]),
            ModelResponse(parts=[ToolCallPart("list_tables", {})]),
            ModelResponse(parts=[TextPart("done")]),
        ]
        assert tool_names(messages) == ("run_sql", "run_sql", "list_tables")

    def test_no_tool_calls_is_an_empty_tuple(self):
        assert tool_names([ModelResponse(parts=[TextPart("hi")])]) == ()


class TestPassRate:
    def test_is_the_fraction_of_passing_assertions(self):
        dataset = Dataset(
            name="probe",
            cases=[
                Case(name="good", inputs="q", metadata=Expectation(amounts=(Decimal("1.00"),))),
                Case(name="bad", inputs="q", metadata=Expectation(amounts=(Decimal("2.00"),))),
            ],
            evaluators=[ReportsExpectedFigures()],
        )
        report = dataset.evaluate_sync(
            lambda _q: AgentAnswer(text="1.00", tools_called=()), progress=False
        )
        assert pass_rate(report) == pytest.approx(0.5)

    def test_getting_every_figure_wrong_falls_below_the_gate(self):
        """`pass_rate` averages assertions, so correctness is only one third
        of the score. This pins that the floor still rejects an agent that
        routes perfectly and is wrong about every number — the failure mode
        the whole suite exists for."""
        dataset = Dataset(
            name="probe",
            cases=[
                Case(
                    name=f"c{i}",
                    inputs="q",
                    metadata=Expectation(
                        amounts=(Decimal("1234.56"),), tools=frozenset({"run_sql"})
                    ),
                )
                for i in range(5)
            ],
            evaluators=[ReportsExpectedFigures(), UsedAnExpectedTool(), WithinToolBudget()],
        )
        report = dataset.evaluate_sync(
            lambda _q: AgentAnswer(text="$9,999.99", tools_called=("run_sql",)), progress=False
        )
        assert pass_rate(report) < MIN_PASS_RATE

    def test_an_empty_run_scores_zero_rather_than_passing(self):
        """`averages()` is None with no cases. Reading that as success would
        make a suite that evaluated nothing look like a clean bill of health."""
        report = Dataset(name="empty", cases=[]).evaluate_sync(lambda _q: None, progress=False)
        assert pass_rate(report) == pytest.approx(0.0)


class TestBuildDataset:
    def test_derives_every_figure_from_the_warehouse(self, warehouse):
        """Nothing is hardcoded, so regenerating the synth data changes what
        the cases assert instead of leaving them asserting history."""
        with duckdb.connect(str(warehouse), read_only=True) as conn:
            dataset = build_plan(conn).dataset
        by_name = {case.name: case for case in dataset.cases}

        assert by_name["groceries_total"].metadata.amounts == (Decimal("1234.56"),)
        # Top by SPEND, not by transaction count.
        assert by_name["top_merchant"].metadata.phrases == ("COSTCO",)
        # Top by transaction COUNT — a different merchant, which is what makes
        # this case worth having rather than a restatement of the one above.
        assert by_name["busiest_merchant"].metadata.phrases == ("NETFLIX",)
        assert by_name["busiest_merchant"].metadata.counts == (30,)

    def test_the_largest_outflow_case_excludes_transfers(self, warehouse):
        """The fixture's biggest row by magnitude is a -9999 transfer between
        the user's own accounts. Treating it as spend would bake this
        project's central convention backwards into the ground truth."""
        with duckdb.connect(str(warehouse), read_only=True) as conn:
            dataset = build_plan(conn).dataset
        largest = next(c for c in dataset.cases if c.name == "largest_outflow")
        assert largest.metadata.phrases == ("CITYLINE RENT",)
        assert largest.metadata.amounts == (Decimal("-1800.00"),)

    def test_run_sql_only_questions_are_present(self, warehouse):
        """These are what distinguish an agent that can analyse from one that
        reads the same tables the dashboard shows."""
        with duckdb.connect(str(warehouse), read_only=True) as conn:
            dataset = build_plan(conn).dataset
        sql_only = [c for c in dataset.cases if c.metadata.tools == frozenset({"run_sql"})]
        assert sql_only, "no case forces open-ended analysis"

    def test_a_missing_mart_costs_only_its_own_cases(self, tmp_path, monkeypatch):
        """A partially-built warehouse must not take down the whole suite —
        but the surviving cases must still be real ones."""
        path = tmp_path / "partial.duckdb"
        with duckdb.connect(str(path)) as conn:
            conn.execute("CREATE SCHEMA main_silver")
            conn.execute(
                "CREATE TABLE main_silver.silver_merchants AS SELECT * FROM (VALUES "
                "('COSTCO', 12, 1400.00)) AS t(merchant_name, transaction_count, total_outflow)"
            )
            conn.execute("CHECKPOINT")
        with duckdb.connect(str(path), read_only=True) as conn:
            dataset = build_plan(conn).dataset
        names = {case.name for case in dataset.cases}
        assert "top_merchant" in names
        assert "groceries_total" not in names  # gold_category_rollups absent

    def test_the_skipped_cases_are_reported_not_just_logged(self, tmp_path):
        """A run of one surviving case can score 100% and satisfy a
        --min-score gate. If the skips are only logged, a thin run is
        indistinguishable from a clean one."""
        path = tmp_path / "partial.duckdb"
        with duckdb.connect(str(path)) as conn:
            conn.execute("CREATE SCHEMA main_silver")
            conn.execute(
                "CREATE TABLE main_silver.silver_merchants AS SELECT * FROM (VALUES "
                "('COSTCO', 12, 1400.00)) AS t(merchant_name, transaction_count, total_outflow)"
            )
            conn.execute("CHECKPOINT")
        with duckdb.connect(str(path), read_only=True) as conn:
            plan = build_plan(conn)
        assert "groceries_total" in plan.skipped
        assert "largest_outflow" in plan.skipped
        assert len(plan.skipped) + len(plan.dataset.cases) == 5

    def test_a_null_in_any_column_skips_the_case_instead_of_exploding(self, tmp_path):
        """`net_amount` is a bare sum and so is NULL-able. Guarding only the
        first column let a NULL reach Decimal() and raise out of the whole
        build, losing every other case to one gap."""
        path = tmp_path / "nulls.duckdb"
        with duckdb.connect(str(path)) as conn:
            conn.execute("CREATE SCHEMA main_gold")
            conn.execute(
                "CREATE TABLE main_gold.gold_monthly_flow AS SELECT * FROM (VALUES "
                "(DATE '2026-06-01', 5000.00, 3000.00, CAST(NULL AS DECIMAL(18,2)), 42)"
                ") AS t(month, total_inflow, total_outflow, net_amount, transaction_count)"
            )
            conn.execute("CHECKPOINT")
        with duckdb.connect(str(path), read_only=True) as conn:
            plan = build_plan(conn)  # must not raise
        assert "month_net" in plan.skipped

    def test_an_empty_warehouse_yields_no_cases_rather_than_broken_ones(self, tmp_path):
        path = tmp_path / "bare.duckdb"
        duckdb.connect(str(path)).close()
        with duckdb.connect(str(path), read_only=True) as conn:
            assert build_plan(conn).dataset.cases == []


class TestAgentUnderTest:
    """The task callable, exercised against the real MCP server in process."""

    def test_records_the_answer_and_the_route(self, warehouse):
        def call_then_answer(messages, info):
            if len(messages) == 1:
                return ModelResponse(parts=[ToolCallPart("top_merchants", {"limit": 1})])
            return ModelResponse(parts=[TextPart("COSTCO, $1,400.00")])

        agent = FinanceAgent(FunctionModel(call_then_answer), mcp_client=build_server())
        answer = asyncio.run(AgentUnderTest(agent).answer("who?"))

        assert answer.tools_called == ("top_merchants",)
        assert "COSTCO" in answer.text

    def test_the_dataset_awaits_the_task(self, warehouse):
        """Regression: pydantic-evals decides whether to await via
        `inspect.iscoroutinefunction`, which is False for an instance with an
        async `__call__`. Passing the instance therefore scored every case
        against an un-awaited coroutine — 0% across the board, reading as a
        total agent failure rather than as a broken harness. Only calling the
        task the way the framework does catches it; awaiting it by hand, as
        the tests above do, passes either way.
        """

        def answer_directly(messages, info):
            return ModelResponse(parts=[TextPart("COSTCO")])

        agent = FinanceAgent(FunctionModel(answer_directly), mcp_client=build_server())
        dataset = Dataset(
            name="probe",
            cases=[Case(name="c", inputs="who?", metadata=Expectation(phrases=("COSTCO",)))],
            evaluators=[ReportsExpectedFigures()],
        )
        report = dataset.evaluate_sync(AgentUnderTest(agent).answer, progress=False)

        assert isinstance(report.cases[0].output, AgentAnswer)
        assert report.cases[0].assertions["ReportsExpectedFigures"].value is True

    def test_a_blown_up_run_scores_as_a_failed_case_not_an_aborted_suite(self, warehouse):
        """One exploded question must not destroy the run that was measuring
        how often that happens."""

        def always_explode(messages, info):
            raise RuntimeError("model died")

        agent = FinanceAgent(FunctionModel(always_explode), mcp_client=build_server())
        answer = asyncio.run(AgentUnderTest(agent).answer("who?"))

        assert "run failed" in answer.text
        assert "model died" in answer.text


@pytest.mark.integration
class TestIntegrationGate:
    """The eval proper. Needs `ollama serve` and a running `pf mcp --http`.

    Deselected by default; run with `pytest -m integration`.
    """

    def test_the_agent_scores_above_the_floor(self):
        from personal_finance.agent import agent_model_error, tool_server_error

        for problem in (agent_model_error(), asyncio.run(tool_server_error())):
            if problem:
                pytest.skip(problem)

        settings = get_settings()
        with duckdb.connect(str(settings.data.warehouse_path), read_only=True) as conn:
            plan = build_plan(conn)
        if not plan.dataset.cases:
            pytest.skip("No marts built — run `pf ingest` and `pf transform` first.")

        async def run():
            agent = FinanceAgent()
            async with agent:
                return await plan.dataset.evaluate(AgentUnderTest(agent).answer)

        report = asyncio.run(run())
        report.print(include_input=True, include_output=True)
        assert pass_rate(report) >= MIN_PASS_RATE
