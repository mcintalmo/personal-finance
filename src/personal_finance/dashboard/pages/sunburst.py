"""Sunburst drill-down of the category hierarchy."""

from __future__ import annotations

from typing import Any

import dash
import dash_ag_grid as dag
import plotly.graph_objects as go
from dash import Input, Output, callback, dcc, html

from personal_finance.dashboard._client import ApiError, get
from personal_finance.dashboard.components import (
    GRID_CLASS,
    GRID_DEFAULTS,
    GRID_STYLE,
    empty_state,
    error_alert,
    graph,
    money_column,
    page_header,
)
from personal_finance.dashboard.theme import SEQUENTIAL_BLUE, figure_layout, ink

dash.register_page(__name__, path="/sunburst", name="Categories", icon="bi-pie-chart", order=1)

_INK = ink("light")


def layout(**_kwargs: Any) -> html.Div:
    return html.Div(
        [
            page_header(
                "Category drill-down", "Click a wedge to descend; click the centre to go back."
            ),
            dcc.RadioItems(
                id="sunburst-metric",
                options=[
                    {"label": " Outflow", "value": "total_outflow"},
                    {"label": " Inflow", "value": "total_inflow"},
                ],
                value="total_outflow",
                inline=True,
                className="mb-3",
                labelStyle={"marginRight": "1.5rem"},
            ),
            html.Div(id="sunburst-body"),
        ]
    )


@callback(Output("sunburst-body", "children"), Input("sunburst-metric", "value"))
def render(metric: str) -> Any:
    try:
        rollups = get("/categories/sunburst")
    except ApiError as exc:
        return error_alert(exc)
    if not rollups:
        return empty_state("No categories yet — run `pf init-db` and `pf transform` first.")

    values = [row[metric] for row in rollups]
    # Coloured by MAGNITUDE, not identity: a category's spend is a quantity,
    # and a categorical scheme here would both imply the wedges are unordered
    # and blow past the three-slot cap the palette validates to. One hue,
    # light to dark, is the correct encoding and scales to any depth.
    figure = go.Figure(
        go.Sunburst(
            ids=[row["category_id"] for row in rollups],
            labels=[row["name"] for row in rollups],
            parents=[row["parent_id"] or "" for row in rollups],
            values=values,
            branchvalues="total",
            marker={
                "colors": values,
                "colorscale": [[i / 9, c] for i, c in enumerate(SEQUENTIAL_BLUE)],
                "line": {"color": _INK["surface"], "width": 2},  # 2px surface gap between wedges
            },
            hovertemplate="<b>%{label}</b><br>%{value:$,.2f}<extra></extra>",
            insidetextorientation="radial",
        )
    )
    figure.update_layout(**figure_layout(margin={"t": 16, "l": 8, "r": 8, "b": 8}))

    # The table is not decoration: three light-mode slots sit below 3:1 on the
    # light surface, and the palette's relief rule requires a readable
    # alternative wherever colour alone might not carry.
    table = dag.AgGrid(
        rowData=sorted(rollups, key=lambda r: r["total_outflow"], reverse=True),
        columnDefs=[
            {"headerName": "Path", "field": "path", "flex": 2},
            {"headerName": "Depth", "field": "depth", "type": "numericColumn"},
            {"headerName": "Transactions", "field": "transaction_count", "type": "numericColumn"},
            money_column("Outflow", "total_outflow"),
            money_column("Inflow", "total_inflow"),
            money_column("Net", "net_amount"),
        ],
        defaultColDef=GRID_DEFAULTS,
        className=GRID_CLASS,
        style=GRID_STYLE,
        dashGridOptions={"pagination": True, "paginationPageSize": 15},
    )
    return html.Div([graph(figure, height=620), html.Div(table, className="mt-4")])
