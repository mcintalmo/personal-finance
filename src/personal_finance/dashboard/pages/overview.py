"""Overview: net flow, spend over time, top movers."""

from __future__ import annotations

from typing import Any

import dash
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import Input, Output, callback, dcc, html

from personal_finance.dashboard._client import ApiError, get, get_optional
from personal_finance.dashboard.components import (
    callout_card,
    empty_state,
    error_alert,
    graph,
    money,
    page_header,
    stat_tile,
)
from personal_finance.dashboard.theme import categorical, figure_layout

dash.register_page(__name__, path="/", name="Overview", icon="bi-speedometer2", order=0)

_BLUE, _ORANGE, _ = categorical("light")


def layout(**_kwargs: Any) -> html.Div:
    return html.Div(
        [
            page_header("Overview", "Where the money went, and what changed."),
            html.Div(id="overview-callouts"),
            html.Div(id="overview-body"),
            html.Hr(className="my-4"),
            dbc.Row(
                [
                    dbc.Col(html.Label("Top merchants", className="small fw-semibold"), md="auto"),
                    dbc.Col(
                        dcc.Slider(
                            id="overview-top-n",
                            min=5,
                            max=30,
                            step=5,
                            value=10,
                            marks={n: str(n) for n in (5, 10, 20, 30)},
                            tooltip={"placement": "bottom"},
                        ),
                        md=5,
                    ),
                ],
                class_name="align-items-center g-3 mb-2",
            ),
            html.Div(id="overview-merchants"),
        ]
    )


@callback(Output("overview-callouts", "children"), Input("overview-body", "id"))
def render_callouts(_trigger: str) -> Any:
    """The three most notable callouts, above the charts.

    The point of a callout is that the user sees it without going looking.
    `get_optional` so a warehouse built before the callout marts existed
    still renders the page the user actually came for.
    """
    try:
        feed = get_optional("/callouts", limit=3)
    except ApiError as exc:
        return error_alert(exc)
    if feed is None:
        return None
    # Linked whenever the feed came back at all, not only when it had
    # something to say: an empty band and a broken endpoint look identical
    # here, and the Callouts page is where the real error is visible.
    return html.Div(
        [
            *[callout_card(callout) for callout in feed.get("callouts") or []],
            dcc.Link("See all callouts →", href="/callouts", className="small"),
        ],
        className="mb-4",
    )


@callback(Output("overview-body", "children"), Input("overview-body", "id"))
def render_body(_trigger: str) -> Any:
    # Merchants are fetched by a SEPARATE callback below. They come from
    # silver while everything here comes from gold, so they fail
    # independently — sharing one try/except would let a missing
    # silver_merchants blank the tiles and the flow chart that had loaded fine.
    try:
        overview = get("/overview")
    except ApiError as exc:
        return error_alert(exc)

    tiles = dbc.Row(
        [
            dbc.Col(stat_tile("Total inflow", money(overview["total_inflow"])), md=4),
            dbc.Col(stat_tile("Total outflow", money(overview["total_outflow"])), md=4),
            dbc.Col(stat_tile("Net", money(overview["net_amount"])), md=4),
        ],
        class_name="g-3 mb-4",
    )

    months = overview.get("months") or []
    if months:
        flow = go.Figure()
        # Two series, so a legend is always present. Rounded data-ends and a
        # gap between the paired bars keep the two readable as one group.
        flow.add_bar(
            x=[m["month"] for m in months],
            y=[m["total_inflow"] for m in months],
            name="Inflow",
            marker={"color": _BLUE, "cornerradius": 4},
            hovertemplate="%{x|%b %Y}<br>Inflow %{y:$,.2f}<extra></extra>",
        )
        flow.add_bar(
            x=[m["month"] for m in months],
            y=[m["total_outflow"] for m in months],
            name="Outflow",
            marker={"color": _ORANGE, "cornerradius": 4},
            hovertemplate="%{x|%b %Y}<br>Outflow %{y:$,.2f}<extra></extra>",
        )
        flow.update_layout(
            **figure_layout(
                barmode="group",
                bargap=0.3,
                bargroupgap=0.08,
                title="Money in and out, by month",
                yaxis={"tickprefix": "$", "title": {"text": ""}},
                hovermode="x unified",
            )
        )
        flow_block = graph(flow)
    else:
        flow_block = empty_state("No monthly flow yet — run `pf transform` after ingesting data.")

    return html.Div([tiles, flow_block])


@callback(Output("overview-merchants", "children"), Input("overview-top-n", "value"))
def render_merchants(top_n: int) -> Any:
    try:
        merchants = get("/merchants/top", limit=top_n)
    except ApiError as exc:
        return error_alert(exc)
    if merchants:
        ordered = sorted(merchants, key=lambda m: m["total_outflow"])
        bars = go.Figure(
            go.Bar(
                x=[m["total_outflow"] for m in ordered],
                y=[m["merchant_name"] for m in ordered],
                orientation="h",
                marker={"color": _BLUE, "cornerradius": 4},
                hovertemplate="%{y}<br>%{x:$,.2f}<extra></extra>",
            )
        )
        # One series, so no legend — the title names it.
        bars.update_layout(
            **figure_layout(
                title="Top merchants by spend",
                xaxis={"tickprefix": "$"},
                yaxis={"title": {"text": ""}},
            )
        )
        return graph(bars, height=460)
    return empty_state("No merchant activity yet — run `pf transform`.")
