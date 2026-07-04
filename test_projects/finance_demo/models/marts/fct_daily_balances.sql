-- Daily end-of-day account balances joined to account attributes.
-- The grain is (account_id, balance_date). eod_balance is SEMI-ADDITIVE:
-- summable across accounts, but NOT across balance_date (you take the last
-- value in a period, not the sum). MetricFlow enforces that via
-- non_additive_dimension in sem_daily_balances.yml.
select
    b.account_id,
    b.balance_date,
    b.eod_balance,
    a.account_name,
    a.region
from {{ ref('stg_daily_balances') }} b
inner join {{ ref('stg_accounts') }} a
    on b.account_id = a.account_id
