"""Review-queue UI: approve/correct categorizations the automated cascade
declined to guess on (mirrors `pf review list` / `pf review label`)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from personal_finance.webapp._client import get, post

st.set_page_config(
    page_title="Review queue — personal-finance", page_icon="\U0001f9fe", layout="wide"
)
st.title("Review queue")

kind = st.radio("Kind", ["transaction", "split"], horizontal=True)
limit = st.slider("Queue size", min_value=5, max_value=50, value=20)

queue = get("/review/queue", kind=kind, limit=limit)
if not queue:
    st.success("Nothing waiting for review.")
    st.stop()

st.dataframe(pd.DataFrame(queue), width="stretch")

st.subheader("Label an item")
subject_id = st.selectbox("Item", [item["subject_id"] for item in queue])
category_path = st.text_input("Category path (e.g. essentials/groceries/apples)")
note = st.text_input("Note (optional)")

if st.button("Submit correction", disabled=not category_path):
    result = post(
        "/review/label",
        {
            "kind": kind,
            "subject_id": subject_id,
            "category_path": category_path,
            "note": note or None,
        },
    )
    st.success(f"Labeled {result['subject_id']} -> category {result['category_id']}")
    st.rerun()
