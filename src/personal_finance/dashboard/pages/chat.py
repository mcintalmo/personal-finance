"""Chat: ask the agent a question, watch the answer arrive.

This is the page Dash exists for. The answer streams token by token over a
Dash **websocket callback**, which is the one thing Streamlit's re-run model
could not express — a persistent connection the server pushes into while the
callback is still running.

The mechanism, end to end:

1. The callback opens a streaming POST to ``/agent`` — the AG-UI endpoint from
   stage B, unchanged, the same one a React frontend would use.
2. Each SSE frame is folded into a :class:`ChatTurn` by
   :mod:`personal_finance.dashboard.agent_stream`, which knows nothing about
   Dash and is tested on recorded frames.
3. ``set_props`` pushes the growing text to the browser after every frame,
   before the callback returns.

Tool calls are surfaced as they happen rather than hidden. A local model
spends its first 30 seconds picking a tool, and "Thinking…" for half a minute
reads as a hang; "Using spend_by_category…" reads as work. It is also the
honest thing to show — the user can see which numbers the answer rests on.
"""

from __future__ import annotations

import uuid
from typing import Any

import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, dcc, html, set_props

from personal_finance.config import get_settings
from personal_finance.dashboard.agent_stream import ChatTurn, stream_answer
from personal_finance.dashboard.theme import ink

dash.register_page(__name__, path="/chat", name="Chat", icon="bi-chat-dots", order=7)

_INK = ink("light")

SUGGESTIONS = (
    "How much did I spend on groceries last month?",
    "What are my recurring subscriptions?",
    "Which merchant did I spend the most with?",
    "Am I on track against my budgets?",
)


def _agent_url() -> str:
    return f"{get_settings().serving.api_url}/agent"


def layout(**_kwargs: Any) -> html.Div:
    return html.Div(
        [
            html.H1("Chat", className="h3 mb-1", style={"color": _INK["primary"]}),
            html.P(
                "Answered only from your warehouse, through read-only tools.",
                style={"color": _INK["secondary"]},
            ),
            dbc.Card(
                dbc.CardBody(
                    [
                        html.Div(id="chat-question", className="fw-semibold mb-2"),
                        html.Div(
                            id="chat-status",
                            className="small font-monospace mb-2",
                            style={"color": _INK["muted"]},
                        ),
                        dcc.Markdown(
                            id="chat-answer",
                            children="",
                            className="mb-0",
                            style={"color": _INK["primary"]},
                        ),
                    ]
                ),
                class_name="border-0 shadow-sm mb-3",
                id="chat-card",
                style={"minHeight": "12rem"},
            ),
            dbc.InputGroup(
                [
                    dbc.Input(
                        id="chat-input",
                        placeholder="Ask about your spending…",
                        debounce=True,
                        autoFocus=True,
                    ),
                    dbc.Button("Ask", id="chat-ask", color="primary"),
                ],
                class_name="mb-3",
            ),
            html.Div(
                [
                    dbc.Button(
                        text,
                        id={"type": "chat-suggestion", "index": i},
                        color="light",
                        size="sm",
                        class_name="me-2 mb-2 border",
                    )
                    for i, text in enumerate(SUGGESTIONS)
                ]
            ),
        ]
    )


@callback(
    Output("chat-input", "value"),
    Input({"type": "chat-suggestion", "index": dash.ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def use_suggestion(_clicks: list[int | None]) -> Any:
    """Fill the box from a suggestion chip. Does not submit — the user may edit."""
    triggered = dash.ctx.triggered_id
    if not triggered:
        return dash.no_update
    return SUGGESTIONS[triggered["index"]]


@callback(
    Output("chat-answer", "children"),
    Input("chat-ask", "n_clicks"),
    Input("chat-input", "n_submit"),
    State("chat-input", "value"),
    prevent_initial_call=True,
    websocket=True,
)
async def ask(_clicks: int | None, _submits: int | None, question: str | None) -> str:
    """Stream one answer.

    ``websocket=True`` is what allows `set_props` to push mid-flight; over a
    plain HTTP callback the browser would see nothing until the whole answer
    was finished, which for a 60-90s local model is indistinguishable from a
    hung page.
    """
    question = (question or "").strip()
    if not question:
        return ""

    set_props("chat-question", {"children": question})
    set_props("chat-status", {"children": "Thinking…"})
    set_props("chat-answer", {"children": ""})

    turn = ChatTurn()
    async for turn in stream_answer(
        question,
        agent_url=_agent_url(),
        thread_id="dash",
        run_id=uuid.uuid4().hex,
    ):
        set_props("chat-status", {"children": _status_line(turn)})
        if turn.text:
            set_props("chat-answer", {"children": turn.text})

    set_props("chat-status", {"children": _status_line(turn)})
    if turn.error:
        # Rendered where the answer would be, not swallowed into the status
        # line: "the tool server is down" is the whole message, and it names
        # the command that fixes it.
        return f"**The agent could not answer.**\n\n{turn.error}"
    return turn.text or "_The agent returned no text._"


def _status_line(turn: ChatTurn) -> str:
    """Show the route taken, not just that something is happening."""
    if turn.error:
        return "Failed"
    if turn.tools:
        trail = " → ".join(turn.tools)
        return trail if turn.finished else f"{trail} …"
    return turn.status
