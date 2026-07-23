-- Category rollups: one row per taxonomy category (every level, leaf or
-- branch), aggregating every categorized transaction assigned to that
-- category *or any of its descendants* — e.g. essentials/groceries/apples
-- activity counts toward essentials/groceries and essentials too. Powers the
-- sunburst drill-down and budget-vs-actual views (Phase 6).
--
-- Excludes is_transfer transactions, same convention as every other
-- spend/income measure in this project (silver_transactions.is_transfer) —
-- moving money between your own accounts isn't spend or income.
--
-- Every taxonomy category gets a row, even with zero categorized activity
-- anywhere in its subtree (zeroed out), so a dashboard's category dimension
-- is always complete. A transaction not yet categorized by any cascade stage
-- (see silver_transaction_categories_all) simply isn't counted anywhere yet.

with categorized as (
    select a.transaction_id, a.category_id, t.amount, t.flow
    from {{ ref('silver_transaction_categories_all') }} as a
    inner join {{ ref('silver_transactions') }} as t using (transaction_id)
    where not t.is_transfer
),

rolled_up as (
    select
        anc.ancestor_id as category_id,
        c.transaction_id,
        c.amount,
        c.flow
    from categorized as c
    inner join {{ ref('gold_category_ancestors') }} as anc using (category_id)
)

select
    gc.id as category_id,
    gc.name,
    gc.path,
    gc.depth,
    coalesce(count(r.transaction_id), 0) as transaction_count,
    coalesce(sum(case when r.flow = 'outflow' then -r.amount else 0 end), 0) as total_outflow,
    coalesce(sum(case when r.flow = 'inflow' then r.amount else 0 end), 0) as total_inflow,
    coalesce(sum(r.amount), 0) as net_amount
from {{ ref('gold_category_paths') }} as gc
left join rolled_up as r on r.category_id = gc.id
group by gc.id, gc.name, gc.path, gc.depth
