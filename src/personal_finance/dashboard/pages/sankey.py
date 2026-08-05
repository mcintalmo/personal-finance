"""Sankey of money flow: income -> account -> top-level category."""

from __future__ import annotations

from typing import Any

import dash
import plotly.graph_objects as go
from dash import Input, Output, callback, html

from personal_finance.dashboard._client import ApiError, get
from personal_finance.dashboard.components import empty_state, error_alert, graph, page_header
from personal_finance.dashboard.theme import categorical, figure_layout, ink, rgba

dash.register_page(__name__, path="/sankey", name="Money flow", icon="bi-diagram-3", order=2)

_BLUE, _ORANGE, _ = categorical("light")
_INK = ink("light")


def layout(**_kwargs: Any) -> html.Div:
    return html.Div(
        [
            page_header("Money flow", "Income into accounts, and out to categories."),
            html.Div(id="sankey-body"),
        ]
    )


@callback(Output("sankey-body", "children"), Input("sankey-body", "id"))
def render(_trigger: str) -> Any:
    try:
        edges = get("/sankey")
    except ApiError as exc:
        return error_alert(exc)
    if not edges:
        return empty_state("No flow yet — run `pf transform` after ingesting some data.")

    nodes = sorted({e["source_node"] for e in edges} | {e["target_node"] for e in edges})
    index = {name: i for i, name in enumerate(nodes)}
    # Two categorical slots, carrying the two stages the mart already labels:
    # income->account and account->category. Colour follows the stage (the
    # entity), never the link's rank, so filtering cannot repaint survivors.
    link_colours = [rgba(_BLUE if edge["stage"] == "income" else _ORANGE, 0.4) for edge in edges]

    figure = go.Figure(
        go.Sankey(
            node={
                "label": nodes,
                "pad": 18,
                "thickness": 14,
                "color": _INK["muted"],
                "line": {"color": _INK["surface"], "width": 2},
                "hovertemplate": "%{label}<br>%{value:$,.2f}<extra></extra>",
            },
            link={
                "source": [index[e["source_node"]] for e in edges],
                "target": [index[e["target_node"]] for e in edges],
                "value": [e["value"] for e in edges],
                "color": link_colours,
                "hovertemplate": (
                    "%{source.label} → %{target.label}<br>%{value:$,.2f}<extra></extra>"
                ),
            },
        )
    )
    figure.update_layout(**figure_layout(margin={"t": 16, "l": 8, "r": 8, "b": 8}))

    legend = html.Div(
        [
            html.Span(
                [
                    html.Span(
                        style={
                            "display": "inline-block",
                            "width": "12px",
                            "height": "12px",
                            "borderRadius": "3px",
                            "backgroundColor": colour,
                            "marginRight": "0.4rem",
                        }
                    ),
                    label,
                ],
                className="me-4 small d-inline-flex align-items-center",
                style={"color": _INK["secondary"]},
            )
            for colour, label in ((_BLUE, "Income → account"), (_ORANGE, "Account → category"))
        ],
        className="mb-2",
    )
    # Two series means a legend is always present. Plotly's Sankey draws none
    # of its own, so identity would otherwise rest on colour alone.
    return html.Div([legend, graph(figure, height=600)])
