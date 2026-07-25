-- Every categorized line item (gold_line_items — a split when its parent
-- transaction was decomposed, else the whole transaction) belongs to
-- exactly one leaf category, which rolls up into exactly one root (roots
-- partition the taxonomy) — so summing every root category's
-- transaction_count must equal the total categorized line item count. Fails
-- (returns a row) if the rollup logic double-counts or drops a line item
-- anywhere.
--
-- root_total is coalesced to -1 (not 0) rather than left null: a taxonomy
-- with no root category at all is itself the failure this test should catch,
-- and `null != categorized_count` would otherwise evaluate to unknown and
-- silently drop the row instead of failing.

with totals as (
    select count(*) as categorized_count
    from {{ ref('gold_line_items') }}
    where category_id is not null
),

root_sum as (
    select coalesce(sum(transaction_count), -1) as root_total
    from {{ ref('gold_category_rollups') }}
    where depth = 0
)

select *
from totals, root_sum
where categorized_count != root_total
