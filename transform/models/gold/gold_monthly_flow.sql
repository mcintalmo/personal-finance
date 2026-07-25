-- Net cash flow by calendar month: powers the Phase 6 overview dashboard's
-- "spend over time" chart and net-flow headline. Reads silver_transactions
-- directly (not gold_line_items) — a split doesn't change how much money
-- actually left an account on a given day, only how that spend is
-- categorized, so the whole-transaction amount is the right grain for a
-- cash-flow-over-time view. Excludes is_transfer, same convention as every
-- other spend/income measure in this project.

select
    date_trunc('month', posted_on) as month,
    sum(case when flow = 'outflow' then -amount else 0 end) as total_outflow,
    sum(case when flow = 'inflow' then amount else 0 end) as total_inflow,
    sum(amount) as net_amount,
    count(*) as transaction_count
from {{ ref('silver_transactions') }}
where not is_transfer
group by date_trunc('month', posted_on)
