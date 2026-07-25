-- Money-flow edges for the Phase 6 Sankey diagram: income -> account ->
-- top-level category subtree (essentials/non-essentials, not every leaf —
-- a Sankey with hundreds of leaf nodes is unreadable; the sunburst already
-- covers full-depth drill-down). One row per edge, valued in dollars, ready
-- to hand straight to a Plotly Sankey trace (source/target/value).
--
-- Two edge kinds, unioned by a literal `stage` discriminant so a consumer
-- can build node lists without inspecting values:
--   income   : "Income" -> account_name, one edge per account, valued at
--              that account's total non-transfer inflow.
--   spend    : account_name -> top-level category name, valued at that
--              account's outflow rolled up to depth=0 (essentials,
--              non-essentials, ...) via gold_line_items + gold_category_rollups'
--              own ancestor walk (gold_category_ancestors), so a leaf-level
--              "apples" line item still lands on its top-level ancestor here.

with income_edges as (
    select
        'income' as stage,
        'Income' as source_node,
        t.account_name as target_node,
        sum(t.amount) as value
    from {{ ref('silver_transactions') }} as t
    where t.flow = 'inflow' and not t.is_transfer
    group by t.account_name
    having sum(t.amount) > 0
),

line_items_by_account as (
    select
        t.account_name,
        li.line_item_id,
        li.category_id,
        li.amount
    from {{ ref('gold_line_items') }} as li
    inner join {{ ref('silver_transactions') }} as t using (transaction_id)
    where li.category_id is not null and li.amount < 0
),

top_level as (
    select category_id, ancestor_id as top_category_id
    from {{ ref('gold_category_ancestors') }} as anc
    inner join {{ ref('gold_category_paths') }} as gc on gc.id = anc.ancestor_id
    where gc.depth = 0
),

spend_edges as (
    select
        'spend' as stage,
        li.account_name as source_node,
        gc.name as target_node,
        sum(-li.amount) as value
    from line_items_by_account as li
    inner join top_level as tl using (category_id)
    inner join {{ ref('gold_category_paths') }} as gc on gc.id = tl.top_category_id
    group by li.account_name, gc.name
)

select * from income_edges
union all
select * from spend_edges
