-- Every transaction categorized so far, across every cascade stage. Human
-- corrections (see silver_transaction_categories_human) are the highest
-- priority — a human can override a wrong rule/embedding/LLM assignment, not
-- just fill a gap — so this is unioned first and every automated stage below
-- excludes what it covers. The automated stages 1-3 are additive among
-- themselves (each only covers what prior stages missed entirely), so no
-- dedup is needed between them.

with human as (
    select transaction_id, category_id, categorization_source, categorization_confidence
    from {{ ref('silver_transaction_categories_human') }}
)

select * from human

union all

select transaction_id, category_id, categorization_source, categorization_confidence
from {{ ref('silver_transaction_categories') }}
where transaction_id not in (select transaction_id from human)

union all

select transaction_id, category_id, categorization_source, categorization_confidence
from {{ ref('silver_transaction_categories_embedding') }}
where transaction_id not in (select transaction_id from human)

union all

select transaction_id, category_id, categorization_source, categorization_confidence
from {{ ref('silver_transaction_categories_llm') }}
where transaction_id not in (select transaction_id from human)
