"""What changed: spending spikes, trends, and budgets at risk.

The rest of the dashboard shows the user their money; this page tells them
which part of it to look at. Everything on it comes from `GET /callouts`,
which computes the feed on demand — there is no callout table to rebuild.
"""

from __future__ import annotations

import streamlit as st

from personal_finance.webapp._client import get

st.set_page_config(page_title="Callouts — personal-finance", page_icon="\U0001f514", layout="wide")
st.title("Callouts")

feed = get("/callouts")
callouts = feed["callouts"]

if not feed["forecasts_available"]:
    st.info(
        "No forecasts yet — run `pf forecast` to get trend and budget-risk callouts. "
        "Spike and dip callouts don't need it and are shown below."
    )

if not callouts:
    # Scoped to what was actually checked: claiming "no trends" when no
    # forecast exists to derive a trend from is an all-clear nobody earned.
    st.success(
        "Nothing notable to report — no unusual months, trends, or budget overruns."
        if feed["forecasts_available"]
        else "No unusual months found. Trends and budget risk were not checked."
    )
    st.stop()

KIND_LABELS = {
    "spike": "Spikes",
    "dip": "Dips",
    "trend": "Trends",
    "budget_risk": "Budget risk",
}
# Matches CalloutLevel: the level already encodes whether a change is good or
# bad news for the direction of money involved, so the widget just follows it.
LEVEL_WIDGETS = {"critical": st.error, "warning": st.warning, "info": st.info}

kinds = [kind for kind in KIND_LABELS if any(c["kind"] == kind for c in callouts)]
chosen = st.multiselect(
    "Show",
    options=kinds,
    default=kinds,
    format_func=lambda kind: KIND_LABELS[kind],
)

shown = [c for c in callouts if c["kind"] in chosen]
if not shown:
    st.caption("No callouts of the selected kinds.")

for callout in shown:
    widget = LEVEL_WIDGETS.get(callout["level"], st.info)
    widget(f"**{callout['title']}**\n\n{callout['detail']}")
