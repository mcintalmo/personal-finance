"""Config editing: edit the six user-editable YAML files in place. A write
re-validates the WHOLE configuration (cross-file referential integrity, not
just this file's own schema) before touching disk — see
personal_finance.user_config.write_config_file. Changes only take effect
after re-running `pf init-db` / `pf transform`."""

from __future__ import annotations

import streamlit as st

from personal_finance.webapp._client import get, put

st.set_page_config(page_title="Config — personal-finance", page_icon="⚙️", layout="wide")
st.title("Config editor")

names = get("/config")
name = st.selectbox("File", names)

config_file = get(f"/config/{name}")
content = st.text_area("YAML", value=config_file["content"], height=500)

if st.button("Save"):
    put(f"/config/{name}", {"name": name, "content": content})
    st.success(f"Saved {name}.yaml. Run `pf init-db`/`pf transform` to apply it.")
    st.rerun()
