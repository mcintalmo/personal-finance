-- Every embedding-stage assignment must clear the confidence threshold and
-- stay within a valid cosine-similarity range. Fails (returns rows) if not.

select transaction_id, categorization_confidence
from {{ ref('silver_transaction_categories_embedding') }}
where categorization_confidence < {{ var('embedding_confidence_threshold', 0.80) }}
or categorization_confidence > 1.0
