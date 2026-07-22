-- Every LLM-stage assignment must clear the confidence threshold and stay
-- within a valid [0, 1] range. Fails (returns rows) if not.

select transaction_id, categorization_confidence
from {{ ref('silver_transaction_categories_llm') }}
where categorization_confidence < {{ var('llm_confidence_threshold', 0.50) }}
or categorization_confidence > 1.0
