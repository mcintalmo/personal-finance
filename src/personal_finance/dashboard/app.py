"""The Dash app: shell, navigation and theme (Phase 7, stage C).

Replaces the Streamlit app. Streamlit was the right way to get a dashboard on
screen in Phase 6, but its model — re-run the script on every interaction —
puts a ceiling on what the UI can do, and the chat page is where that ceiling
bites: streaming an answer token by token needs the server to push into a
live page, which a re-run cannot express.

``backend="fastapi"`` with ``websocket_callbacks=True`` is what makes that
possible. Dash 4 runs its callbacks over a persistent WebSocket when asked,
and inside one an async callback can call ``set_props`` repeatedly to push
partial updates before it returns. That is the mechanism the chat page uses;
every other page is an ordinary HTTP callback.

Pages are registered from ``dashboard/pages/`` via Dash's own page registry,
so the nav is built from what actually exists rather than from a hand-kept
list that can drift.
"""

from __future__ import annotations

import dash
import dash_bootstrap_components as dbc
from dash import Dash, dcc, html

from personal_finance.dashboard.theme import ink, register_templates

register_templates()

app = Dash(
    __name__,
    backend="fastapi",
    websocket_callbacks=True,
    use_pages=True,
    pages_folder="pages",
    external_stylesheets=[dbc.themes.BOOTSTRAP, dbc.icons.BOOTSTRAP],
    title="personal-finance",
    suppress_callback_exceptions=True,
)

_LIGHT = ink("light")


def _nav() -> dbc.Nav:
    """Build the sidebar from the page registry, in declared order."""
    pages = sorted(dash.page_registry.values(), key=lambda page: page.get("order", 99))
    return dbc.Nav(
        [
            dbc.NavLink(
                [html.I(className=f"bi {page['icon']} me-2"), page["name"]],
                href=page["relative_path"],
                active="exact",
                class_name="rounded-2 mb-1",
            )
            for page in pages
        ],
        vertical=True,
        pills=True,
    )


def serve_layout() -> html.Div:
    """Built per request so the nav reflects pages registered at import time."""
    return html.Div(
        [
            dcc.Location(id="url"),
            dbc.Row(
                [
                    dbc.Col(
                        html.Div(
                            [
                                html.Div(
                                    [
                                        html.I(className="bi bi-wallet2 me-2"),
                                        html.Span("personal-finance", className="fw-semibold"),
                                    ],
                                    className="fs-5 mb-4 d-flex align-items-center",
                                ),
                                _nav(),
                            ],
                            className="p-3 h-100",
                            style={"borderRight": f"1px solid {_LIGHT['border']}"},
                        ),
                        width="auto",
                        style={"minWidth": "15rem"},
                    ),
                    dbc.Col(dash.page_container, class_name="p-4"),
                ],
                class_name="g-0 min-vh-100 flex-nowrap",
            ),
        ],
        style={"backgroundColor": _LIGHT["plane"], "minHeight": "100vh"},
    )


app.layout = serve_layout

# The ASGI app uvicorn serves. Exposed by name so `pf dashboard` can hand
# uvicorn an import string, the same shape `pf serve` uses — Dash's own
# `app.run()` derives that string from its calling module, which resolves to
# the CLI rather than to this module and fails to import.
server = app.server
