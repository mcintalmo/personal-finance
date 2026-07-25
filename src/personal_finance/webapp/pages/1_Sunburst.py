"""Sunburst drill-down of the category hierarchy, by outflow."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from personal_finance.webapp._client import get

st.set_page_config(page_title="Sunburst — personal-finance", page_icon="\U0001f31e", layout="wide")
st.title("Category drill-down")

rollups = pd.DataFrame(get("/categories/sunburst"))
if rollups.empty:
    st.info("No categories yet — run `pf init-db` and `pf transform` first.")
    st.stop()

metric = st.radio("Value", ["total_outflow", "total_inflow"], horizontal=True)

fig = go.Figure(
    go.Sunburst(
        ids=rollups["category_id"],
        labels=rollups["name"],
        parents=rollups["parent_id"].fillna(""),
        values=rollups[metric],
        branchvalues="total",
        hovertemplate="<b>%{label}</b><br>$%{value:,.2f}<extra></extra>",
    )
)
fig.update_layout(margin={"t": 10, "l": 10, "r": 10, "b": 10}, height=700)
st.plotly_chart(fig, width="stretch")

st.dataframe(
    rollups[
        ["path", "depth", "transaction_count", "total_outflow", "total_inflow", "net_amount"]
    ].sort_values("total_outflow", ascending=False),
    width="stretch",
)
