"""Budget vs. actual: define buckets in budgets.yaml (see the Config page),
this page just visualizes the resulting gold_budget_actuals time series."""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from personal_finance.webapp._client import get

st.set_page_config(page_title="Budgets — personal-finance", page_icon="\U0001f4ca", layout="wide")
st.title("Budget vs. actual")

actuals = pd.DataFrame(get("/budgets"))
if actuals.empty:
    st.info(
        "No budgets yet — add buckets to config/budgets.yaml (see the Config page), "
        "then `pf init-db` and `pf transform`."
    )
    st.stop()

actuals["period_start"] = pd.to_datetime(actuals["period_start"])

for name, group in actuals.groupby("name"):
    st.subheader(name)
    latest = group.sort_values("period_start").iloc[-1]
    over_budget = latest["actual_outflow"] > latest["budgeted_amount"]
    col1, col2, col3 = st.columns(3)
    col1.metric(f"Budget ({latest['period']})", f"${latest['budgeted_amount']:,.2f}")
    col2.metric(
        "Actual (latest period)",
        f"${latest['actual_outflow']:,.2f}",
        delta=f"${latest['variance']:,.2f}",
        delta_color="inverse",
    )
    col3.metric("Status", "Over budget" if over_budget else "On track")

    fig = px.bar(
        group, x="period_start", y="actual_outflow", labels={"actual_outflow": "Actual ($)"}
    )
    fig.add_hline(y=latest["budgeted_amount"], line_dash="dash", annotation_text="Budgeted amount")
    st.plotly_chart(fig, width="stretch")
