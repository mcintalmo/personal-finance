"""Phase 6 overview dashboard: net flow, spend over time, top movers."""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from personal_finance.webapp._client import get

st.set_page_config(page_title="Overview — personal-finance", page_icon="\U0001f4b0", layout="wide")
st.title("Overview")

overview = get("/overview")

col1, col2, col3 = st.columns(3)
col1.metric("Total inflow", f"${overview['total_inflow']:,.2f}")
col2.metric("Total outflow", f"${overview['total_outflow']:,.2f}")
col3.metric("Net", f"${overview['net_amount']:,.2f}")

months = pd.DataFrame(overview["months"])
if not months.empty:
    months["month"] = pd.to_datetime(months["month"])
    st.subheader("Spend over time")
    flow = months.melt(
        id_vars="month",
        value_vars=["total_inflow", "total_outflow"],
        var_name="flow",
        value_name="amount",
    )
    fig = px.bar(flow, x="month", y="amount", color="flow", barmode="group")
    st.plotly_chart(fig, width="stretch")

st.subheader("Top movers")
top_n = st.slider("Show top N merchants", min_value=5, max_value=30, value=10)
merchants = pd.DataFrame(get("/merchants/top", limit=top_n))
if not merchants.empty:
    fig = px.bar(
        merchants.sort_values("total_outflow"),
        x="total_outflow",
        y="merchant_name",
        orientation="h",
        labels={"total_outflow": "Total outflow ($)", "merchant_name": "Merchant"},
    )
    st.plotly_chart(fig, width="stretch")
else:
    st.info("No merchant activity yet — run `pf transform` after ingesting some data.")
