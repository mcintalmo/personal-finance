"""Tests for the Dash app (personal_finance.dashboard).

Two things are worth testing here and they are not the layout code.

**The AG-UI stream parser**, because its field names came off the wire rather
than from a spec — text arrives as `delta`, but a tool's name arrives as
`toolCallName` on a different event from its arguments — and a wrong guess
produces an empty answer rather than a crash.

**The palette**, because its values are load-bearing rather than decorative:
they were chosen by a validator against contrast and colour-vision-deficiency
thresholds, and an innocent-looking edit would silently drop below them.

The page modules are exercised through `render` functions with the HTTP layer
stubbed, which catches the errors that actually happen — a renamed API field,
a Plotly property that rejects its input — without a browser.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

from personal_finance.dashboard._client import ApiError
from personal_finance.dashboard.agent_stream import (
    ChatTurn,
    apply_event,
    parse_sse_line,
    replay,
    run_input,
)
from personal_finance.dashboard.theme import (
    CATEGORICAL_DARK,
    CATEGORICAL_LIGHT,
    SEQUENTIAL_BLUE,
    STATUS,
    ink,
    rgba,
)


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}"


class TestParseSseLine:
    def test_reads_a_data_frame(self):
        assert parse_sse_line(sse({"type": "RUN_STARTED"})) == {"type": "RUN_STARTED"}

    @pytest.mark.parametrize("line", ["", ":ping", "event: message", "id: 4", "  "])
    def test_ignores_everything_that_is_not_a_data_frame(self, line):
        """An SSE stream is mostly blanks, comments and other field lines."""
        assert parse_sse_line(line) is None

    def test_a_malformed_frame_is_skipped_not_raised_on(self):
        """The stream is still live at that point — killing the answer over
        one bad frame would throw away the text already received."""
        assert parse_sse_line("data: {not json") is None

    def test_a_non_object_frame_is_skipped(self):
        assert parse_sse_line("data: [1, 2]") is None


class TestApplyEvent:
    def test_text_deltas_accumulate(self):
        """119 deltas arrived for one real answer, so this is the hot path."""
        turn = ChatTurn()
        for piece in ("You ", "spent ", "$12.00"):
            apply_event(turn, {"type": "TEXT_MESSAGE_CONTENT", "delta": piece})
        assert turn.text == "You spent $12.00"

    def test_a_tool_call_is_recorded_by_name(self):
        """`toolCallName`, not `name` — the field this got wrong would show an
        empty tool trail rather than fail, so it is pinned explicitly."""
        turn = ChatTurn()
        apply_event(
            turn,
            {"type": "TOOL_CALL_START", "toolCallId": "c1", "toolCallName": "spend_by_category"},
        )
        assert turn.tools == ["spend_by_category"]

    def test_repeated_tools_are_kept(self):
        """ "Called run_sql four times" is the finding, not noise."""
        turn = ChatTurn()
        for _ in range(3):
            apply_event(turn, {"type": "TOOL_CALL_START", "toolCallName": "run_sql"})
        assert turn.tools == ["run_sql"] * 3

    def test_unknown_events_are_inert(self):
        """AG-UI has more event types than this page renders; a new one
        upstream must not become an error here."""
        turn = ChatTurn()
        apply_event(turn, {"type": "STATE_DELTA", "delta": "ignored"})
        apply_event(turn, {"type": "STEP_STARTED"})
        assert turn == ChatTurn()

    def test_run_error_carries_the_message_and_finishes(self):
        turn = ChatTurn()
        apply_event(turn, {"type": "RUN_ERROR", "message": "tool server unreachable"})
        assert turn.error == "tool server unreachable"
        assert turn.finished

    def test_run_finished_with_a_success_outcome_is_not_an_error(self):
        turn = ChatTurn()
        apply_event(turn, {"type": "RUN_FINISHED", "outcome": {"type": "success"}})
        assert turn.finished
        assert turn.error is None

    def test_run_finished_with_a_failing_outcome_is_an_error(self):
        """A run can end without a RUN_ERROR frame — treating every
        RUN_FINISHED as success would render a failed run as a blank answer."""
        turn = ChatTurn()
        apply_event(turn, {"type": "RUN_FINISHED", "outcome": {"type": "error", "message": "nope"}})
        assert turn.finished
        assert turn.error == "nope"


class TestReplayRecordedStream:
    """The exact frame sequence captured from a real run."""

    FRAMES: ClassVar[list[str]] = [
        sse({"type": "RUN_STARTED", "threadId": "t", "runId": "r"}),
        sse({"type": "TOOL_CALL_START", "toolCallId": "c1", "toolCallName": "spend_by_category"}),
        sse({"type": "TOOL_CALL_ARGS", "toolCallId": "c1", "delta": '{"category_path":"x"}'}),
        sse({"type": "TOOL_CALL_END", "toolCallId": "c1"}),
        sse({"type": "TOOL_CALL_RESULT", "toolCallId": "c1", "content": '{"row_count":4}'}),
        sse({"type": "TEXT_MESSAGE_START", "messageId": "m", "role": "assistant"}),
        sse({"type": "TEXT_MESSAGE_CONTENT", "messageId": "m", "delta": "You spent "}),
        sse({"type": "TEXT_MESSAGE_CONTENT", "messageId": "m", "delta": "$18,876.63."}),
        sse({"type": "TEXT_MESSAGE_END", "messageId": "m"}),
        sse({"type": "RUN_FINISHED", "outcome": {"type": "success"}}),
        "",
    ]

    def test_the_whole_stream_folds_into_one_answer(self):
        turn = replay(ChatTurn(), self.FRAMES)
        assert turn.text == "You spent $18,876.63."
        assert turn.tools == ["spend_by_category"]
        assert turn.finished
        assert turn.error is None

    def test_tool_args_do_not_leak_into_the_answer(self):
        """TOOL_CALL_ARGS also carries a `delta`. Folding it into the text —
        the obvious mistake given the shared field name — would splice raw
        JSON into the middle of the user's answer."""
        turn = replay(ChatTurn(), self.FRAMES)
        assert "category_path" not in turn.text

    def test_tool_results_do_not_leak_into_the_answer(self):
        turn = replay(ChatTurn(), self.FRAMES)
        assert "row_count" not in turn.text


