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
from dash.exceptions import PreventUpdate

from personal_finance.dashboard._client import api_url
from personal_finance.dashboard.agent_stream import ChatTurn, stream_answer
from personal_finance.dashboard.theme import ink

dash.register_page(__name__, path="/chat", name="Chat", icon="bi-chat-dots", order=7)

_INK = ink("light")

# mount token -> the run currently allowed to paint that page. Module-level
# because a websocket callback has nowhere else to keep per-page state, and
# this app is single-user and local.
_ACTIVE_RUN: dict[str, str] = {}

SUGGESTIONS = (
    "How much did I spend on groceries last month?",
    "What are my recurring subscriptions?",
    "Which merchant did I spend the most with?",
    "Am I on track against my budgets?",
)


def _agent_url() -> str:
    """Via `_client.api_url`, so `pf dashboard --api-url` moves the chat page too.

    Reading `settings.serving.api_url` directly would leave chat pointed at
    the default while the seven data pages followed the override — and if a
    second `pf serve` happened to be running there, the answers would come
    from a different warehouse than the charts on screen, with nothing
    on the page saying so.
    """
    return f"{api_url()}/agent"


def layout(**_kwargs: Any) -> html.Div:
    return html.Div(
        [
            # Regenerated on every mount. A run that outlives its page —
            # because the user navigated away and back — sees a token that no
            # longer matches and stops pushing, instead of filling the fresh
            # page with an abandoned answer. Dash only cancels websocket
            # callbacks when the *connection* drops, and SPA navigation does
            # not drop it.
            dcc.Store(id="chat-mount", data=uuid.uuid4().hex),
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
    Output("chat-ask", "disabled"),
    Input("chat-ask", "n_clicks"),
    Input("chat-input", "n_submit"),
    State("chat-input", "value"),
    State("chat-mount", "data"),
    prevent_initial_call=True,
    websocket=True,
)
async def ask(
    _clicks: int | None, _submits: int | None, question: str | None, mount: str
) -> tuple[Any, bool]:
    """Stream one answer.

    ``websocket=True`` is what allows `set_props` to push mid-flight; over a
    plain HTTP callback the browser would see nothing until the whole answer
    was finished, which for a 60-90s local model is indistinguishable from a
    hung page.
    """
    question = (question or "").strip()
    if not question:
        # Not `return ""`: that clears the answer while leaving the previous
        # question and status on screen, so the page reads as "the agent
        # returned nothing for this" — a failure the agent never had.
        raise PreventUpdate

    # `_ACTIVE_MOUNT` fences two things at once. A second click while a run is
    # in flight claims the slot, so the first run stops pushing instead of
    # interleaving its text with the second's — Dash runs each websocket
    # callback as its own task and does not serialise them. And a run whose
    # page was unmounted sees a stale token and stops too.
    run = uuid.uuid4().hex
    _ACTIVE_RUN[mount] = run

    def current() -> bool:
        return _ACTIVE_RUN.get(mount) == run

    set_props("chat-question", {"children": question})
    set_props("chat-status", {"children": "Thinking…"})
    set_props("chat-answer", {"children": ""})

    turn = ChatTurn()
    async for turn in stream_answer(
        question,
        agent_url=_agent_url(),
        thread_id="dash",
        run_id=run,
    ):
        if not current():
            # Superseded. Leave the screen to whoever owns it now.
            return dash.no_update, False
        set_props("chat-status", {"children": _status_line(turn)})
        if turn.text:
            set_props("chat-answer", {"children": turn.text})

    if not current():
        return dash.no_update, False
    set_props("chat-status", {"children": _status_line(turn)})
    if turn.error:
        # Rendered where the answer would be, not swallowed into the status
        # line: "the tool server is down" is the whole message, and it names
        # the command that fixes it.
        return f"**The agent could not answer.**\n\n{turn.error}", False
    return turn.text or "_The agent returned no text._", False


def _status_line(turn: ChatTurn) -> str:
    """Show the route taken, not just that something is happening."""
    if turn.error:
        return "Failed"
    if turn.tools:
        trail = " → ".join(turn.tools)
        return trail if turn.finished else f"{trail} …"
    return turn.status
