"""Local-LLM categorization fallback, cached in ``merchant_llm_categories``.

Stage 3 of the categorization cascade (Phase 4): merchants neither a rule
(stage 1) nor embedding similarity (stage 2, see :mod:`personal_finance.embed`)
could place get asked directly of a local Ollama chat model — "which of these
category paths best fits this merchant?" — with structured JSON output so the
response is a category path + a self-reported confidence, no free-text parsing
needed. Like stage 2, this embeds/asks once per distinct merchant and caches
the result, so re-running never re-asks Ollama about a merchant it already
classified.

Nothing here ever leaves the machine — the request goes to
``settings.ollama.base_url``, which defaults to Ollama's own local-only
address.
"""

import json
import logging
from typing import TYPE_CHECKING, Self
from uuid import NAMESPACE_URL, uuid5

import httpx

from personal_finance.exceptions import ExternalServiceError
from personal_finance.models import MerchantLlmCategory

if TYPE_CHECKING:
    from types import TracebackType

    import duckdb

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120.0  # local CPU/GPU chat inference can be slow

_RESPONSE_FORMAT = {
    "type": "object",
    "properties": {
        "category": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["category", "confidence"],
}


def _build_prompt(merchant_name: str, category_paths: list[str]) -> str:
    options = "\n".join(f"- {path}" for path in category_paths)
    return (
        "You are categorizing a bank transaction merchant into a personal "
        "finance taxonomy.\n\n"
        f"Merchant: {merchant_name}\n\n"
        "Choose exactly one category from this list (respond with the full "
        f"path, exactly as written):\n{options}\n\n"
        "Respond with a JSON object: `category` (one of the paths above, "
        "verbatim) and `confidence` (your confidence in that choice, 0.0-1.0)."
    )


class LlmCategorizeClient:
    """A thin, mockable wrapper around Ollama's ``/api/chat`` endpoint.

    Pass a pre-built ``client`` (e.g. one using ``httpx.MockTransport``) to
    test without a live Ollama server; otherwise a real ``httpx.Client``
    bound to ``base_url`` is constructed. Use as a context manager, or call
    :meth:`close` directly, to release the underlying connection.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        self._model = model
        self._client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def classify(self, merchant_name: str, category_paths: list[str]) -> tuple[str, float] | None:
        """Ask the model to pick a category path for one merchant.

        Returns ``None`` — rather than raising — when the model's response
        can't be trusted (malformed JSON, or a category outside the given
        list): that's the LLM stage declining to guess, same as stage 2
        leaving a candidate uncategorized below its confidence threshold, and
        it lets the caller keep processing the rest of the batch. Connection
        and HTTP-level failures still raise, since those signal Ollama itself
        is unreachable rather than one bad classification.

        Raises:
            ExternalServiceError: Ollama is unreachable or the model isn't
                pulled.
        """
        try:
            response = self._client.post(
                "/api/chat",
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "user", "content": _build_prompt(merchant_name, category_paths)}
                    ],
                    "format": _RESPONSE_FORMAT,
                    "stream": False,
                    "options": {"temperature": 0},
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _error_detail(exc.response)
            msg = f"Ollama chat request failed ({exc.response.status_code}): {detail}"
            raise ExternalServiceError(msg) from exc
        except httpx.HTTPError as exc:
            msg = (
                f"Could not reach Ollama at {self._client.base_url!r} — is it running? "
                f"(pull the model first: `ollama pull {self._model}`) ({exc})"
            )
            raise ExternalServiceError(msg) from exc

        content = response.json().get("message", {}).get("content")
        try:
            parsed = json.loads(content)
            category = parsed["category"]
            confidence = float(parsed["confidence"])
        except TypeError, KeyError, ValueError, json.JSONDecodeError:
            logger.warning("Ollama returned an unparseable classification for %r", merchant_name)
            return None
        if category not in category_paths:
            logger.warning(
                "Ollama chose category %r (not in the taxonomy) for %r", category, merchant_name
            )
            return None
        return category, max(0.0, min(1.0, confidence))


def _error_detail(response: httpx.Response) -> str:
    try:
        return response.json().get("error", response.text)
    except ValueError:
        return response.text


def merchant_llm_category_id(merchant_name: str, model: str) -> str:
    """Return the deterministic id for one (merchant_name, model) classification.

    Stable across runs (like :func:`personal_finance.embed.merchant_embedding_id`),
    so re-classifying the same merchant with the same model upserts in place
    rather than duplicating.
    """
    return uuid5(
        NAMESPACE_URL, f"personal-finance:merchant_llm_category:{model}:{merchant_name}"
    ).hex


_CATEGORY_PATHS_SQL = """
with recursive category_paths as (
    select id, name, name as path
    from categories
    where parent_id is null

    union all

    select child.id, child.name, parent.path || '/' || child.name as path
    from categories as child
    inner join category_paths as parent on child.parent_id = parent.id
)
select path, id from category_paths order by path
"""


def fetch_category_paths(conn: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """Return every taxonomy leaf/branch as {full slash-separated path: category_id}.

    Reads the application ``categories`` table directly (seeded by `pf
    init-db`) rather than the dbt gold mart, so this works before any dbt
    build has run.
    """
    return dict(conn.execute(_CATEGORY_PATHS_SQL).fetchall())


_UPSERT_LLM_CATEGORY = """
INSERT INTO merchant_llm_categories (id, created_at, merchant_name, model, category_id, confidence, note)
VALUES ($id, $created_at, $merchant_name, $model, $category_id, $confidence, $note)
ON CONFLICT (id) DO UPDATE SET category_id = excluded.category_id, confidence = excluded.confidence
"""


def compute_missing_llm_categories(
    conn: duckdb.DuckDBPyConnection,
    client: LlmCategorizeClient,
    model: str,
) -> int:
    """Classify every distinct merchant stages 1-2 missed and not yet cached for ``model``.

    Reads ``main_silver.silver_transactions`` / ``silver_transaction_categories``
    / ``silver_transaction_categories_embedding`` — the dbt build must have run
    at least once (``pf transform``) before this can see what's still
    uncategorized. A merchant the model can't confidently classify (see
    :meth:`LlmCategorizeClient.classify`) is left uncached, ready for a future
    human-review stage instead.

    Returns:
        How many merchants were newly cached (0 if all were already cached, or
        the model couldn't confidently classify any of the remainder).
    """
    category_paths = fetch_category_paths(conn)
    rows = conn.execute(
        """
        SELECT DISTINCT t.merchant_name
        FROM main_silver.silver_transactions AS t
        WHERE t.merchant_name IS NOT NULL
        AND t.transaction_id NOT IN (
            SELECT transaction_id FROM main_silver.silver_transaction_categories
        )
        AND t.transaction_id NOT IN (
            SELECT transaction_id FROM main_silver.silver_transaction_categories_embedding
        )
        AND t.merchant_name NOT IN (
            SELECT merchant_name FROM merchant_llm_categories WHERE model = $model
        )
        ORDER BY t.merchant_name
        """,
        {"model": model},
    ).fetchall()

    count = 0
    for (name,) in rows:
        result = client.classify(name, list(category_paths))
        if result is None:
            continue
        category_path, confidence = result
        llm_category = MerchantLlmCategory(
            id=merchant_llm_category_id(name, model),
            merchant_name=name,
            model=model,
            category_id=category_paths[category_path],
            confidence=confidence,
        )
        conn.execute(_UPSERT_LLM_CATEGORY, llm_category.model_dump())
        count += 1
    return count
