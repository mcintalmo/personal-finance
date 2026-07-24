-- Public cleaned transaction grain: the staged/cleaned transactions, with
-- merchant_name resolved through config-driven aliases (merchants.yaml) and
-- then human-confirmed embedding-similarity merges (merchant_merges — see
-- personal_finance.merchant_merge) on top of the generic normalize_merchant
-- cleanup, plus an `is_transfer` flag marking legs of a detected
-- inter-account transfer (see silver_transfers). Spend/income measures
-- should exclude is_transfer rows so moving money between your own accounts
-- doesn't look like activity.

with base as (
    select * from {{ ref('stg_transactions') }}
),

aliases as (
    select pattern, canonical_name, priority
    from {{ source('app', 'merchant_aliases') }}
),

-- First-match-wins by priority (file order), same macro as stage 1 of the
-- categorization cascade (silver_transaction_categories.sql) — one row per
-- transaction whose merchant_name matched some alias pattern.
matched_aliases as (
    select
        base.transaction_id,
        aliases.canonical_name
    from base
    inner join aliases
        on base.merchant_name is not null
        and regexp_matches(base.merchant_name, aliases.pattern)
    qualify {{ first_match_wins('base.transaction_id', 'aliases.priority') }}
),

aliased as (
    select
        base.transaction_id,
        coalesce(matched_aliases.canonical_name, base.merchant_name) as merchant_name
    from base
    left join matched_aliases using (transaction_id)
),

-- Human-confirmed merges (pf review merge-candidates / merge), keyed by
-- merchant_name (exact match, not regex). Ranking happens over EVERY
-- decision (accepted or rejected) ordered by recency — not just the
-- accepted ones — so a later reject overrides a stale earlier accept
-- instead of being filtered out before it ever gets to compete; only once
-- the single latest decision per merchant_name is picked do we keep it if
-- it was an accept. Single-hop only: a merge target that is itself later
-- merged elsewhere is not chased further.
merges as (
    select merchant_name, canonical_name
    from (
        select *
        from {{ source('app', 'merchant_merges') }}
        qualify {{ first_match_wins('merchant_name', 'created_at desc') }}
    )
    where status = 'accepted'
),

transfer_legs as (
    select outflow_id as transaction_id from {{ ref('silver_transfers') }}
    union
    select inflow_id as transaction_id from {{ ref('silver_transfers') }}
)

select
    base.* exclude (merchant_name),
    coalesce(merges.canonical_name, aliased.merchant_name) as merchant_name,
    legs.transaction_id is not null as is_transfer
from base
left join aliased using (transaction_id)
left join merges on merges.merchant_name = aliased.merchant_name
left join transfer_legs as legs using (transaction_id)
