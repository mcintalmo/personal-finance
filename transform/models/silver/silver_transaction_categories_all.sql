-- Every transaction categorized so far, across every cascade stage. Each
-- stage model guarantees at most one row per transaction_id and only includes
-- transactions it categorized (excluding what prior stages already caught),
-- so the stages are disjoint by construction and a plain UNION ALL is safe —
-- no dedup needed. Adding a future stage (LLM fallback, human review) means
-- adding one more UNION ALL branch here, with that stage's own model
-- excluding transaction_ids already present in this view.

select transaction_id, category_id, categorization_source, categorization_confidence
from {{ ref('silver_transaction_categories') }}

union all

select transaction_id, category_id, categorization_source, categorization_confidence
from {{ ref('silver_transaction_categories_embedding') }}
