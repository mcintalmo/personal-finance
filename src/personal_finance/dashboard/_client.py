"""API client for the Dash app: every page reads `pf serve` over HTTP.

The Dash app never opens DuckDB. That is the same contract the Streamlit app
had, and it is what keeps the UI swappable — but the error handling is
deliberately different. Streamlit's client called ``st.stop()``, which works
because a Streamlit page is a script. A Dash callback is a function that has
to *return* something renderable, so failures are raised as :class:`ApiError`
and each page turns them into a visible alert.

Nothing is softened into "no data". A 500 that renders as an empty chart is
indistinguishable from a genuinely empty warehouse, which is the exact shape
of silent failure this project has been bitten by before. The one exception
is :func:`get_optional`, for a supplementary band bolted onto a page whose
main content should still render.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from personal_finance.config import get_settings


class ApiError(Exception):
    """A call to `pf serve` failed, carrying a message fit to show a user."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_missing_marts(self) -> bool:
        """503 is how the API says "this mart has not been built yet"."""
        return self.status_code == 503


def api_url() -> str:
    """Where `pf serve` is. Read per call so a test can repoint it."""
    return os.environ.get("PF_API_URL") or get_settings().serving.api_url


def _detail(response: httpx.Response) -> str:
    try:
        return response.json().get("detail", response.text)
    except ValueError:
        return response.text


def request(method: str, path: str, **kwargs: Any) -> Any:
    """Call the API, raising :class:`ApiError` with something worth reading."""
    url = api_url()
    try:
        response = httpx.request(method, f"{url}{path}", timeout=30.0, **kwargs)
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        # /callouts computes over the whole ledger on demand, so it is the
        # likeliest to blow the timeout. 30s rather than the Streamlit app's
        # 10s: a Dash callback shows a spinner meanwhile, so waiting is
        # cheaper here than failing.
        message = f"{path} timed out after 30s."
        raise ApiError(message) from exc
    except httpx.TransportError as exc:
        message = f"Can't reach the API at {url} — run `pf serve` in another terminal."
        raise ApiError(message) from exc
    except httpx.HTTPStatusError as exc:
        raise ApiError(
            f"{path}: {_detail(exc.response)}", status_code=exc.response.status_code
        ) from exc
    return response.json()


def get(path: str, **params: Any) -> Any:
    return request("GET", path, params=params)


def get_optional(path: str, **params: Any) -> Any | None:
    """Fetch a supplementary section, returning None only when its marts are missing.

    Used by the Overview page's callout band. A warehouse built before the
    callout marts existed should not take down the page a user actually came
    for — but only the 503 goes quiet. Every other failure still raises, so a
    500 cannot masquerade as "nothing to report".
    """
    try:
        return get(path, **params)
    except ApiError as exc:
        if exc.is_missing_marts:
            return None
        raise


def post(path: str, json: dict[str, Any]) -> Any:
    return request("POST", path, json=json)


def put(path: str, json: dict[str, Any]) -> Any:
    return request("PUT", path, json=json)
