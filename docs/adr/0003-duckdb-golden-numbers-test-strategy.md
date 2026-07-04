# ADR-0003: DuckDB-backed golden-numbers integration testing

Date: 2026-07-04 · Status: **Accepted**

## Context

The existing smoke test (L1) proves wire framing only. The success criterion —
"a real dbt project with `sem_*.yml` queried by analysts, with guardrails such
as `non_additive_dimension` enforced" — requires executing MetricFlow-compiled
SQL against real data and asserting *numeric* correctness. Snowflake/Redshift
are unavailable in CI and in the air gap.

## Decision

Adopt **`dbt-duckdb`** as the CI/integration warehouse and build a four-layer
test pyramid:

| Layer | Proves | Backend |
|---|---|---|
| L1 wire smoke (exists) | protocol framing, error paths | none |
| L2 manifest | `dbt parse` on the committed fixture project → registry correct | none |
| L3 **golden numbers** | analyst SQL → MetricFlow plan → *executed* → exact aggregates | DuckDB |
| L4 driver conformance | replayed QuickSight-JDBC / Power BI-Npgsql packet corpora | DuckDB |

The fixture is a **real, committed dbt-core 1.11 project**
(`test_projects/finance_demo/`) with seeds, staging models, and
`models/semantics/sem_*.yml` files defining:

- an additive measure/metric (e.g. `transaction_amount` → `total_transactions`)
- a **semi-additive** measure: `eod_balance` with
  `non_additive_dimension: {name: metric_time, window_choice: max}` → `mrr` /
  account-balance style metric
- ≥ 2 semantic models joined via entities (exercises join-path planning)
- ≥ 1 `saved_query` (feeds the virtual catalog, ADR-0004)

Canonical L3 assertion: grouping the semi-additive metric by
`metric_time__month` over 3 months of seeded daily balances returns the
**end-of-window balance**, not the ~30-day sum — proving the YAML guardrail
survives the full git → parse → proxy → BI-SQL → warehouse path, and proving
a BI-emitted `SUM(...)` wrapper cannot defeat it (the wrapper is unwrapped and
re-planned through the metric definition).

## Consequences

- `dbt-duckdb` + `duckdb` join requirements (dev/test extra); fully offline.
- The dialect under test is DuckDB, not Snowflake/Redshift — dialect-specific
  rendering bugs are out of L3's scope and covered by pre-prod UAT.
- L4 corpora are captured once from real drivers and committed as fixtures;
  they double as regression tests for the catalog emulation.

## Alternatives considered

- **Mocked executor for everything**: cannot catch wrong-numbers bugs — the
  highest-severity failure class for a bank.
- **Postgres-in-Docker as warehouse**: viable but heavier than DuckDB and
  adds no fidelity for MetricFlow planning.
