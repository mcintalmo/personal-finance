-- The forecast decomposition is the whole point of the model: every row's
-- predicted_amount must be exactly its committed + variable parts, and the
-- interval must bracket the prediction. If a future change to
-- personal_finance.forecast lets these drift apart, the "committed vs.
-- variable" breakdown the UI shows would no longer add up to the headline
-- number it sits under. Fails (returns rows) if either invariant breaks.
--
-- The sum is compared exactly, not with a tolerance: all three columns are
-- DECIMAL(18,2) written from the same quantized Python Decimals, so there is
-- no float error to absorb here.

select
    forecast_id,
    committed_amount,
    variable_amount,
    predicted_amount,
    lower_bound,
    upper_bound
from {{ ref('gold_forecasts') }}
where predicted_amount != committed_amount + variable_amount
   or lower_bound > predicted_amount
   or upper_bound < predicted_amount
