"""Sankey diagram of money flow: income -> account -> top-level category."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from personal_finance.webapp._client import get

st.set_page_config(page_title="Sankey — personal-finance", page_icon="\U0001f30a", layout="wide")
st.title("Money flow")

edges = pd.DataFrame(get("/sankey"))
if edges.empty:
    st.info("No flow yet — run `pf transform` after ingesting some data.")
    st.stop()

nodes = sorted(set(edges["source_node"]) | set(edges["target_node"]))
node_index = {name: i for i, name in enumerate(nodes)}

fig = go.Figure(
    go.Sankey(
        node={"label": nodes, "pad": 15, "thickness": 15},
        link={
            "source": edges["source_node"].map(node_index),
            "target": edges["target_node"].map(node_index),
            "value": edges["value"],
        },
    )
)
fig.update_layout(height=600)
st.plotly_chart(fig, width="stretch")
