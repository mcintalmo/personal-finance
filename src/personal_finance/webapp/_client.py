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


def _request(method: str, path: str, optional: bool = False, **kwargs: Any) -> Any:
    """Call the API. ``optional`` softens a *missing-mart* 503 into None.

    Nothing else is softened. A 500, a 422 or a serialization failure is a
    real defect, and returning None for it would render identically to "there
    was nothing to show" — the exact shape of silent failure this project has
    already been bitten by once.
    """
    try:
        response = httpx.request(method, f"{API_URL}{path}", timeout=10.0, **kwargs)
        response.raise_for_status()
    except httpx.TimeoutException:
        # /callouts computes over the whole ledger on demand, so it is the
        # likeliest endpoint to blow the timeout. Letting httpx raise here
        # would kill the page with a traceback — strictly worse than the
        # st.stop() this function exists to avoid.
        st.error(f"{path} timed out after 10s.")
        if optional:
            return None
        st.stop()
    except httpx.TransportError:
        st.error(f"Can't reach the API at {API_URL} — run `pf serve` in another terminal.")
        st.stop()
    except httpx.HTTPStatusError as exc:
        if optional and exc.response.status_code == 503:
            return None  # marts not built; the caller renders its own explanation
        st.error(f"{path}: {_error_detail(exc.response)}")
        if optional:
            return None
        st.stop()
    return response.json()


def get(path: str, **params: Any) -> Any:
    return _request("GET", path, params=params)


def get_optional(path: str, **params: Any) -> Any | None:
    """Fetch a supplementary section, returning None instead of stopping the page.

    `get` calls `st.stop()` on an error, which is right when the endpoint *is*
    the page. It is wrong for a secondary band bolted onto another page: a
    warehouse built before that endpoint's marts existed would take the whole
    page down over a section the user didn't come for.

    Only that case (503) goes quiet. Every other error is still shown — it just
    doesn't halt the page — because a 500 rendering as an empty section is
    indistinguishable from good news. A lost connection still stops outright:
    nothing else on the page will render either.
    """
    return _request("GET", path, optional=True, params=params)


def post(path: str, json: dict[str, Any]) -> Any:
    return _request("POST", path, json=json)


def put(path: str, json: dict[str, Any]) -> Any:
    return _request("PUT", path, json=json)
