"""Budget vs. actual, per configured bucket."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import dash
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import Input, Output, callback, html

from personal_finance.dashboard._client import ApiError, get
from personal_finance.dashboard.components import (
    empty_state,
    error_alert,
    graph,
    money,
    page_header,
    stat_tile,
)
from personal_finance.dashboard.theme import STATUS, categorical, figure_layout, ink

dash.register_page(__name__, path="/budgets", name="Budgets", icon="bi-bullseye", order=3)

_BLUE, _ORANGE, _ = categorical("light")
_INK = ink("light")


def layout(**_kwargs: Any) -> html.Div:
    return html.Div(
        [
            page_header(
                "Budget vs. actual",
                "Buckets are defined in budgets.yaml — edit them on the Config page.",
            ),
            html.Div(id="budgets-body"),
        ]
    )


@callback(Output("budgets-body", "children"), Input("budgets-body", "id"))
def render(_trigger: str) -> Any:
    try:
        actuals = get("/budgets")
    except ApiError as exc:
        return error_alert(exc)
    if not actuals:
        return empty_state(
            "No budgets yet — add buckets to config/budgets.yaml (see the Config page), "
            "then run `pf init-db` and `pf transform`."
        )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in actuals:
        grouped[row["name"]].append(row)

    return html.Div([_bucket(name, rows) for name, rows in grouped.items()])


def _bucket(name: str, rows: list[dict[str, Any]]) -> dbc.Card:
    periods = sorted(rows, key=lambda r: r["period_start"])
    latest = periods[-1]
    budgeted = latest["budgeted_amount"]
    # variance is actual - budgeted (matching gold_budget_actuals), so a
    # POSITIVE value means the period ran over.
    over = latest["variance"] > 0

    figure = go.Figure(
        go.Bar(
            x=[r["period_start"] for r in periods],
            y=[r["actual_outflow"] for r in periods],
            marker={
                # Status colour marks the periods that breached, and the
                # "Over budget"/"On track" tile beside it says so in words —
                # warning-orange is sub-3:1 on this surface, so it never
                # carries the meaning alone.
                "color": [STATUS["critical"] if r["variance"] > 0 else _BLUE for r in periods],
                "cornerradius": 4,
            },
            hovertemplate="%{x|%b %Y}<br>Actual %{y:$,.2f}<extra></extra>",
            name="Actual",
        )
    )
    figure.add_hline(
        y=budgeted,
        line_dash="dash",
        line_color=_INK["muted"],
        annotation_text=f"Budgeted {money(budgeted)}",
        annotation_position="top left",
        annotation_font_color=_INK["secondary"],
    )
    figure.update_layout(
        **figure_layout(
            bargap=0.35,
            yaxis={"tickprefix": "$", "title": {"text": ""}},
            xaxis={"title": {"text": ""}},
            showlegend=False,
        )
    )

    return dbc.Card(
        dbc.CardBody(
            [
                html.H2(name, className="h5 mb-3", style={"color": _INK["primary"]}),
                dbc.Row(
                    [
                        dbc.Col(stat_tile(f"Budget ({latest['period']})", money(budgeted)), md=4),
                        dbc.Col(
                            stat_tile("Actual (latest)", money(latest["actual_outflow"])), md=4
                        ),
                        dbc.Col(
                            stat_tile(
                                "Status",
                                f"Over by {money(abs(latest['variance']))}"
                                if over
                                else f"Under by {money(abs(latest['variance']))}",
                                tone=STATUS["critical"] if over else STATUS["good"],
                            ),
                            md=4,
                        ),
                    ],
                    class_name="g-3 mb-3",
                ),
                graph(figure, height=320),
            ]
        ),
        class_name="border-0 shadow-sm mb-4",
    )
