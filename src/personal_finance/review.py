"""Human review queue: the categorization cascade's ambiguous tail.

Stages 1-3 (rules, embedding similarity, local-LLM fallback) each decline to
guess when unsure rather than risk a wrong categorization — what's left after
all three is the genuinely ambiguous tail this module surfaces for a human to
resolve. A correction is stored as a :class:`~personal_finance.models.Label`
(``subject_kind=transaction``), which outranks every automated stage once
`pf transform` re-runs — see ``silver_transaction_categories_all``, where the
human stage is unioned first and every other stage excludes what it covers.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from personal_finance.exceptions import NotFoundError
from personal_finance.models import EntityKind, Label

if TYPE_CHECKING:
    from datetime import date
    from decimal import Decimal

    import duckdb


@dataclass
class ReviewItem:
    """One transaction the categorization cascade could not confidently place."""

    transaction_id: str
    posted_on: date
    amount: Decimal
    merchant_name: str | None
    description_raw: str
    source: str


def fetch_review_queue(conn: duckdb.DuckDBPyConnection, *, limit: int = 20) -> list[ReviewItem]:
    """Return up to `limit` transactions absent from every cascade stage, most recent first.

    Reads ``main_silver.silver_transactions`` / ``silver_transaction_categories_all``
    — `pf transform` must have run at least once.
    """
    rows = conn.execute(
        """
        SELECT transaction_id, posted_on, amount, merchant_name, description_raw, source
        FROM main_silver.silver_transactions
        WHERE transaction_id NOT IN (
            SELECT transaction_id FROM main_silver.silver_transaction_categories_all
        )
        ORDER BY posted_on DESC, transaction_id
        LIMIT $limit
        """,
        {"limit": limit},
    ).fetchall()
    return [ReviewItem(*row) for row in rows]


def record_label(
    conn: duckdb.DuckDBPyConnection,
    transaction_id: str,
    category_path: str,
    category_paths: dict[str, str],
    *,
    note: str | None = None,
) -> Label:
    """Store a human category correction for one transaction.

    ``category_paths`` is the ``{path: category_id}`` map from
    :func:`personal_finance.llm_categorize.fetch_category_paths` — passed in
    rather than refetched here so a caller recording many labels pays the
    recursive taxonomy query once.

    Raises:
        NotFoundError: `transaction_id` isn't a real silver transaction, or
            `category_path` isn't in the taxonomy.
    """
    if category_path not in category_paths:
        msg = f"Unknown category path {category_path!r}. Known paths: {sorted(category_paths)}"
        raise NotFoundError(msg)
    result = conn.execute(
        "SELECT count(*) FROM main_silver.silver_transactions WHERE transaction_id = $id",
        {"id": transaction_id},
    ).fetchone()
    if not result or not result[0]:
        msg = f"No such transaction: {transaction_id!r}"
        raise NotFoundError(msg)

    label = Label(
        subject_kind=EntityKind.TRANSACTION,
        subject_id=transaction_id,
        category_id=category_paths[category_path],
        note=note,
    )
    conn.execute(
        "INSERT INTO labels (id, created_at, subject_kind, subject_id, category_id, note) "
        "VALUES ($id, $created_at, $subject_kind, $subject_id, $category_id, $note)",
        label.model_dump(),
    )
    return label
