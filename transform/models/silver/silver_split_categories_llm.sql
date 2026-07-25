-- Stage 3 of the split-categorization cascade: local-LLM fallback — the same
-- approach as silver_transaction_categories_llm, applied to splits/product_name
-- instead of transactions/merchant_name.
--
-- Grain: at most one row per split_id, same contract as stages 1-2 — a split
-- absent here (and from both prior stages) is still uncategorized, ready for
-- the human-review stage.
--
-- Requires `pf classify` to have run (product_llm_categories populated)
-- before this model has anything to assign; with no classifications yet it
-- safely resolves to zero rows, same as stages 1-2 do with nothing to match.

with splits as (
    select split_id, product_name
    from {{ ref('silver_amazon_splits') }}
),

already_categorized as (
    select split_id from {{ ref('silver_split_categories') }}
    union
    select split_id from {{ ref('silver_split_categories_embedding') }}
),

classifications as (
    select product_name, category_id, confidence
    from {{ source('app', 'product_llm_categories') }}
    where model = '{{ var("llm_model", "phi3:mini") }}'
    and confidence >= {{ var('llm_confidence_threshold', 0.50) }}
)

select
    s.split_id,
    c.category_id,
    'llm' as categorization_source,
    c.confidence as categorization_confidence
from splits as s
inner join classifications as c using (product_name)
where s.split_id not in (select split_id from already_categorized)
