with source as (
    select * from {{ ref('raw_transactions') }}
)

select
    cast(transaction_id as integer)   as transaction_id,
    cast(account_id as integer)       as account_id,
    cast(transaction_date as date)    as transaction_date,
    cast(amount as double)            as amount,
    transaction_type
from source
