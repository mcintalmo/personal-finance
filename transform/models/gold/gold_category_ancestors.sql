-- Closure table: every category paired with itself and each of its
-- ancestors, up to the taxonomy root. Powers gold_category_rollups (summing
-- a leaf category's activity into every level above it) without repeating
-- the recursive walk in every downstream model.

with recursive ancestors as (
    select id as category_id, id as ancestor_id
    from {{ ref('silver_categories') }}

    union all

    select a.category_id, c.parent_id as ancestor_id
    from ancestors as a
    inner join {{ ref('silver_categories') }} as c on c.id = a.ancestor_id
    where c.parent_id is not null
)

select category_id, ancestor_id
from ancestors
