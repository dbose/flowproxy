with source as (
    select * from {{ ref('raw_accounts') }}
)

select
    cast(account_id as integer)   as account_id,
    account_name,
    region,
    cast(opened_on as date)       as opened_on
from source
