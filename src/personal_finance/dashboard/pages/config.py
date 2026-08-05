"""Config editing for the user-editable YAML files.

A write re-validates the WHOLE configuration — cross-file referential
integrity, not just this file's own schema — before touching disk. See
`personal_finance.user_config.write_config_file`. Changes take effect after
re-running `pf init-db` / `pf transform`, which the success message says,
because a config edit that appears to do nothing is otherwise baffling.
"""

from __future__ import annotations

from typing import Any

import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, dcc, html

from personal_finance.dashboard._client import ApiError, get, put
from personal_finance.dashboard.components import error_alert, page_header
from personal_finance.dashboard.theme import ink

dash.register_page(__name__, path="/config", name="Config", icon="bi-sliders", order=6)

_INK = ink("light")


def layout(**_kwargs: Any) -> html.Div:
    try:
        names = get("/config")
    except ApiError as exc:
        return html.Div([page_header("Config editor"), error_alert(exc)])
    return html.Div(
        [
            page_header("Config editor", "Edited here, applied by `pf init-db` / `pf transform`."),
            dcc.Dropdown(
                id="config-name",
                options=[{"label": f"{n}.yaml", "value": n} for n in names],
                value=names[0] if names else None,
                className="mb-3",
                style={"maxWidth": "24rem"},
            ),
            dcc.Textarea(
                id="config-content",
                style={
                    "width": "100%",
                    "height": "28rem",
                    "fontFamily": "ui-monospace, SFMono-Regular, Menlo, monospace",
                    "fontSize": "13px",
                    "border": f"1px solid {_INK['border']}",
                    "borderRadius": "0.5rem",
                    "padding": "0.75rem",
                },
            ),
            html.Div(
                [dbc.Button("Save", id="config-save", color="primary")],
                className="mt-3",
            ),
            html.Div(id="config-result", className="mt-3"),
        ]
    )


@callback(Output("config-content", "value"), Input("config-name", "value"))
def load(name: str | None) -> str:
    if not name:
        return ""
    try:
        return get(f"/config/{name}")["content"]
    except ApiError as exc:
        return f"# Could not load {name}.yaml: {exc}"


@callback(
    Output("config-result", "children"),
    Input("config-save", "n_clicks"),
    State("config-name", "value"),
    State("config-content", "value"),
    prevent_initial_call=True,
)
def save(_clicks: int, name: str | None, content: str) -> Any:
    if not name:
        return dbc.Alert("Pick a config file first.", color="warning")
    try:
        put(f"/config/{name}", {"name": name, "content": content})
    except ApiError as exc:
        # A 400 here is the config validator rejecting the edit, and its
        # message names the offending field — the single most useful thing
        # to show, so it is passed through rather than replaced.
        return error_alert(exc)
    return dbc.Alert(
        f"Saved {name}.yaml. Run `pf init-db` / `pf transform` to apply it.",
        color="success",
        duration=8000,
    )
