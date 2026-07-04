# WS1 — DuckDB golden-numbers harness (complete)

Implements [ADR-0003](adr/0003-duckdb-golden-numbers-test-strategy.md).

## What shipped

- **`test_projects/finance_demo/`** — a real dbt-core 1.11 project:
  - seeds: `raw_accounts`, `raw_daily_balances` (182 daily EOD rows), `raw_transactions`
  - staging views + mart tables + `metricflow_time_spine`
  - **`models/semantics/sem_*.yml`**: three semantic models
    - `accounts` (dimension model — `account` primary; owns region/account_name)
    - `daily_balances` — semi-additive `month_end_balance` measure via
      **`non_additive_dimension: {name: balance_date, window_choice: max}}`**
    - `transactions` — additive `total_amount` / `transaction_count`
  - `saved_queries.yml` — two certified saved queries (feed WS3 catalog)
- **`tests/conftest.py`** — builds the project into DuckDB once per session,
  boots a real (non-dry-run) `SemanticCompiler` against it.
- **`tests/test_l2_manifest.py`** — registry classification from the real manifest.
- **`tests/test_l3_golden_numbers.py`** — exact-value assertions.
- **`engine/compiler.py`** — rewired to the real MetricFlow 0.211 /
  dbt-metricflow 0.13 API (`dbtProjectMetadata.load_from_paths`,
  `MetricFlowQueryRequest.create`, `engine.query(...).result_df`); added
  `execute_request()` (plan **and** execute, returns rows) and a boot-time
  semantic-stack **compatibility-matrix assertion** (ADR-0002).

## The guardrail, proven with real numbers

| Query (grouped by month) | Result | Meaning |
|---|---|---|
| `account_balance` (semi-additive) | EMEA 1500/1200/2000, AMER 4000/4500/3000 | **end-of-month** balance |
| `naive_balance_total` (sum-over-days) | 31500, 43200, … | wrong — ~30× inflated |
| `account_balance` by month only | 5500 / 5700 / 5000 | additive **across accounts** |

The semi-additive metric ignores a BI-emitted `SUM()` because the proxy
re-plans through MetricFlow; summing over days is impossible through the
metric. This is the WS1 thesis, executed end-to-end against DuckDB.

## Bug the test caught (why L3 exists)

First run: `total_transactions` by region returned **95550** instead of 1050.
Cause: `region` was declared on *both* the transactions and daily_balances
models, so MetricFlow joined transactions to the 182-row daily table →
fan-out double-count. Fix: move account attributes to a dedicated `accounts`
dimension model (`account` primary), `primary_entity: account` on the fact
models. A mock executor would never have surfaced this — exactly the
wrong-numbers class L3 is designed to catch.

## Locked matrix (verified against the real index)

`dbt-core 1.11.12` · `metricflow 0.211.0` · `dbt-metricflow 0.13.0` ·
`dbt-semantic-interfaces 0.9.0` · `dbt-duckdb 1.10.1` · `sqlglot 26.33.0`.
Pinned in `pyproject.toml`, locked in `uv.lock`, asserted at boot.

## Run it

```bash
uv sync --extra duckdb --group dev
uv run pytest tests/ -v          # L2 + L3 (builds finance_demo into DuckDB)
uv run python -m tests.smoke_test  # L1 wire path (dry-run + mock)
```

## Notes for later workstreams

- `execute_request()` already accepts `where_constraints`, `order_by`,
  `limit` (MetricFlow-native) — WS2 wires sqlglot predicates into these.
- MetricFlow's `.query()` owns execution on the real path, so the separate
  `WarehouseExecutor` is now **dry-run/mock only**; the server must branch
  real→`compiler.execute_request` vs dry-run→mock (WS-integration follow-up).
- Output column order is MetricFlow's (dims then metrics); the wire layer must
  re-project to the client's SELECT order.
