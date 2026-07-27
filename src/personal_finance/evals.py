"""Evaluation suite for the chat agent (Phase 7).

The unit tests in ``tests/test_agent.py`` drive a ``FunctionModel`` that writes
correct SQL by construction. That makes them fast and deterministic, and it
means they are structurally blind to the only question that matters in
production: **can a real model actually answer with the right number?** Every
defect found while running Stage B by hand — a guessed table name, an
identical query resent until the retry budget was gone, giving up after
discovering the schema — was invisible to them.

This module closes that gap, and it can do something most eval suites cannot:
**assert exact figures, with no LLM judge.** "Was that answer good?" is
normally subjective, so evals lean on a judge model and inherit its noise and
cost. Here the ground truth is sitting in the warehouse — the grocery rollup
is one number — so every case computes its own expected value by querying the
marts at build time. That makes the cases deterministic, free, and
self-updating: regenerate the synth data and they still assert the truth
rather than a figure baked in when they were written.

The second thing worth asserting is *how* the answer was reached. A model that
produces the right total after twelve flailing ``run_sql`` attempts has told
you the instructions are wrong, even though the text was correct — so cases
also pin which tool the agent reached for and how many calls it took. That is
the regression signal for :data:`personal_finance.agent.AGENT_INSTRUCTIONS`
and the tool docstrings, neither of which any other test covers.

Running this needs a live Ollama and a running ``pf mcp --http``, so it is
deliberately outside the default test run — see ``pf eval`` and the
``integration`` marker.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic_ai import capture_run_messages
from pydantic_ai.messages import ToolCallPart
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator

from personal_finance.agent import usage_limits

if TYPE_CHECKING:
    from collections.abc import Sequence

    import duckdb
    from pydantic_ai import Agent
    from pydantic_evals.evaluators import EvaluatorContext
    from pydantic_evals.reporting import EvaluationReport

logger = logging.getLogger(__name__)

# Calibrated for `settings.ollama.agent_model`'s default. A smaller model will
# score below this, which is the point: the floor encodes "this model is good
# enough to drive twelve tools", not "the plumbing works" — the unit tests
# already cover the plumbing.
MIN_PASS_RATE = 0.8

# Enough calls to look something up, correct one mistake, and answer. Past
# this the model is flailing, even if it eventually lands on the right number.
DEFAULT_TOOL_BUDGET = 5


@dataclass(frozen=True)
class AgentAnswer:
    """One answer, plus the route the agent took to reach it."""

    text: str
    tools_called: tuple[str, ...]
    # A run that blew up must fail every assertion, not just the figure one.
    # `capture_run_messages` still records the calls made before the crash, so
    # a run that picked the right tool and then died would otherwise satisfy
    # both route and budget and score 2 of 3 — a total failure reported as 67%.
    failed: bool = False


@dataclass(frozen=True)
class Expectation:
    """Ground truth for one case, derived from the marts rather than written down.

    ``amounts`` and ``phrases`` are what the answer must contain; ``tools`` is
    the set of tools any *reasonable* route would use, not a single required
    one — several questions here are legitimately answerable either through a
    curated tool or through ``run_sql``, and failing the second would be
    asserting a preference rather than a fact.
    """

    amounts: tuple[Decimal, ...] = ()
    counts: tuple[int, ...] = ()
    phrases: tuple[str, ...] = ()
    tools: frozenset[str] = frozenset()
    tool_budget: int = DEFAULT_TOOL_BUDGET
    signed: bool = False

    def __post_init__(self) -> None:
        # `all([])` is True, so an expectation with nothing to check would pass
        # every answer including gibberish — a free 100% for a case whose
        # ground-truth derivation silently produced nothing. Refused at
        # construction so it surfaces as a loud build error rather than as a
        # suspiciously good score.
        if not (self.amounts or self.counts or self.phrases):
            message = "An Expectation must assert at least one amount, count or phrase."
            raise ValueError(message)


_NORMALIZE = str.maketrans({",": "", "$": "", "\u2212": "-"})

# Words that carry the direction of a signed figure when the model writes it
# out rather than with a minus. "A deficit of $2,000" is the same answer as
# "-$2,000" and must not be scored as the opposite one.
_NEGATIVE_WORDS = ("deficit", "negative", "shortfall", "overspent", "in the red")


def mentions_amount(text: str, amount: Decimal, *, signed: bool = False) -> bool:
    """Is this money figure present in the answer, however the model wrote it?

    Models render money inconsistently — "$18,876.63", "18876.63", "$18,876"
    are all the same correct answer — so separators are stripped and a
    whole-dollar rendering counts. What is NOT allowed is matching a *different*
    number that happens to contain these digits. A bare substring test scored
    "$91,234.56" as a correct answer to an expected $1,234.56, and "$50.00" as
    a correct answer to an expected $5.00; for a harness whose whole job is
    catching wrong figures, that is the one mistake it cannot make. Hence the
    digit boundaries on both sides.

    ``signed`` is for figures whose sign IS the answer — a net of +$2,000 and
    one of -$2,000 are opposite claims, not the same magnitude. Left off for
    outflow totals, where the marts store a negative and the tools and the
    agent both report a positive magnitude, so a sign check would fail correct
    answers.
    """
    haystack = text.translate(_NORMALIZE)
    magnitude = abs(amount)
    # A trailing "." must block the whole-dollar renderings, or "1234.00" would
    # satisfy an expected 1234.56 via the truncated "1234".
    renderings = (
        (f"{magnitude:.2f}", r"(?![\d])"),
        (f"{magnitude:.0f}", r"(?![\d.])"),
        (str(int(magnitude)), r"(?![\d.])"),
    )
    for rendering, tail in renderings:
        for match in re.finditer(rf"(?<![\d.]){re.escape(rendering)}{tail}", haystack):
            if not signed or _reads_as_negative(haystack, match.start(), text) == (amount < 0):
                return True
    return False


def _reads_as_negative(haystack: str, start: int, text: str) -> bool:
    """Does the figure at ``start`` read as a negative one?"""
    if start > 0 and haystack[start - 1] in "-(":
        return True
    return any(word in text.lower() for word in _NEGATIVE_WORDS)


def mentions_count(text: str, count: int) -> bool:
    """Is this whole-number count present, and not part of another number?

    Kept apart from :func:`mentions_amount` because counts collide with things
    money does not: an answer about a merchant routinely contains dates, and
    "2026-06-30" contains a "30" that has nothing to do with a transaction
    count. Adjacency to any of ``. , - /`` therefore disqualifies a match.
    """
    return re.search(rf"(?<![\d.,\-/]){count}(?![\d.,\-/])", text.translate(_NORMALIZE)) is not None


@dataclass
class ReportsExpectedFigures(Evaluator[str, AgentAnswer, Expectation]):
    """Every ground-truth figure and phrase appears in the answer.

    The single most valuable assertion in the suite: a confidently wrong
    number is far worse than a refusal, and it is exactly what no unit test
    here can catch.
    """

    def evaluate(self, ctx: EvaluatorContext[str, AgentAnswer, Expectation]) -> bool:
        expected = _require_expectation(ctx)
        if ctx.output.failed:
            return False
        text = ctx.output.text
        return (
            all(mentions_amount(text, a, signed=expected.signed) for a in expected.amounts)
            and all(mentions_count(text, c) for c in expected.counts)
            and all(phrase.lower() in text.lower() for phrase in expected.phrases)
        )


@dataclass
class UsedAnExpectedTool(Evaluator[str, AgentAnswer, Expectation]):
    """The agent reached for a tool that could actually answer the question.

    Pins the instructions and tool docstrings. A right answer produced without
    calling any tool is a model reciting from its own weights, which for this
    application is a failure however plausible the number looks.
    """

    def evaluate(self, ctx: EvaluatorContext[str, AgentAnswer, Expectation]) -> bool:
        expected = _require_expectation(ctx)
        if ctx.output.failed:
            return False
        if not expected.tools:
            return bool(ctx.output.tools_called)
        return bool(expected.tools.intersection(ctx.output.tools_called))


@dataclass
class WithinToolBudget(Evaluator[str, AgentAnswer, Expectation]):
    """The answer was reached without flailing.

    Separate from correctness on purpose: a run that lands the right figure on
    the twelfth attempt is a passing answer and a failing agent, and collapsing
    the two would hide the signal that the instructions need work.
    """

    def evaluate(self, ctx: EvaluatorContext[str, AgentAnswer, Expectation]) -> bool:
        expected = _require_expectation(ctx)
        if ctx.output.failed:
            return False
        return len(ctx.output.tools_called) <= expected.tool_budget


def _require_expectation(ctx: EvaluatorContext[str, AgentAnswer, Expectation]) -> Expectation:
    """Fail loudly on a case with no ground truth, rather than scoring it zero.

    A missing expectation is a harness bug, and returning False for it would
    make it indistinguishable from a model that answered nothing correctly —
    the same signature as the un-awaited-coroutine bug, where a broken harness
    read as a total agent failure.
    """
    if ctx.metadata is None:
        message = f"Eval case {ctx.name!r} has no Expectation; its ground truth was never built."
        raise ValueError(message)
    return ctx.metadata


@dataclass
class AgentUnderTest:
    """Runs one question and records the route.

    Pass :meth:`answer` — the bound method, not the instance — as the
    dataset's task. That is not stylistic: pydantic-evals decides whether to
    await the task with ``inspect.iscoroutinefunction``, which is ``False``
    for an instance whose ``__call__`` is async (it inspects the object, not
    ``type(obj).__call__``) and ``True`` for a bound async method. With an
    async ``__call__`` every case silently scored zero against an un-awaited
    coroutine, and the report read as a total agent failure rather than as a
    harness bug.
    """

    agent: Agent[None, str]

    async def answer(self, question: str) -> AgentAnswer:
        with capture_run_messages() as messages:
            try:
                result = await self.agent.run(question, usage_limits=usage_limits())
                text = result.output
            except Exception as exc:
                # Deliberately broad. A run that blows up is a *result* here,
                # not an error to propagate: the harness exists to score how
                # often that happens, and letting one exploded case abort the
                # whole dataset would destroy the run that was measuring it.
                # The text records the failure so it shows in the report.
                logger.warning("Eval question failed: %s", question, exc_info=True)
                return AgentAnswer(
                    text=f"<run failed: {type(exc).__name__}: {exc}>",
                    tools_called=tool_names(messages),
                    failed=True,
                )
        return AgentAnswer(text=text, tools_called=tool_names(messages))


def tool_names(messages: Sequence[Any]) -> tuple[str, ...]:
    """Tool calls in the order they were made, duplicates kept.

    Repeats are the signal, not noise — "called run_sql four times" is the
    finding. Read from the message history rather than from a counter inside
    the tools, because the tools live in another process: this module never
    touches the MCP server's code.

    Matched on the imported type rather than on ``type(part).__name__``: a
    rename upstream would make every tool call invisible, and that surfaces as
    "the agent used no tools" — a harness break wearing an agent failure's
    clothes. An import error is the louder, more honest way to find out.
    """
    return tuple(
        part.tool_name
        for message in messages
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolCallPart)
    )


def pass_rate(report: EvaluationReport[Any, Any, Any]) -> float:
    """Fraction of assertions that passed, across every case.

    ``averages()`` returns ``None`` for an empty run; a suite that evaluated
    nothing has not passed, so that reads as 0.0 rather than as success.
    """
    averages = report.averages()
    return averages.assertions if averages and averages.assertions is not None else 0.0


@dataclass
class _Ground:
    """The mart figures the cases are built from, read once."""

    conn: duckdb.DuckDBPyConnection
    missing: list[str] = field(default_factory=list)

    def one(self, label: str, sql: str) -> tuple[Any, ...] | None:
        """Fetch one row of ground truth, recording rather than raising if absent.

        A partially-built warehouse should cost the cases that depend on the
        missing mart, not the whole suite — and the skipped ones are named in
        `missing` so a thin run cannot be mistaken for a clean one.
        """
        try:
            row = self.conn.execute(sql).fetchone()
        except Exception:
            logger.warning("Eval case %r: ground-truth query failed", label, exc_info=True)
            row = None
        if row is None or any(value is None for value in row):
            # Every column, not just the first: `net_amount` is a bare sum and
            # so is NULL-able, and a NULL reaching Decimal() raises out of
            # build_dataset entirely — losing every other case to one gap.
            self.missing.append(label)
            return None
        return row


@dataclass(frozen=True)
class EvalPlan:
    """The cases that could be built, and the ones that could not.

    ``skipped`` is returned rather than only logged because a thin run scores
    like a clean one: four missing marts leave a single case that can hit 100%
    and satisfy a ``--min-score`` gate, with the warning buried in log output
    the CLI never configures a handler for.
    """

    dataset: Dataset[str, AgentAnswer, Expectation]
    skipped: tuple[str, ...] = ()


def build_plan(conn: duckdb.DuckDBPyConnection) -> EvalPlan:
    """Build the case set, computing every expected value from the warehouse.

    Nothing here hardcodes a figure. Regenerating the synth data changes what
    the cases assert, so they keep testing the agent rather than slowly
    becoming a record of what the warehouse contained the day they were
    written.
    """
    ground = _Ground(conn)
    cases: list[Case[str, AgentAnswer, Expectation]] = []

    groceries = ground.one(
        "groceries_total",
        "SELECT total_outflow FROM main_gold.gold_category_rollups "
        "WHERE path = 'essentials/groceries'",
    )
    if groceries:
        cases.append(
            Case(
                name="groceries_total",
                inputs="How much have I spent on groceries in total?",
                metadata=Expectation(
                    amounts=(Decimal(groceries[0]),),
                    tools=frozenset({"spend_by_category", "run_sql"}),
                ),
            )
        )

    merchant = ground.one(
        "top_merchant",
        "SELECT merchant_name, total_outflow FROM main_silver.silver_merchants "
        "ORDER BY total_outflow DESC LIMIT 1",
    )
    if merchant:
        cases.append(
            Case(
                name="top_merchant",
                inputs="Which merchant have I spent the most money with, and how much in total?",
                metadata=Expectation(
                    amounts=(Decimal(merchant[1]),),
                    phrases=(merchant[0],),
                    # Only the curated tool. `silver_merchants.total_outflow`
                    # sums outflows with no `is_transfer` filter, unlike every
                    # other mart — so an agent computing this through run_sql
                    # and correctly excluding transfers would get a different,
                    # equally defensible number and be scored wrong. Scoring
                    # the more correct route as a failure is worse than not
                    # exercising it here. (The mart itself is worth a look.)
                    tools=frozenset({"top_merchants"}),
                ),
            )
        )

    month = ground.one(
        "month_net",
        "SELECT month, net_amount FROM main_gold.gold_monthly_flow "
        "ORDER BY abs(net_amount) DESC LIMIT 1",
    )
    if month:
        cases.append(
            Case(
                name="month_net",
                inputs=(f"What was my net amount — income minus spending — in {month[0]:%B %Y}?"),
                metadata=Expectation(
                    amounts=(Decimal(month[1]),),
                    tools=frozenset({"monthly_flow", "run_sql"}),
                    # The one case where sign carries meaning: a $2,000 surplus
                    # and a $2,000 deficit are opposite answers, and magnitude
                    # matching would score them the same. Outflow totals stay
                    # unsigned, since the marts store a negative there while
                    # the tools and the agent both report a magnitude.
                    signed=True,
                ),
            )
        )

    # The two below have no curated tool that answers them, so they can only be
    # reached through run_sql. They are the cases that test whether the agent
    # can do real analysis rather than read the tables the dashboard shows —
    # which was the whole reason run_sql exists.
    largest = ground.one(
        "largest_outflow",
        "SELECT merchant_name, amount FROM main_silver.silver_transactions "
        "WHERE NOT is_transfer AND amount < 0 ORDER BY amount ASC LIMIT 1",
    )
    if largest:
        cases.append(
            Case(
                name="largest_outflow",
                inputs="What is the single largest outflow transaction, and at which merchant?",
                metadata=Expectation(
                    amounts=(Decimal(largest[1]),),
                    phrases=(largest[0],),
                    tools=frozenset({"run_sql"}),
                    # Higher: this legitimately needs describe_table first.
                    tool_budget=DEFAULT_TOOL_BUDGET + 2,
                ),
            )
        )

    busiest = ground.one(
        "busiest_merchant",
        "SELECT merchant_name, transaction_count FROM main_silver.silver_merchants "
        "ORDER BY transaction_count DESC LIMIT 1",
    )
    if busiest:
        cases.append(
            Case(
                name="busiest_merchant",
                inputs=(
                    "Which merchant do I have the most separate transactions with, and how many?"
                ),
                metadata=Expectation(
                    counts=(int(busiest[1]),),
                    phrases=(busiest[0],),
                    tools=frozenset({"top_merchants", "run_sql"}),
                ),
            )
        )

    if ground.missing:
        logger.warning(
            "Skipped %d eval case(s) with no data in the warehouse: %s",
            len(ground.missing),
            ", ".join(ground.missing),
        )
    return EvalPlan(
        dataset=Dataset(
            name="personal-finance chat agent",
            cases=cases,
            evaluators=[ReportsExpectedFigures(), UsedAnExpectedTool(), WithinToolBudget()],
        ),
        skipped=tuple(ground.missing),
    )
