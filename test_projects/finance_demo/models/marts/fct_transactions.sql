-- Transaction fact. amount is fully ADDITIVE across every dimension.
select
    t.transaction_id,
    t.account_id,
    t.transaction_date,
    t.amount,
    t.transaction_type,
    a.account_name,
    a.region
from {{ ref('stg_transactions') }} t
inner join {{ ref('stg_accounts') }} a
    on t.account_id = a.account_id
