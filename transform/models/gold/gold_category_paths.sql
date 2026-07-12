-- Full slash-separated path per category (apples -> essentials/groceries/apples),
-- ready for hierarchy rollups and the sunburst drill-down.

with recursive category_paths as (
    select
        id,
        name,
        parent_id,
        name as path,
        0 as depth
    from {{ ref('silver_categories') }}
    where parent_id is null

    union all

    select
        child.id,
        child.name,
        child.parent_id,
        parent.path || '/' || child.name as path,
        parent.depth + 1 as depth
    from {{ ref('silver_categories') }} as child
    inner join category_paths as parent on child.parent_id = parent.id
)

select
    id,
    name,
    parent_id,
    path,
    depth
from category_paths
