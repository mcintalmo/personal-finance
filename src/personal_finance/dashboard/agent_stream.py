"""Consume the agent's AG-UI event stream (Phase 7, stage C).

The Dash chat page talks to ``POST /agent`` — the same AG-UI endpoint a React
frontend would use — rather than to a second, Dash-shaped endpoint. One
contract means the agent surface built in stage B is the only one to keep
correct, and a later frontend swap needs no backend change.

The event shapes below were captured off the wire against a live run rather
than read off a spec, because the field names are not guessable: text arrives
as ``delta`` on ``TEXT_MESSAGE_CONTENT``, but a tool's *name* arrives as
``toolCallName`` on ``TOOL_CALL_START`` while its *arguments* arrive as a
``delta`` on a separate ``TOOL_CALL_ARGS`` event keyed by ``toolCallId``. One
real question produced 119 text deltas, so this genuinely streams token by
token — which is the whole reason the chat page does not just wait for a
finished answer. A local model takes 60-90s to reply, and that is the
difference between watching an answer appear and staring at a spinner.

Parsing is kept here, apart from any Dash import, so it can be tested against
recorded frames without a browser or a running agent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

logger = logging.getLogger(__name__)

_DATA_PREFIX = "data: "


@dataclass
class ChatTurn:
    """The state of one question, updated in place as events arrive."""

    text: str = ""
    tools: list[str] = field(default_factory=list)
    error: str | None = None
    finished: bool = False

    @property
    def status(self) -> str:
        """A one-line description of what the agent is doing right now.

        Shown while the answer is still empty. A local model spends its first
        30 seconds deciding which tool to call, and "Thinking…" for half a
        minute reads as a hang; naming the tool shows it is working.
        """
        if self.error:
            return "Failed"
        if self.finished:
            return "Done"
        if self.tools:
            return f"Using {self.tools[-1]}…"
        return "Thinking…"


def parse_sse_line(line: str) -> dict[str, Any] | None:
    """Decode one SSE line, or None if it carries no event.

    An SSE stream is mostly blank lines and comments; only ``data:`` frames
    matter. A frame that is not JSON is skipped rather than raised on — the
    stream is still live at that point, and killing the whole answer over one
    malformed frame would lose the text already received.
    """
    if not line.startswith(_DATA_PREFIX):
        return None
    try:
        event = json.loads(line[len(_DATA_PREFIX) :])
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def apply_event(turn: ChatTurn, event: dict[str, Any]) -> ChatTurn:
    """Fold one AG-UI event into the turn.

    Unknown event types are ignored on purpose: AG-UI has more of them than
    this page renders (message lifecycle, state deltas, step boundaries), and
    a new one appearing upstream should be inert here rather than an error.
    """
    match event.get("type"):
        case "TEXT_MESSAGE_CONTENT":
            turn.text += str(event.get("delta") or "")
        case "TOOL_CALL_START":
            name = event.get("toolCallName")
            if name:
                turn.tools.append(str(name))
        case "RUN_ERROR":
            # The agent failed mid-stream. Its own message is the useful
            # thing — this is where "the tool server went away" surfaces.
            turn.error = str(event.get("message") or "The agent run failed.")
            turn.finished = True
        case "RUN_FINISHED":
            outcome = event.get("outcome") or {}
            if isinstance(outcome, dict) and outcome.get("type") not in (None, "success"):
                turn.error = str(outcome.get("message") or f"Run ended: {outcome.get('type')}")
            turn.finished = True
    return turn


def run_input(question: str, *, thread_id: str, run_id: str) -> dict[str, Any]:
    """The AG-UI request body for one question."""
    return {
        "threadId": thread_id,
        "runId": run_id,
        "messages": [{"id": run_id, "role": "user", "content": question}],
        "tools": [],
        "context": [],
        "state": None,
        "forwardedProps": None,
    }


def replay(turn: ChatTurn, lines: Iterable[str]) -> ChatTurn:
    """Fold a sequence of raw SSE lines into a turn. Used by tests."""
    for line in lines:
        event = parse_sse_line(line)
        if event is not None:
            apply_event(turn, event)
    return turn


async def stream_answer(
    question: str, *, agent_url: str, thread_id: str, run_id: str, timeout: float = 300.0
) -> AsyncIterator[ChatTurn]:
    """Yield the turn after every event that changed it.

    Yields the same mutable :class:`ChatTurn` each time rather than a copy —
    the caller renders it immediately and does not retain it, and copying a
    growing string 119 times per answer is waste for no gain.

    A transport failure becomes a turn carrying `error` rather than an
    exception, because the caller is a UI callback: an exception there is a
    blank page, while an error on the turn is a message the user can act on.
    """
    body = run_input(question, thread_id=thread_id, run_id=run_id)
    turn = ChatTurn()
    try:
        async with (
            httpx.AsyncClient(timeout=timeout) as client,
            client.stream("POST", agent_url, json=body) as response,
        ):
            if response.status_code != 200:
                # Read the body before raising: the 503 from `POST /agent`
                # names the command that starts the missing tool server, and
                # discarding it would leave the user with a bare status code.
                await response.aread()
                turn.error = _http_detail(response)
                turn.finished = True
                yield turn
                return
            async for line in response.aiter_lines():
                event = parse_sse_line(line)
                if event is None:
                    continue
                yield apply_event(turn, event)
    except httpx.TimeoutException:
        turn.error = f"The agent did not answer within {timeout:g}s."
        turn.finished = True
        yield turn
    except httpx.TransportError as exc:
        turn.error = f"Can't reach the agent at {agent_url} — is `pf serve` running? ({exc})"
        turn.finished = True
        yield turn
    except Exception as exc:
        # Deliberately broad, and load-bearing. The caller is a UI callback
        # with no error path of its own: anything escaping here leaves the
        # page reading "Thinking…" forever with no message anywhere, which is
        # the exact silent failure this module claims to have designed out.
        # httpx.InvalidURL from a malformed api_url is not a TransportError,
        # so the two narrow clauses above genuinely do not cover it.
        logger.warning("Agent stream failed", exc_info=True)
        turn.error = f"The agent stream failed: {type(exc).__name__}: {exc}"
        turn.finished = True
        yield turn


def _http_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or f"The agent returned HTTP {response.status_code}."
    if isinstance(payload, dict) and payload.get("detail"):
        return str(payload["detail"])
    return f"The agent returned HTTP {response.status_code}."