class TestChatTurnStatus:
    def test_names_the_tool_while_it_runs(self):
        """A local model spends its first 30s picking a tool; "Thinking…"
        for that long reads as a hang."""
        turn = ChatTurn(tools=["run_sql"])
        assert turn.status == "Using run_sql…"

    def test_thinking_before_any_tool(self):
        assert ChatTurn().status == "Thinking…"

    def test_error_beats_finished(self):
        assert ChatTurn(error="boom", finished=True).status == "Failed"


class TestRunInput:
    def test_matches_the_ag_ui_request_shape(self):
        """camelCase keys — the endpoint rejects snake_case outright."""
        body = run_input("hello", thread_id="t", run_id="r")
        assert set(body) == {
            "threadId",
            "runId",
            "messages",
            "tools",
            "context",
            "state",
            "forwardedProps",
        }
        assert body["messages"] == [{"id": "r", "role": "user", "content": "hello"}]


class TestApiError:
    def test_503_is_recognised_as_missing_marts(self):
        """`get_optional` keys off this to let a page render without its
        supplementary band."""
        assert ApiError("nope", status_code=503).is_missing_marts

    @pytest.mark.parametrize("status", [400, 404, 500, None])
    def test_nothing_else_is_softened(self, status):
        """A 500 rendering as an empty section is indistinguishable from good
        news, which is the silent failure this project keeps guarding against."""
        assert not ApiError("nope", status_code=status).is_missing_marts


class TestPalette:
    """These values were picked by a validator, not by eye."""

    def test_three_categorical_slots_in_both_modes(self):
        """Not a style choice: past three slots no ordering of the source
        palette clears the all-pairs colour-vision floors."""
        assert len(CATEGORICAL_LIGHT) == len(CATEGORICAL_DARK) == 3

    def test_dark_is_its_own_set_of_steps(self):
        """Flipping a light palette onto a dark surface loses the contrast
        guarantee, so the dark column must not simply equal the light one."""
        assert CATEGORICAL_LIGHT != CATEGORICAL_DARK

    def test_status_colours_are_not_reused_as_series_colours(self):
        """A status colour impersonating a series is the failure the reserved
        palette exists to prevent."""
        assert not set(STATUS.values()) & set(CATEGORICAL_LIGHT)

    def test_the_sequential_ramp_is_ordered_light_to_dark(self):
        """Sequential encoding depends on monotone lightness; an unsorted ramp
        renders magnitude as noise."""
        luminance = [
            sum(int(c.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)) for c in SEQUENTIAL_BLUE
        ]
        assert luminance == sorted(luminance, reverse=True)

    def test_both_modes_define_every_chrome_role(self):
        assert set(ink("light")) == set(ink("dark"))


class TestRgba:
    def test_produces_a_form_plotly_accepts(self):
        """Plotly rejects 8-digit hex outright — the Sankey page 500'd on it
        until this existed."""
        assert rgba("#2a78d6", 0.4) == "rgba(42,120,214,0.4)"

    def test_tolerates_a_missing_hash(self):
        assert rgba("2a78d6", 1) == "rgba(42,120,214,1)"
