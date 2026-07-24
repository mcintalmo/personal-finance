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
a decided merchant_name doesn't resurface as a candidate); only single-hop
merges are resolved, so a chain of merges (A into B, B into C) is not
followed to a single root.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from personal_finance.exceptions import NotFoundError, ValidationError
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

    Raises:
        ValidationError: `merchant_name` and `canonical_name` are the same.
        NotFoundError: either isn't a real `silver_transactions.merchant_name`.
    """
    if merchant_name == canonical_name:
        msg = f"Cannot merge a merchant into itself: {merchant_name!r}"
        raise ValidationError(msg)
    for name in (merchant_name, canonical_name):
        result = conn.execute(
            "SELECT count(*) FROM main_silver.silver_transactions WHERE merchant_name = $name",
            {"name": name},
        ).fetchone()
        if not result or not result[0]:
            msg = f"No such merchant: {name!r}"
            raise NotFoundError(msg)

    merge = MerchantMerge(
        merchant_name=merchant_name,
        canonical_name=canonical_name,
        similarity=similarity,
        status=status,
        note=note,
    )
    conn.execute(_INSERT_MERGE, merge.model_dump())
    return merge
