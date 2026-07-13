-- Silver view over the seeded category taxonomy.
-- Referential integrity for the hierarchy is enforced HERE (schema.yml
-- relationships test), not as a declared FK — see ddl.py for why.

select
    id,
    name,
    parent_id,
    description,
    note,
    created_at
from {{ source('app', 'categories') }}
