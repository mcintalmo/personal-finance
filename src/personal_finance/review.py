"""Human review queue: the categorization cascade's ambiguous tail.

Stages 1-3 (rules, embedding similarity, local-LLM fallback) each decline to
guess when unsure rather than risk a wrong categorization — what's left after
all three is the genuinely ambiguous tail this module surfaces for a human to
resolve, for both transactions and (Amazon) line-item splits. A correction is
stored as a :class:`~personal_finance.models.Label`, which outranks every
automated stage once `pf transform` re-runs — see
``silver_transaction_categories_all``/``silver_split_categories_all``, where
the human stage is unioned first and every other stage excludes what it covers.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from personal_finance.exceptions import NotFoundError
from personal_finance.models import EntityKind, Label

if TYPE_CHECKING:
    from datetime import date
    from decimal import Decimal

    import duckdb

# Which silver table/id-column a subject_kind's rows live in — used by
# record_label to validate a subject_id exists before storing a correction
# for it. Only kinds a human can actually label belong here (not DOCUMENT).
_SUBJECT_TABLES: dict[EntityKind, str] = {
    EntityKind.TRANSACTION: "main_silver.silver_transactions",
    EntityKind.SPLIT: "main_silver.silver_amazon_splits",
}
_SUBJECT_ID_COLUMNS: dict[EntityKind, str] = {
    EntityKind.TRANSACTION: "transaction_id",
    EntityKind.SPLIT: "split_id",
}


@dataclass
class ReviewItem:
    """One transaction the categorization cascade could not confidently place."""

    transaction_id: str
    posted_on: date
    amount: Decimal
    merchant_name: str | None
    description_raw: str
    source: str


@dataclass
class SplitReviewItem:
    """One line-item split the split-categorization cascade could not confidently place."""

    split_id: str
    transaction_id: str
    asin: str
    product_name: str
    amount: Decimal
    quantity: int


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


def fetch_split_review_queue(
    conn: duckdb.DuckDBPyConnection, *, limit: int = 20
) -> list[SplitReviewItem]:
    """Return up to `limit` splits absent from every split-cascade stage.

    Reads ``main_silver.silver_amazon_splits`` / ``silver_split_categories_all``
    — `pf transform` must have run at least once. Ordered by split_id (a
    split has no posted_on of its own the way a transaction does).
    """
    rows = conn.execute(
        """
        SELECT split_id, transaction_id, asin, product_name, amount, quantity
        FROM main_silver.silver_amazon_splits
        WHERE split_id NOT IN (
            SELECT split_id FROM main_silver.silver_split_categories_all
        )
        ORDER BY split_id
        LIMIT $limit
        """,
        {"limit": limit},
    ).fetchall()
    return [SplitReviewItem(*row) for row in rows]


def record_label(
    conn: duckdb.DuckDBPyConnection,
    subject_id: str,
    category_path: str,
    category_paths: dict[str, str],
    *,
    subject_kind: EntityKind = EntityKind.TRANSACTION,
    note: str | None = None,
) -> Label:
    """Store a human category correction for one transaction or split.

    ``category_paths`` is the ``{path: category_id}`` map from
    :func:`personal_finance.llm_categorize.fetch_category_paths` — passed in
    rather than refetched here so a caller recording many labels pays the
    recursive taxonomy query once.

    Raises:
        NotFoundError: `subject_id` isn't a real silver transaction/split for
            `subject_kind`, or `category_path` isn't in the taxonomy.
        ValueError: `subject_kind` isn't one a human can label (e.g. DOCUMENT).
    """
    if subject_kind not in _SUBJECT_TABLES:
        msg = f"Cannot label a {subject_kind.value!r} subject; expected one of {sorted(k.value for k in _SUBJECT_TABLES)}"
        raise ValueError(msg)
    if category_path not in category_paths:
        msg = f"Unknown category path {category_path!r}. Known paths: {sorted(category_paths)}"
        raise NotFoundError(msg)
    # table/id_column come from the fixed internal dicts above, keyed by the
    # EntityKind enum — never user input, so splicing them into SQL is safe.
    table = _SUBJECT_TABLES[subject_kind]
    id_column = _SUBJECT_ID_COLUMNS[subject_kind]
    result = conn.execute(
        f"SELECT count(*) FROM {table} WHERE {id_column} = $id",
        {"id": subject_id},
    ).fetchone()
    if not result or not result[0]:
        msg = f"No such {subject_kind.value}: {subject_id!r}"
        raise NotFoundError(msg)

    label = Label(
        subject_kind=subject_kind,
        subject_id=subject_id,
        category_id=category_paths[category_path],
        note=note,
    )
    conn.execute(
        "INSERT INTO labels (id, created_at, subject_kind, subject_id, category_id, note) "
        "VALUES ($id, $created_at, $subject_kind, $subject_id, $category_id, $note)",
        label.model_dump(),
    )
    return label
