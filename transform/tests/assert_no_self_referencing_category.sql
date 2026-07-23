-- gold_category_paths and gold_category_ancestors both walk parent_id
-- recursively with no cycle guard; the existing relationships test on
-- parent_id only checks it references a real category, which a
-- self-referencing row (parent_id = id) would pass trivially while sending
-- the recursive walk into an infinite loop. Fails (returns a row) if any
-- category is its own parent.

select id
from {{ ref('silver_categories') }}
where parent_id = id
