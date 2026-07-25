-- The forecast decomposition is the whole point of the model: every row's
-- predicted_amount must be exactly its committed + variable parts, and the
-- interval must bracket the prediction. If a future change to
-- personal_finance.forecast lets these drift apart, the "committed vs.
-- variable" breakdown the UI shows would no longer add up to the headline
-- number it sits under. Fails (returns rows) if either invariant breaks.
--
-- The sum is compared exactly, not with a tolerance, because
-- personal_finance.forecast builds predicted_amount by ADDING THE QUANTIZED
-- parts rather than quantizing their float sum. That distinction matters:
-- quantizing the sum lets both halves round up while the total rounds down
-- (0.125 + 0.125 -> 0.26 vs 0.25), which would break this test by a cent on
-- ordinary data. A pydantic validator on Forecast now enforces the same
-- invariant at construction, so this is the second line of defence rather
-- than the first.

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
