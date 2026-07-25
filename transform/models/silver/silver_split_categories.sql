-- Line-item categorization: the same rules-based cascade stage used for
-- transactions (silver_transaction_categories.sql), applied to
-- silver_amazon_splits' product_name instead of merchant_name — this is what
-- makes "how much did I spend on apples this year" answerable at the
-- individual-item level, not just per-charge.
--
-- Only rules.yaml entries with applies_to: product_name are relevant here
-- (transaction-targeting rules — merchant_name, description_raw, source,
-- account_name — are for silver_transaction_categories, not this model); no
-- UNION ALL per-field branching is needed like the transaction model's,
-- since product_name is the only split-level field today.
--
-- Grain: at most one row per split_id — a split absent here is
-- uncategorized by this stage; embedding-similarity / LLM-fallback /
-- human-review stages pick up the remainder — see
-- silver_split_categories_embedding/_llm/_human and silver_split_categories_all.

with splits as (
    select split_id, product_name
    from {{ ref('silver_amazon_splits') }}
),

rules as (
    select * from {{ source('app', 'rules') }}
    where applies_to = 'product_name'
),

matched as (
    select
        s.split_id,
        r.category_id,
        r.id as rule_id,
        r.pattern,
        r.priority
    from splits as s
    inner join rules as r on regexp_matches(s.product_name, r.pattern)
    qualify {{ first_match_wins('split_id', 'priority') }}
)

select
    split_id,
    category_id,
    rule_id,
    pattern as matched_pattern,
    'rule' as categorization_source,
    1.0 as categorization_confidence
from matched
