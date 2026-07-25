-- Forecast mart: the app-computed forecasts (personal_finance.forecast,
-- `pf forecast`) joined to the taxonomy so a consumer gets a category path
-- without a second lookup. Same shape as every other Python-computes ->
-- dbt-publishes stage in this project (merchant_embeddings, merchant_llm_
-- categories): the statistics happen in Python because SQL can't fit a Theta
-- model, and dbt's job is to expose the result as a tested, joined mart.
--
-- Each row is one (series, forecast month). predicted_amount is always
-- committed_amount + variable_amount, and the interval covers the VARIABLE
-- component only — committed spend (rent, subscriptions) is known, so it
-- shifts the band without widening it. A category that is mostly
-- subscriptions therefore gets a tight interval and a mostly-discretionary
-- one gets an honest wide interval.

select
    f.id as forecast_id,
    f.series_kind,
    f.series_key,
    f.series_label,
    f.category_id,
    gc.path as category_path,
    f.period_start,
    f.horizon,
    f.committed_amount,
    f.variable_amount,
    f.predicted_amount,
    f.lower_bound,
    f.upper_bound,
    f.interval_level,
    f.model_name,
    f.mase,
    f.trend,
    f.trained_through
from {{ source('app', 'forecasts') }} as f
left join {{ ref('gold_category_paths') }} as gc on gc.id = f.category_id
