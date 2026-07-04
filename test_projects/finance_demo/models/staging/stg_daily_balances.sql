with source as (
    select * from {{ ref('raw_daily_balances') }}
)

select
    cast(account_id as integer)     as account_id,
    cast(balance_date as date)      as balance_date,
    cast(eod_balance as double)     as eod_balance
from source
