"""Merchant-identity merge review queue (Phase 3 follow-up: the outlier tail
config-driven aliases can't resolve).

``merchants.yaml`` (see :mod:`personal_finance.user_config`) handles brand
variants a human already knows about. What's left is the descriptor tail no
regex was written for — a typo, an unfamiliar chain, a location suffix the
generic macro doesn't recognize — where two ``merchant_name`` values are
probably the same real merchant but nothing said so. This module surfaces
candidates for those by embedding-similarity over ``merchant_embeddings``
(cached by ``pf enrich`` — see :mod:`personal_finance.embed`), and records a
human's accept/reject decision.

Deliberately human-reviewed, not auto-applied: mis-merging two distinct real
merchants silently corrupts spend history in a way a wrong category (which a
better rule or another look can still catch) doesn't. Accepted merges are
stored in ``merchant_merges``, read by ``silver_transactions.sql`` after
``merchant_aliases`` resolution — same "human decision in its own table,
outranking nothing but filling gaps" shape as :class:`~personal_finance.models.
Label` for categorization corrections, not written back into any YAML config.

A decision is per ``merchant_name`` (both accept and reject are terminal —
a decided merchant_name doesn't resurface as a candidate, and the *latest*
decision for a given merchant_name wins if it's ever reviewed more than
once — see the ``merges`` CTE in ``silver_transactions.sql``); only
single-hop merges are resolved, so a chain of merges (A into B, B into C)
is not followed to a single root. Accepting a merge whose canonical_name
is itself already merged into merchant_name is rejected outright, since
that would form a 2-cycle the single-hop resolution can't distinguish from
a genuine merge — it would silently swap the two identities instead.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from personal_finance.embed import merchant_embedding_id
from personal_finance.exceptions import NotFoundError, ValidationError
from personal_finance.llm_categorize import merchant_llm_category_id
from personal_finance.models import MerchantMerge, MergeStatus

if TYPE_CHECKING:
    import duckdb


@dataclass
class MergeCandidate:
    """One suggested merchant-identity merge, awaiting a human decision."""

    merchant_name: str
    canonical_name: str
    similarity: float


def fetch_merge_candidates(
    conn: duckdb.DuckDBPyConnection,
    *,
    model: str,
    threshold: float = 0.90,
    limit: int = 20,
) -> list[MergeCandidate]:
    """Return up to `limit` candidate merges, highest similarity first.

    Compares every distinct merchant_name's cached embedding (`pf enrich`)
    against every other's by cosine similarity, keeping pairs at or above
    `threshold`. Each merchant appears in at most one candidate (its single
    best match); direction is picked so `canonical_name` is the merchant
    with more transaction history — the more likely "real" spelling.

    A merchant_name that already has a prior decision recorded *as the
    variant being merged* (`merchant_merges.merchant_name`, accepted or
    rejected) is excluded, so it never resurfaces once reviewed. A merchant
    that has only ever appeared as a decision's `canonical_name` (an
    absorption target) stays eligible — it can go on to absorb further,
    distinct variants later.

    Requires `pf transform` and `pf enrich` to have run — resolves to an
    empty list if either hasn't (no silver_transactions / no embeddings for
    `model`), same as every other cascade stage.
    """
    rows = conn.execute(
        """
        with embeddings as (
            select merchant_name, embedding
            from merchant_embeddings
            where model = $model
        ),

        counts as (
            select merchant_name, count(*) as transaction_count
            from main_silver.silver_transactions
            where merchant_name is not null
            group by merchant_name
        ),

        decided as (
            select distinct merchant_name from merchant_merges
        ),

        -- One row per unordered pair (a.merchant_name < b.merchant_name avoids
        -- both self-pairs and each pair's mirror image).
        pairs as (
            select
                a.merchant_name as a_name,
                b.merchant_name as b_name,
                list_cosine_similarity(a.embedding, b.embedding) as similarity
            from embeddings as a
            inner join embeddings as b on a.merchant_name < b.merchant_name
        ),

        scored as (
            select
                p.a_name,
                p.b_name,
                p.similarity,
                ca.transaction_count as a_count,
                cb.transaction_count as b_count
            from pairs as p
            inner join counts as ca on ca.merchant_name = p.a_name
            inner join counts as cb on cb.merchant_name = p.b_name
            where p.similarity >= $threshold
            and p.a_name not in (select merchant_name from decided)
            and p.b_name not in (select merchant_name from decided)
        ),

        -- More transaction history = the more likely "real" spelling.
        directed as (
            select
                case when a_count >= b_count then b_name else a_name end as merchant_name,
                case when a_count >= b_count then a_name else b_name end as canonical_name,
                similarity
            from scored
        )

        select merchant_name, canonical_name, similarity
        from (
            select
                *,
                row_number() over (partition by merchant_name order by similarity desc) as rnk
            from directed
        )
        where rnk = 1
        order by similarity desc, merchant_name
        limit $limit
        """,
        {"model": model, "threshold": threshold, "limit": limit},
    ).fetchall()
    return [MergeCandidate(*row) for row in rows]


def fetch_similarity(
    conn: duckdb.DuckDBPyConnection, merchant_name: str, canonical_name: str, *, model: str
) -> float | None:
    """Return the cached cosine similarity between two merchants' `model`
    embeddings, or None if either lacks one for `model`.

    Used to record the score that justified a merge decision without
    requiring a reviewer to retype the number `pf review merge-candidates`
    already showed them.
    """
    row = conn.execute(
        """
        select list_cosine_similarity(a.embedding, b.embedding)
        from merchant_embeddings as a, merchant_embeddings as b
        where a.merchant_name = $merchant_name and a.model = $model
        and b.merchant_name = $canonical_name and b.model = $model
        """,
        {"merchant_name": merchant_name, "canonical_name": canonical_name, "model": model},
    ).fetchone()
    return row[0] if row else None


def _latest_decision(
    conn: duckdb.DuckDBPyConnection, merchant_name: str
) -> tuple[MergeStatus, str] | None:
    # partition by merchant_name (redundant given the WHERE, since every row
    # here already has the same merchant_name) so an empty result stays
    # empty — DuckDB's qualify with an unpartitioned window function
    # produces one phantom all-NULL row over zero input rows instead.
    row = conn.execute(
        """
        select status, canonical_name
        from merchant_merges
        where merchant_name = $name
        qualify row_number() over (partition by merchant_name order by created_at desc) = 1
        """,
        {"name": merchant_name},
    ).fetchone()
    if row is None:
        return None
    return MergeStatus(row[0]), row[1]


def _carry_forward_cached_classifications(
    conn: duckdb.DuckDBPyConnection, merchant_name: str, canonical_name: str
) -> None:
    """Copy any cached embedding/LLM-category rows from the merged-away name
    to its canonical target (skipped where the target already has its own
    row for that model — its own history takes precedence).

    Without this, a transaction already classified by stage 2/3 under the
    old name silently drops out of both stages after the merge — the
    embedding/LLM cascade models join on the *current* merchant_name, and
    the cache is still keyed to the name that no longer appears anywhere in
    `silver_transactions` — sending it back to human review for no reason
    the human review queue was supposed to protect against.
    """
    for model, embedding in conn.execute(
        "SELECT model, embedding FROM merchant_embeddings WHERE merchant_name = $name",
        {"name": merchant_name},
    ).fetchall():
        conn.execute(
            "INSERT INTO merchant_embeddings (id, created_at, merchant_name, model, embedding) "
            "VALUES ($id, now(), $name, $model, $embedding) "
            "ON CONFLICT (merchant_name, model) DO NOTHING",
            {
                "id": merchant_embedding_id(canonical_name, model),
                "name": canonical_name,
                "model": model,
                "embedding": embedding,
            },
        )
    for model, category_id, confidence in conn.execute(
        "SELECT model, category_id, confidence FROM merchant_llm_categories WHERE merchant_name = $name",
        {"name": merchant_name},
    ).fetchall():
        conn.execute(
            "INSERT INTO merchant_llm_categories "
            "(id, created_at, merchant_name, model, category_id, confidence) "
            "VALUES ($id, now(), $name, $model, $category_id, $confidence) "
            "ON CONFLICT (merchant_name, model) DO NOTHING",
            {
                "id": merchant_llm_category_id(canonical_name, model),
                "name": canonical_name,
                "model": model,
                "category_id": category_id,
                "confidence": confidence,
            },
        )


def _merchant_name_exists(conn: duckdb.DuckDBPyConnection, merchant_name: str) -> bool:
    """A name is a real merchant if it's currently a `silver_transactions.merchant_name` —
    OR it was one when an earlier decision was recorded about it.

    silver_transactions is a dbt view: once a merge is accepted, the merged-away
    name stops appearing there immediately (it now resolves to its canonical
    name) — well before anyone runs `pf transform` again. Without the second
    check, `pf review reject-merge` could never undo an already-accepted
    decision, since the very name it needs to validate no longer "exists".
    """
    result = conn.execute(
        "SELECT count(*) FROM main_silver.silver_transactions WHERE merchant_name = $name",
        {"name": merchant_name},
    ).fetchone()
    if result and result[0]:
        return True
    result = conn.execute(
        "SELECT count(*) FROM merchant_merges WHERE merchant_name = $name",
        {"name": merchant_name},
    ).fetchone()
    return bool(result and result[0])


_INSERT_MERGE = """
INSERT INTO merchant_merges (id, created_at, merchant_name, canonical_name, similarity, status, note)
VALUES ($id, $created_at, $merchant_name, $canonical_name, $similarity, $status, $note)
"""


def record_merge_decision(
    conn: duckdb.DuckDBPyConnection,
    merchant_name: str,
    canonical_name: str,
    status: MergeStatus,
    *,
    similarity: float | None = None,
    note: str | None = None,
) -> MerchantMerge:
    """Record a human decision on a candidate merchant-identity merge.

    An accepted decision also copies any cached embedding/LLM-category rows
    from `merchant_name` to `canonical_name` (see
    :func:`_carry_forward_cached_classifications`), so transactions already
    classified under the old name don't silently fall back to human review.

    Raises:
        ValidationError: `merchant_name` and `canonical_name` are the same,
            or accepting would form a 2-cycle (canonical_name is already
            merged into merchant_name).
        NotFoundError: either isn't a real `silver_transactions.merchant_name`.
    """
    if merchant_name == canonical_name:
        msg = f"Cannot merge a merchant into itself: {merchant_name!r}"
        raise ValidationError(msg)
    for name in (merchant_name, canonical_name):
        if not _merchant_name_exists(conn, name):
            msg = f"No such merchant: {name!r}"
            raise NotFoundError(msg)

    if status is MergeStatus.ACCEPTED:
        existing = _latest_decision(conn, canonical_name)
        if (
            existing is not None
            and existing[0] is MergeStatus.ACCEPTED
            and existing[1] == merchant_name
        ):
            msg = (
                f"{canonical_name!r} is already merged into {merchant_name!r} — accepting this "
                f"would form a two-way cycle. Reject that decision first "
                f"(`pf review reject-merge {canonical_name} {merchant_name}`) to reverse it."
            )
            raise ValidationError(msg)

    merge = MerchantMerge(
        merchant_name=merchant_name,
        canonical_name=canonical_name,
        similarity=similarity,
        status=status,
        note=note,
    )
    conn.execute(_INSERT_MERGE, merge.model_dump())
    if status is MergeStatus.ACCEPTED:
        _carry_forward_cached_classifications(conn, merchant_name, canonical_name)
    return merge
