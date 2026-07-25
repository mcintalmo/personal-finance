"""Shared API client for the Streamlit app: every page calls `pf serve` over
HTTP rather than touching DuckDB directly, so the UI stays swappable (a
future React frontend hits the same FastAPI contract, per docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import streamlit as st

from personal_finance.config import get_settings

API_URL = os.environ.get("PF_API_URL") or get_settings().serving.api_url


def _error_detail(response: httpx.Response) -> str:
    try:
        return response.json().get("detail", response.text)
    except ValueError:
        return response.text


def _request(method: str, path: str, **kwargs: Any) -> Any:
    try:
        response = httpx.request(method, f"{API_URL}{path}", timeout=10.0, **kwargs)
        response.raise_for_status()
    except httpx.ConnectError:
        st.error(f"Can't reach the API at {API_URL} — run `pf serve` in another terminal.")
        st.stop()
    except httpx.HTTPStatusError as exc:
        st.error(f"{path}: {_error_detail(exc.response)}")
        st.stop()
    return response.json()


def get(path: str, **params: Any) -> Any:
    return _request("GET", path, params=params)


def post(path: str, json: dict[str, Any]) -> Any:
    return _request("POST", path, json=json)


def put(path: str, json: dict[str, Any]) -> Any:
    return _request("PUT", path, json=json)
