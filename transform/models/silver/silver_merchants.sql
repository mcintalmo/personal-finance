-- Merchant dimension: one row per distinct cleaned merchant, derived from the
-- normalized descriptors on silver_transactions (see the normalize_merchant
-- macro). merchant_id is a deterministic hash of the name, so it is stable
-- across runs and joinable back to transactions by merchant_name.

with transactions as (
    select *
    from {{ ref('silver_transactions') }}
    where merchant_name is not null
)

select
    md5(merchant_name) as merchant_id,
    merchant_name,
    count(*) as transaction_count,
    sum(case when flow = 'outflow' then -amount else 0 end) as total_outflow,
    min(posted_on) as first_seen_on,
    max(posted_on) as last_seen_on
from transactions
group by merchant_name
