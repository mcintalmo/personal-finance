-- The human-review stage of the split-categorization cascade — corrections
-- recorded via `pf review label --kind split` (see personal_finance.review).
-- Unlike stages 1-3, this isn't additive: it's the highest-priority stage,
-- overriding whatever an earlier stage assigned (see silver_split_categories_all,
-- which unions this first and excludes what it covers from every other stage).
--
-- A split corrected more than once keeps only its latest label.

with human_labels as (
    select
        subject_id as split_id,
        category_id,
        created_at
    from {{ source('app', 'labels') }}
    where subject_kind = 'split'
    qualify row_number() over (partition by subject_id order by created_at desc) = 1
)

select
    split_id,
    category_id,
    'human' as categorization_source,
    1.0 as categorization_confidence
from human_labels
