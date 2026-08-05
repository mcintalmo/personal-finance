"""Review queue: approve or correct what the cascade declined to guess on.

Mirrors `pf review list` / `pf review label`. The category field is a dropdown
over the real taxonomy rather than the Streamlit page's free-text box — a
typo there was a 404 after the fact, which is a poor way to learn you spelled
a category wrong.
"""

from __future__ import annotations

from typing import Any

import dash
import dash_ag_grid as dag
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, dcc, html, no_update

from personal_finance.dashboard._client import ApiError, get, post
from personal_finance.dashboard.components import (
    GRID_CLASS,
    GRID_DEFAULTS,
    GRID_STYLE,
    empty_state,
    error_alert,
    page_header,
)
from personal_finance.dashboard.theme import ink

dash.register_page(__name__, path="/review", name="Review queue", icon="bi-check2-square", order=5)

_INK = ink("light")


def layout(**_kwargs: Any) -> html.Div:
    return html.Div(
        [
            page_header("Review queue", "The categorization cascade's ambiguous tail."),
            dbc.Row(
                [
                    dbc.Col(
                        dcc.RadioItems(
                            id="review-kind",
                            options=[
                                {"label": " Transactions", "value": "transaction"},
                                {"label": " Splits", "value": "split"},
                            ],
                            value="transaction",
                            inline=True,
                            labelStyle={"marginRight": "1.5rem"},
                        ),
                        md="auto",
                    ),
                    dbc.Col(
                        dcc.Slider(
                            id="review-limit",
                            min=5,
                            max=50,
                            step=5,
                            value=20,
                            marks={n: str(n) for n in (5, 20, 35, 50)},
                            tooltip={"placement": "bottom"},
                        ),
                        md=5,
                    ),
                ],
                class_name="align-items-center mb-3 g-3",
            ),
            html.Div(id="review-body"),
            html.Div(id="review-form", className="mt-4"),
            html.Div(id="review-result", className="mt-3"),
            # Bumped only on a SUCCESSFUL label. The form lives inside
            # `review-form`, so anything that rebuilds it clears the user's
            # selections — and rebuilding on every outcome meant "pick a
            # category" also wiped the item they had already picked, and a
            # failed POST cost them the whole entry exactly when retrying
            # mattered most.
            dcc.Store(id="review-labeled", data=0),
        ]
    )


@callback(
    Output("review-body", "children"),
    Output("review-form", "children"),
    Input("review-kind", "value"),
    Input("review-limit", "value"),
    Input("review-labeled", "data"),
)
def render(kind: str, limit: int, _labeled: int) -> tuple[Any, Any]:
    """Re-reads the queue whenever a label lands, so the item just handled leaves."""
    try:
        queue = get("/review/queue", kind=kind, limit=limit)
        paths = _category_paths()
    except ApiError as exc:
        return error_alert(exc), None
    if not queue:
        return empty_state("Nothing waiting for review."), None

    # Columns are derived from the first row: the two queue kinds return
    # different shapes, and a hardcoded list would silently drop a column
    # when either changes.
    # Columns are derived from the first row: the two queue kinds return
    # different shapes, and a hardcoded list would silently drop a column when
    # either changes.
    table = dag.AgGrid(
        rowData=queue,
        columnDefs=[
            {"headerName": key.replace("_", " ").title(), "field": key} for key in queue[0]
        ],
        defaultColDef=GRID_DEFAULTS,
        className=GRID_CLASS,
        style=GRID_STYLE,
        dashGridOptions={"pagination": True, "paginationPageSize": 10},
    )

    form = dbc.Card(
        dbc.CardBody(
            [
                html.H2("Label an item", className="h6 mb-3"),
                dbc.Row(
                    [
                        dbc.Col(
                            dcc.Dropdown(
                                id="review-subject",
                                options=[
                                    {"label": _describe(item), "value": item["subject_id"]}
                                    for item in queue
                                ],
                                placeholder="Item",
                            ),
                            md=5,
                        ),
                        dbc.Col(
                            dcc.Dropdown(
                                id="review-category",
                                options=[{"label": p, "value": p} for p in paths],
                                placeholder="Category",
                            ),
                            md=4,
                        ),
                        dbc.Col(dbc.Input(id="review-note", placeholder="Note (optional)"), md=3),
                    ],
                    class_name="g-2 mb-3",
                ),
                dbc.Button("Submit correction", id="review-submit", color="primary"),
            ]
        ),
        class_name="border-0 shadow-sm",
    )
    return table, form


def _describe(item: dict[str, Any]) -> str:
    """A label a human can pick from — an opaque id is not a choice."""
    if item.get("kind") == "split":
        return f"{item.get('product_name', '?')} — ${abs(item.get('amount', 0)):,.2f}"
    return (
        f"{item.get('posted_on', '')} · {item.get('merchant_name') or item.get('description_raw')}"
        f" — ${abs(item.get('amount', 0)):,.2f}"
    )


def _category_paths() -> list[str]:
    """Every category path, for the dropdown. Read from the sunburst mart,
    which is the only endpoint that already exposes the full taxonomy."""
    return sorted(row["path"] for row in get("/categories/sunburst"))


@callback(
    Output("review-result", "children"),
    Output("review-labeled", "data"),
    Input("review-submit", "n_clicks"),
    State("review-kind", "value"),
    State("review-subject", "value"),
    State("review-category", "value"),
    State("review-note", "value"),
    State("review-labeled", "data"),
    prevent_initial_call=True,
)
def submit(
    _clicks: int,
    kind: str,
    subject_id: str,
    category: str,
    note: str | None,
    labeled: int,
) -> tuple[Any, Any]:
    if not subject_id or not category:
        return dbc.Alert("Pick both an item and a category.", color="warning"), no_update
    try:
        result = post(
            "/review/label",
            {
                "kind": kind,
                "subject_id": subject_id,
                "category_path": category,
                "note": note or None,
            },
        )
    except ApiError as exc:
        return error_alert(exc), no_update
    return (
        dbc.Alert(f"Labeled {result['subject_id']} → {category}.", color="success", duration=6000),
        (labeled or 0) + 1,
    )
