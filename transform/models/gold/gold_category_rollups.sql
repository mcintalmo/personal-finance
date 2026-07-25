-- Category rollups: one row per taxonomy category (every level, leaf or
-- branch), aggregating every categorized line item (gold_line_items — a
-- split's product_name when a transaction was decomposed by the Amazon
-- cascade, else the whole transaction) assigned to that category *or any of
-- its descendants* — e.g. essentials/groceries/apples activity counts
-- toward essentials/groceries and essentials too. Powers the sunburst
-- drill-down and budget-vs-actual views (Phase 6).
--
-- gold_line_items already excludes is_transfer transactions (moving money
-- between your own accounts isn't spend or income) and already splits a
-- decomposed transaction's amount across its line items, so no transaction
-- is double-counted here even though a split's parent transaction never
-- appears alongside it.
--
-- Every taxonomy category gets a row, even with zero categorized activity
-- anywhere in its subtree (zeroed out), so a dashboard's category dimension
-- is always complete. A line item not yet categorized by any cascade stage
-- (category_id is NULL) simply isn't counted anywhere yet.
--
-- transaction_count is really "line item count" (a split counts on its own,
-- not its parent transaction) — kept as-is rather than renamed, since
-- nothing outside this phase's own new consumers reads it and the
-- semantics only differ once Amazon splits exist.

with categorized as (
    select li.line_item_id, li.category_id, li.amount
    from {{ ref('gold_line_items') }} as li
    where li.category_id is not null
),

rolled_up as (
    select
        anc.ancestor_id as category_id,
        c.line_item_id,
        c.amount
    from categorized as c
    inner join {{ ref('gold_category_ancestors') }} as anc using (category_id)
)

select
    gc.id as category_id,
    gc.parent_id,
    gc.name,
    gc.path,
    gc.depth,
    coalesce(count(r.line_item_id), 0) as transaction_count,
    coalesce(sum(case when r.amount < 0 then -r.amount else 0 end), 0) as total_outflow,
    coalesce(sum(case when r.amount > 0 then r.amount else 0 end), 0) as total_inflow,
    coalesce(sum(r.amount), 0) as net_amount
from {{ ref('gold_category_paths') }} as gc
left join rolled_up as r on r.category_id = gc.id
group by gc.id, gc.parent_id, gc.name, gc.path, gc.depth
