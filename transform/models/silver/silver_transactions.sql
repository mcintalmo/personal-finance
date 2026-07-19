-- All ingested transactions, cleaned and unioned across every source.
--
-- Bronze is the raw Parquet landing (dlt, Phase 2). Cleaning done here:
--   * dedup on row_hash (the bronze idempotency key) — bronze append is
--     already idempotent, so this is defensive and makes the grain explicit;
--   * type normalization — amount to a 2dp money decimal, description trimmed
--     to NULL when blank, currency upper-cased;
--   * sign convention — amounts are already signed at ingest (negative =
--     outflow), uniform across sources; we surface an explicit `flow` label.
--
-- row_hash is globally unique per logical transaction (it is keyed by source
-- and account), so it is the natural grain and becomes `transaction_id`.

with bronze as (
    select * from {{ source('bronze', 'transactions') }}
),

deduped as (
    select *
    from bronze
    qualify row_number() over (partition by row_hash order by ingested_at desc) = 1
)

select
    row_hash as transaction_id,
    source,
    account_name,
    account_type,
    upper(currency) as currency,
    posted_on,
    cast(amount as decimal(18, 2)) as amount,
    case when amount < 0 then 'outflow' else 'inflow' end as flow,
    nullif(trim(description_raw), '') as description_raw,
    external_id,
    source_file,
    ingested_at
from deduped
