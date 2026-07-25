-- Stage 2 of the split-categorization cascade: embedding-similarity vs.
-- labeled history — the same approach as silver_transaction_categories_embedding,
-- applied to splits/product_name instead of transactions/merchant_name.
--
-- Candidacy is at the product_name level here (unlike the transaction
-- version): a rule's pattern is matched purely against product_name with no
-- other applies_to value in play for splits, so two splits sharing the same
-- product_name always get the same stage-1 outcome — there's no "partially
-- covered" product_name the way a merchant can be partially covered by an
-- account_name/source-targeting rule. The majority-vote reference-building
-- shape is kept anyway (cheap, and correct even if that assumption ever
-- changes), mirroring the transaction model's structure.
--
-- Grain: at most one row per split_id, same contract as stage 1 — a split
-- absent here (and from stage 1) is still uncategorized, ready for the
-- LLM-fallback stage.
--
-- Requires `pf enrich` to have run (product_embeddings populated) before this
-- model has anything to match; with no embeddings yet it safely resolves to
-- zero rows, same as stage 1 resolves to zero rows if no rule ever matches.

with splits as (
    select split_id, product_name
    from {{ ref('silver_amazon_splits') }}
),

stage1 as (
    select split_id, category_id
    from {{ ref('silver_split_categories') }}
),

embeddings as (
    select product_name, embedding
    from {{ source('app', 'product_embeddings') }}
    where model = '{{ var("embedding_model", "nomic-embed-text") }}'
),

product_votes as (
    select s.product_name, s1.category_id, count(*) as votes
    from splits as s
    inner join stage1 as s1 using (split_id)
    group by s.product_name, s1.category_id
),

reference as (
    select pv.product_name, pv.category_id, e.embedding
    from product_votes as pv
    inner join embeddings as e using (product_name)
    qualify row_number() over (
        partition by pv.product_name order by pv.votes desc, pv.category_id
    ) = 1
),

uncategorized_products as (
    select distinct s.product_name
    from splits as s
    where s.split_id not in (select split_id from stage1)
),

candidates as (
    select e.product_name, e.embedding
    from embeddings as e
    where e.product_name in (select product_name from uncategorized_products)
),

matches as (
    select
        c.product_name,
        r.product_name as matched_product,
        r.category_id,
        list_cosine_similarity(c.embedding, r.embedding) as similarity
    from candidates as c
    cross join reference as r
),

best as (
    select *
    from (
        select *, row_number() over (partition by product_name order by similarity desc) as rnk
        from matches
    )
    where rnk = 1
    and similarity >= {{ var('embedding_confidence_threshold', 0.80) }}
)

select
    s.split_id,
    b.category_id,
    b.matched_product,
    'embedding' as categorization_source,
    b.similarity as categorization_confidence
from splits as s
inner join best as b using (product_name)
where s.split_id not in (select split_id from stage1)
