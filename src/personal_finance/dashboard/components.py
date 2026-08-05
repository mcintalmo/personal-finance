"""Shared page furniture: headers, stat tiles, alerts, callout cards.

Kept in one place so the eight pages look like one product rather than eight,
and so the accessibility rules the palette depends on are applied once. In
particular: a status colour never carries meaning alone — every callout and
every over-budget marker ships an icon and a word beside the colour, because
two of the four status steps are deliberately sub-3:1 on the light surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import dash_bootstrap_components as dbc
from dash import dcc, html

from personal_finance.dashboard.theme import INFORMATIONAL, STATUS, ink

if TYPE_CHECKING:
    from personal_finance.dashboard._client import ApiError

_INK = ink("light")

# CalloutLevel -> (status slot, bootstrap alert colour, icon, spoken label).
# The spoken label is what makes this readable without colour.
LEVEL_STYLES = {
    "critical": (STATUS["critical"], "danger", "bi-exclamation-octagon-fill", "Critical"),
    "warning": (STATUS["warning"], "warning", "bi-exclamation-triangle-fill", "Warning"),
    "info": (INFORMATIONAL, "light", "bi-info-circle", "Info"),
}


def page_header(title: str, subtitle: str | None = None) -> html.Div:
    return html.Div(
        [
            html.H1(title, className="h3 mb-1", style={"color": _INK["primary"]}),
            html.P(subtitle, className="mb-0", style={"color": _INK["secondary"]})
            if subtitle
            else None,
        ],
        className="mb-4",
    )


def error_alert(exc: ApiError) -> dbc.Alert:
    """Render a failed API call as something the user can act on.

    Never rendered as an empty chart: "the request failed" and "there is no
    data" look identical on screen and mean opposite things.
    """
    return dbc.Alert(
        [html.I(className="bi bi-exclamation-triangle-fill me-2"), str(exc)],
        color="danger",
        class_name="d-flex align-items-center",
    )


def empty_state(message: str) -> dbc.Alert:
    """A genuinely empty result, distinct in wording and colour from a failure."""
    return dbc.Alert(
        [html.I(className="bi bi-info-circle me-2"), message],
        color="light",
        class_name="d-flex align-items-center border",
    )


def stat_tile(label: str, value: str, *, tone: str | None = None) -> dbc.Card:
    """One headline figure.

    Proportional figures rather than tabular: these are standalone numbers,
    not a column that has to align vertically.
    """
    return dbc.Card(
        dbc.CardBody(
            [
                html.Div(label, className="small text-uppercase", style={"color": _INK["muted"]}),
                html.Div(
                    value,
                    className="fs-3 fw-semibold",
                    style={"color": tone or _INK["primary"]},
                ),
            ]
        ),
        class_name="border-0 shadow-sm h-100",
    )


def callout_card(callout: dict[str, Any]) -> dbc.Alert:
    """One callout, with its level spoken as well as coloured."""
    _colour, alert_colour, icon, spoken = LEVEL_STYLES.get(
        callout.get("level", "info"), LEVEL_STYLES["info"]
    )
    return dbc.Alert(
        [
            html.Div(
                [
                    html.I(className=f"bi {icon} me-2"),
                    html.Span(spoken, className="badge text-bg-light me-2"),
                    html.Strong(callout.get("title", "")),
                ],
                className="d-flex align-items-center mb-1",
            ),
            html.Div(callout.get("detail", ""), className="small"),
        ],
        color=alert_colour,
        class_name="mb-2",
    )


def graph(figure: Any, *, height: int = 420) -> dcc.Graph:
    """A chart with the interaction defaults every page should share."""
    return dcc.Graph(
        figure=figure,
        config={"displayModeBar": False, "responsive": True},
        style={"height": f"{height}px"},
    )


def money(amount: float) -> str:
    return f"${amount:,.2f}"


# Grid chrome, shared so the two tables look like one component. AG Grid
# rather than dash_table: Dash 4 deprecates DataTable and points here, and
# this project treats warnings as errors precisely so that kind of debt gets
# dealt with rather than suppressed.
GRID_CLASS = "ag-theme-alpine"
GRID_STYLE = {"height": "26rem", "width": "100%"}
GRID_DEFAULTS = {"sortable": True, "filter": True, "resizable": True, "flex": 1}


def money_column(name: str, field: str) -> dict[str, Any]:
    """A right-aligned currency column with tabular figures.

    Tabular rather than proportional: these are columns that must align
    vertically, which is the one case the figures rule carves out.
    """
    return {
        "headerName": name,
        "field": field,
        "type": "numericColumn",
        "valueFormatter": {"function": "d3.format('$,.2f')(params.value)"},
        "cellStyle": {"fontVariantNumeric": "tabular-nums"},
    }
