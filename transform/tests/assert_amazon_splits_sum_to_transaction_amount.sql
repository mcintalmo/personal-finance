-- Every matched transaction's splits must sum to EXACTLY its charge amount
-- (not just approximately, unlike the shipment-vs-item-subtotal reconcile
-- test) — that exact invariant is what makes rollups over splits agree with
-- rollups over transactions. Fails (returns a row) if silver_amazon_splits.sql
-- ever drops the last-item remainder allocation that makes proportional
-- rounding land exactly on the charge amount.

select transaction_id, sum(amount) as split_total, any_value(charge_amount) as charge_amount
from (
    select s.transaction_id, s.amount, t.amount as charge_amount
    from {{ ref('silver_amazon_splits') }} as s
    inner join {{ ref('silver_transactions') }} as t on t.transaction_id = s.transaction_id
)
group by transaction_id
having sum(amount) <> any_value(charge_amount)
