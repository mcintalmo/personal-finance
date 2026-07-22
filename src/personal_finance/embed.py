"""Text embeddings via a local Ollama server, cached in ``merchant_embeddings``.

Used by the embedding-similarity categorization stage (Phase 4): every
distinct merchant — both those a rule already categorized (the "reference"/
labeled set) and those it missed (the "candidate" set) — gets embedded once
and cached, so re-running the stage doesn't re-call Ollama for merchants seen
before. The actual nearest-neighbor matching happens in SQL, not here — see
``transform/models/silver/silver_transaction_categories_embedding.sql``.

Nothing here ever leaves the machine — the request goes to
``settings.ollama.base_url``, which defaults to Ollama's own local-only
address.
"""

import logging
from typing import TYPE_CHECKING, Self
from uuid import NAMESPACE_URL, uuid5

import httpx

from personal_finance.exceptions import ExternalServiceError
from personal_finance.models import MerchantEmbedding

if TYPE_CHECKING:
    from types import TracebackType

    import duckdb

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60.0  # local CPU/GPU inference on a batch can be slow


class EmbeddingClient:
    """A thin, mockable wrapper around Ollama's ``/api/embed`` endpoint.

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

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text, in the same order.

        A single request handles the whole batch — Ollama's ``/api/embed``
        natively accepts a list of inputs. Callers with very large batches
        should chunk before calling.

        Raises:
            ExternalServiceError: Ollama is unreachable, the model isn't
                pulled, or the response doesn't have the expected shape.
        """
        if not texts:
            return []
        try:
            response = self._client.post("/api/embed", json={"model": self._model, "input": texts})
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _error_detail(exc.response)
            msg = f"Ollama embedding request failed ({exc.response.status_code}): {detail}"
            raise ExternalServiceError(msg) from exc
        except httpx.HTTPError as exc:
            msg = (
                f"Could not reach Ollama at {self._client.base_url!r} — is it running? "
                f"(pull the model first: `ollama pull {self._model}`) ({exc})"
            )
            raise ExternalServiceError(msg) from exc

        payload = response.json()
        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            msg = f"Unexpected response shape from Ollama /api/embed: {payload!r}"
            raise ExternalServiceError(msg)
        return embeddings


def _error_detail(response: httpx.Response) -> str:
    try:
        return response.json().get("error", response.text)
    except ValueError:
        return response.text


def merchant_embedding_id(merchant_name: str, model: str) -> str:
    """Return the deterministic id for one (merchant_name, model) embedding.

    Stable across runs (like :func:`personal_finance.user_config.
    category_id_for_path`), so re-embedding the same merchant with the same
    model upserts in place rather than duplicating.
    """
    return uuid5(NAMESPACE_URL, f"personal-finance:merchant_embedding:{model}:{merchant_name}").hex


_UPSERT_EMBEDDING = """
INSERT INTO merchant_embeddings (id, created_at, merchant_name, model, embedding, note)
VALUES ($id, $created_at, $merchant_name, $model, $embedding, $note)
ON CONFLICT (id) DO UPDATE SET embedding = excluded.embedding
"""


def compute_missing_embeddings(
    conn: duckdb.DuckDBPyConnection,
    client: EmbeddingClient,
    model: str,
    *,
    chunk_size: int = 128,
) -> int:
    """Embed every distinct merchant not yet cached for ``model``, and cache it.

    Reads ``main_silver.silver_transactions.merchant_name`` — the dbt build
    must have run at least once (``pf transform``) before this can see any
    merchants. Embeds every distinct merchant (not just uncategorized ones):
    the embedding-similarity model needs vectors for already-categorized
    merchants too, to compare uncategorized ones against.

    Returns:
        How many merchants were newly embedded (0 if all were already cached).
    """
    rows = conn.execute(
        """
        SELECT DISTINCT merchant_name
        FROM main_silver.silver_transactions
        WHERE merchant_name IS NOT NULL
        AND merchant_name NOT IN (
            SELECT merchant_name FROM merchant_embeddings WHERE model = $model
        )
        ORDER BY merchant_name
        """,
        {"model": model},
    ).fetchall()
    names = [row[0] for row in rows]
    for start in range(0, len(names), chunk_size):
        batch = names[start : start + chunk_size]
        vectors = client.embed(batch)
        for name, vector in zip(batch, vectors, strict=True):
            embedding = MerchantEmbedding(
                id=merchant_embedding_id(name, model),
                merchant_name=name,
                model=model,
                embedding=vector,
            )
            conn.execute(_UPSERT_EMBEDDING, embedding.model_dump())
    return len(names)
