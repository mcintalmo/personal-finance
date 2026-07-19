-- Public cleaned transaction grain: the staged/cleaned transactions plus an
-- `is_transfer` flag marking legs of a detected inter-account transfer (see
-- silver_transfers). Spend/income measures should exclude is_transfer rows so
-- moving money between your own accounts doesn't look like activity.

with base as (
    select * from {{ ref('stg_transactions') }}
),

transfer_legs as (
    select outflow_id as transaction_id from {{ ref('silver_transfers') }}
    union
    select inflow_id as transaction_id from {{ ref('silver_transfers') }}
)

select
    base.*,
    legs.transaction_id is not null as is_transfer
from base
left join transfer_legs as legs using (transaction_id)
