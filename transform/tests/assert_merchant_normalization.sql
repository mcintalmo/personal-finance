-- Unit test for the normalize_merchant macro. Each case pairs a raw descriptor
-- with the merchant it must clean to (including processor-prefix shapes the
-- synth fixtures don't emit). The test fails if any case doesn't match: dbt
-- flags a singular test that returns rows.

with cases as (
    select * from (values
        ('STARBUCKS #9985', 'STARBUCKS'),
        ('ALDI 73011', 'ALDI'),
        ('CHEVRON 0093 BELLEVUE WA', 'CHEVRON'),
        ('SAFEWAY STORE 1442', 'SAFEWAY'),
        ('TRADER JOE''S #0552 SEATTLE WA', 'TRADER JOE''S'),
        ('NETFLIX.COM', 'NETFLIX'),
        ('SPOTIFY USA', 'SPOTIFY'),
        ('VENMO CASHOUT PPD ID: 5264681992', 'VENMO CASHOUT'),
        ('SQ *BLUE BOTTLE COFFEE', 'BLUE BOTTLE COFFEE'),
        ('PP*GRUBHUB', 'GRUBHUB'),
        ('PAYPAL *STEAM GAMES', 'STEAM GAMES'),
        ('TST* THE PINK DOOR', 'THE PINK DOOR'),
        ('KROGER #718', 'KROGER'),
        ('CHASE CREDIT CRD AUTOPAY', 'CHASE CREDIT CRD AUTOPAY')
    ) as t(raw, expected)
)

select
    raw,
    expected,
    {{ normalize_merchant('raw') }} as actual
from cases
where {{ normalize_merchant('raw') }} is distinct from expected
