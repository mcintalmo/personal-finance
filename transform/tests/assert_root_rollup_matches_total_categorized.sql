-- Every categorized, non-transfer transaction belongs to exactly one leaf
-- category, which rolls up into exactly one root (roots partition the
-- taxonomy) — so summing every root category's transaction_count must equal
-- the total categorized, non-transfer transaction count. Fails (returns a
-- row) if the rollup logic double-counts or drops a transaction anywhere.
--
-- root_total is coalesced to -1 (not 0) rather than left null: a taxonomy
-- with no root category at all is itself the failure this test should catch,
-- and `null != categorized_count` would otherwise evaluate to unknown and
-- silently drop the row instead of failing.

with totals as (
    select count(*) as categorized_count
    from {{ ref('silver_transaction_categories_all') }} as a
    inner join {{ ref('silver_transactions') }} as t using (transaction_id)
    where not t.is_transfer
),

root_sum as (
    select coalesce(sum(transaction_count), -1) as root_total
    from {{ ref('gold_category_rollups') }}
    where depth = 0
)

select *
from totals, root_sum
where categorized_count != root_total
