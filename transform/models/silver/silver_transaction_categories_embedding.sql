-- Stage 2 of the categorization cascade: embedding-similarity vs. labeled
-- history. Picks up transactions stage 1 (rules) didn't match, embeds their
-- merchants (cached by `pf enrich` — see personal_finance.embed), and assigns
-- each the category of its nearest already-categorized merchant by cosine
-- similarity, when that similarity clears `embedding_confidence_threshold`.
--
-- Candidacy is transaction-level, not merchant-level: a rule can target
-- account_name/source/description_raw rather than merchant_name, so a
-- merchant can have *some* rule-matched transactions and others left
-- uncategorized by stage 1. Those leftovers still get a chance here (often a
-- trivial, high-confidence self-match against the same merchant's own
-- rule-assigned category, since a categorized merchant can also appear in
-- its own reference set) — excluding the whole merchant, as an earlier
-- version of this model did, silently stranded them for stages 3/4 instead.
--
-- Grain: at most one row per transaction_id, same contract as stage 1 — a
-- transaction absent here (and from stage 1) is still uncategorized, ready
-- for the LLM-fallback stage (see TODO.md).
--
-- Requires `pf enrich` to have run (merchant_embeddings populated) before this
-- model has anything to match; with no embeddings yet it safely resolves to
-- zero rows, same as stage 1 resolves to zero rows if no rule ever matches.

with tx as (
    select transaction_id, merchant_name
    from {{ ref('silver_transactions') }}
    where merchant_name is not null
),

stage1 as (
    select transaction_id, category_id
    from {{ ref('silver_transaction_categories') }}
),

embeddings as (
    select merchant_name, embedding
    from {{ source('app', 'merchant_embeddings') }}
    where model = '{{ var("embedding_model", "nomic-embed-text") }}'
),

-- Reference set: merchants stage 1 already categorized. A merchant's own
-- transactions could in principle match different rules (applies_to can
-- differ per rule), so take the majority category per merchant, ties broken
-- deterministically by category_id.
merchant_votes as (
    select t.merchant_name, s1.category_id, count(*) as votes
    from tx as t
    inner join stage1 as s1 using (transaction_id)
    group by t.merchant_name, s1.category_id
),

reference as (
    select mv.merchant_name, mv.category_id, e.embedding
    from merchant_votes as mv
    inner join embeddings as e using (merchant_name)
    qualify row_number() over (
        partition by mv.merchant_name order by mv.votes desc, mv.category_id
    ) = 1
),

-- Candidates: merchants used by at least one transaction stage 1 didn't
-- cover, with an embedding available to compare. Deliberately not "merchants
-- with zero categorized transactions" — a merchant can be partly covered
-- (see header) and its remaining transactions still need a chance here.
uncategorized_merchants as (
    select distinct t.merchant_name
    from tx as t
    where t.transaction_id not in (select transaction_id from stage1)
),

candidates as (
    select e.merchant_name, e.embedding
    from embeddings as e
    where e.merchant_name in (select merchant_name from uncategorized_merchants)
),

-- Nearest reference merchant per candidate, by cosine similarity.
matches as (
    select
        c.merchant_name,
        r.merchant_name as matched_merchant,
        r.category_id,
        list_cosine_similarity(c.embedding, r.embedding) as similarity
    from candidates as c
    cross join reference as r
),

best as (
    select *
    from (
        select *, row_number() over (partition by merchant_name order by similarity desc) as rnk
        from matches
    )
    where rnk = 1
    and similarity >= {{ var('embedding_confidence_threshold', 0.80) }}
)

select
    t.transaction_id,
    b.category_id,
    b.matched_merchant,
    'embedding' as categorization_source,
    b.similarity as categorization_confidence
from tx as t
inner join best as b using (merchant_name)
where t.transaction_id not in (select transaction_id from stage1)
