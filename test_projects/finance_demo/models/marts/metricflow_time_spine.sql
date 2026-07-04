-- MetricFlow time spine (daily grain). Required for time-based metric
-- aggregation and for the non_additive_dimension window resolution.
-- DuckDB's range() generates the calendar; production warehouses use their
-- native date-generation function via the same dbt model.
with days as (
    select
        cast(range as date) as date_day
    from range(
        date '2023-01-01',
        date '2025-01-01',
        interval 1 day
    )
)

select date_day from days
