-- The gold-layer "implicit split" union PLAN.md/TODO.md's Phase 5 backlog
-- flagged as not yet built: one row per real spend/income event, at the
-- finest granularity available — a split's product_name/category when a
-- transaction has been decomposed by the Amazon cascade, else the whole
-- transaction itself. This is what makes "spend on apples this year"
-- answerable through the same rollup every other category uses, instead of
-- as a one-off query against silver_amazon_splits.
--
-- A transaction can only be in ONE branch: split_transactions is exactly the
-- set of transaction_ids silver_amazon_splits decomposed, so the two arms
-- are disjoint by construction (no transaction is double-counted).
--
-- category_id is left NULL when a stage hasn't categorized that line item
-- yet — same "absent = uncategorized" contract as every categorization
-- cascade stage; downstream rollups inner-join it away, consistent with how
-- gold_category_rollups always treated an uncategorized transaction.

with split_transactions as (
    select distinct transaction_id from {{ ref('silver_amazon_splits') }}
),

split_line_items as (
    select
        s.split_id as line_item_id,
        s.transaction_id,
        s.amount,
        t.posted_on,
        s.product_name as description,
        sc.category_id,
        sc.categorization_source,
        sc.categorization_confidence
    from {{ ref('silver_amazon_splits') }} as s
    inner join {{ ref('silver_transactions') }} as t using (transaction_id)
    left join {{ ref('silver_split_categories_all') }} as sc using (split_id)
),

transaction_line_items as (
    select
        t.transaction_id as line_item_id,
        t.transaction_id,
        t.amount,
        t.posted_on,
        t.merchant_name as description,
        tc.category_id,
        tc.categorization_source,
        tc.categorization_confidence
    from {{ ref('silver_transactions') }} as t
    left join {{ ref('silver_transaction_categories_all') }} as tc using (transaction_id)
    where not t.is_transfer
    and t.transaction_id not in (select transaction_id from split_transactions)
)

select * from split_line_items
union all
select * from transaction_line_items
