-- Every split categorized so far, across every cascade stage — the same
-- shape as silver_transaction_categories_all. Human corrections are the
-- highest priority, so this is unioned first and every automated stage below
-- excludes what it covers; the automated stages 1-3 are additive among
-- themselves (each only covers what prior stages missed entirely), so no
-- dedup is needed between them.

with human as (
    select split_id, category_id, categorization_source, categorization_confidence
    from {{ ref('silver_split_categories_human') }}
)

select * from human

union all

select split_id, category_id, categorization_source, categorization_confidence
from {{ ref('silver_split_categories') }}
where split_id not in (select split_id from human)

union all

select split_id, category_id, categorization_source, categorization_confidence
from {{ ref('silver_split_categories_embedding') }}
where split_id not in (select split_id from human)

union all

select split_id, category_id, categorization_source, categorization_confidence
from {{ ref('silver_split_categories_llm') }}
where split_id not in (select split_id from human)
