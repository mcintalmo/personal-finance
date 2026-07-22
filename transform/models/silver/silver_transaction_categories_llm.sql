-- Stage 3 of the categorization cascade: local-LLM fallback. Picks up
-- merchants stages 1 (rules) and 2 (embedding similarity) didn't match,
-- classified by a local Ollama chat model (cached by `pf classify` — see
-- personal_finance.llm_categorize), and assigns each the model's chosen
-- category when its self-reported confidence clears
-- `llm_confidence_threshold`.
--
-- Grain: at most one row per transaction_id, same contract as stages 1-2 — a
-- transaction absent here (and from both prior stages) is still
-- uncategorized, ready for the human-review stage (see TODO.md).
--
-- Requires `pf classify` to have run (merchant_llm_categories populated)
-- before this model has anything to assign; with no classifications yet it
-- safely resolves to zero rows, same as stages 1-2 do with nothing to match.

with tx as (
    select transaction_id, merchant_name
    from {{ ref('silver_transactions') }}
    where merchant_name is not null
),

already_categorized as (
    select transaction_id from {{ ref('silver_transaction_categories') }}
    union
    select transaction_id from {{ ref('silver_transaction_categories_embedding') }}
),

classifications as (
    select merchant_name, category_id, confidence
    from {{ source('app', 'merchant_llm_categories') }}
    where model = '{{ var("llm_model", "qwen2.5:3b") }}'
    and confidence >= {{ var('llm_confidence_threshold', 0.50) }}
)

select
    t.transaction_id,
    c.category_id,
    'llm' as categorization_source,
    c.confidence as categorization_confidence
from tx as t
inner join classifications as c using (merchant_name)
where t.transaction_id not in (select transaction_id from already_categorized)
