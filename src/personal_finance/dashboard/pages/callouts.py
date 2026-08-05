"""What changed: spending spikes, trends, and budgets at risk.

The rest of the dashboard shows the user their money; this page tells them
which part of it to look at. Everything comes from `GET /callouts`, which
computes the feed on demand — there is no callout table to rebuild.
"""

from __future__ import annotations

from typing import Any

import dash
from dash import Input, Output, State, callback, dcc, html

from personal_finance.dashboard._client import ApiError, get
from personal_finance.dashboard.components import (
    callout_card,
    empty_state,
    error_alert,
    page_header,
)

dash.register_page(__name__, path="/callouts", name="Callouts", icon="bi-bell", order=4)

KIND_LABELS = {
    "spike": "Spikes",
    "dip": "Dips",
    "trend": "Trends",
    "budget_risk": "Budget risk",
}


def layout(**_kwargs: Any) -> html.Div:
    return html.Div(
        [
            page_header("Callouts", "What moved, and what is about to."),
            # The feed is computed over the whole ledger on demand — it is the
            # endpoint the client raises its timeout to 30s for. Fetched once
            # into this Store and filtered from memory thereafter; re-fetching
            # per checkbox toggle made every chip click pay for a full
            # recompute, and paid for two just to load the page.
            dcc.Store(id="callouts-feed"),
            html.Div(id="callouts-filter"),
            html.Div(id="callouts-body"),
        ]
    )


@callback(
    Output("callouts-feed", "data"),
    Output("callouts-filter", "children"),
    Output("callouts-body", "children"),
    Input("callouts-body", "id"),
)
def render(_trigger: str) -> tuple[Any, Any, Any]:
    try:
        feed = get("/callouts")
    except ApiError as exc:
        return None, None, error_alert(exc)

    callouts = feed.get("callouts") or []
    notes = []
    if not feed.get("forecasts_available"):
        notes.append(
            empty_state(
                "No forecasts yet — run `pf forecast` for trend and budget-risk callouts. "
                "Spikes and dips don't need it and are shown below."
            )
        )

    if not callouts:
        # Scoped to what was actually checked: claiming "no trends" when no
        # forecast exists to derive one from is an all-clear nobody earned.
        notes.append(
            empty_state(
                "Nothing notable — no unusual months, trends, or budget overruns."
                if feed.get("forecasts_available")
                else "No unusual months found. Trends and budget risk were not checked."
            )
        )
        return feed, None, html.Div(notes)

    kinds = [kind for kind in KIND_LABELS if any(c["kind"] == kind for c in callouts)]
    controls = dcc.Checklist(
        id="callouts-kinds",
        options=[{"label": f" {KIND_LABELS[k]}", "value": k} for k in kinds],
        value=kinds,
        inline=True,
        className="mb-3",
        labelStyle={"marginRight": "1.5rem"},
    )
    return feed, controls, html.Div([*notes, html.Div(id="callouts-list")])


@callback(
    Output("callouts-list", "children"),
    Input("callouts-kinds", "value"),
    State("callouts-feed", "data"),
)
def filter_callouts(chosen: list[str] | None, feed: dict[str, Any] | None) -> Any:
    """Filters the already-fetched feed. No second request."""
    shown = [c for c in ((feed or {}).get("callouts") or []) if c["kind"] in (chosen or [])]
    if not shown:
        return html.Div("No callouts of the selected kinds.", className="small text-muted")
    return html.Div([callout_card(callout) for callout in shown])
