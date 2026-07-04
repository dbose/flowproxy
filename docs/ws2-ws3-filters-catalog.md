# WS2 + WS3 — Filters & the analyst exploration surface (complete)

Implements [ADR-0005](adr/0005-where-clause-to-metricflow-filters.md),
[ADR-0010](adr/0010-full-semantic-exposure-catalog.md),
[ADR-0011](adr/0011-auto-cube-exploration-quicksight.md).

## The analyst experience, delivered

An analyst points QuickSight at the PostgreSQL datasource and:

1. **sees the dbt semantic models as cubes** — `information_schema.tables`
   returns one table per model (+ `all_metrics` + saved queries) under schema
   `semantic_layer`;
2. **sees only valid fields per cube** — pre-scoping means the balances cube
   has no `transaction__*` columns, so invalid slices can't be built;
3. **drags metrics + dimensions onto a visual** — QuickSight's `SELECT … GROUP
   BY` is re-planned through MetricFlow;
4. **filters and sorts** — WHERE/ORDER/LIMIT translate to MetricFlow
   constructs;
5. **gets guardrail-correct numbers** — a semi-additive balance stays
   end-of-period even if QuickSight wraps it in SUM().

Proven live: booting the real server and querying `daily_balances` grouped by
region returns AMER 3000 / EMEA 2000 (end-of-period), over the socket.

## WS2 — clause translation (`engine/filters.py`)

`ClauseTranslator` turns sqlglot WHERE/ORDER/LIMIT into MetricFlow request
params — **never** raw SQL splicing (ADR-0005):

| SQL | → MetricFlow |
|---|---|
| `region = 'EMEA'` | `{{ Dimension('account__region') }} = 'EMEA'` |
| `region IN ('EMEA','AMER')` | templated IN filter |
| `metric_time >= '2024-02-01'` | `time_constraint_start` |
| `metric_time BETWEEN a AND b` | `time_constraint_start/end` |
| `ORDER BY 2 DESC` | `order_by_names=['-<col>']` (ordinal resolved) |
| `LIMIT n` | `limit=min(n, MAX_ROWS)` |

**Security**: literals are type-validated and single-quoted with internal
quotes doubled; an injection payload like `x' OR 1=1--` stays inside the
string. **Deny-by-default**: OR-trees, BETWEEN on categoricals, unknown
columns, and functions-on-columns are rejected (SQLSTATE `0A000`), never
silently dropped.

The registry now tracks TIME dimensions authoritatively (no name-suffix
guessing) and honors dbt's model-level `primary_entity` key.

## WS3 — virtual catalog (`engine/catalog.py` + `network/catalog.py`)

- **`engine/catalog.py`** — projects the manifest into cubes. Each cube's
  columns = its metrics + `valid_group_bys(metrics)` (the shared WS-primitive:
  MetricFlow linkable-elements resolution). Deterministic per-manifest OIDs;
  metric→NUMERIC, time dim→TIMESTAMP, categorical→VARCHAR.
- **`network/catalog.py`** — answers the introspection SQL QuickSight's JDBC
  driver emits: `information_schema.tables/columns/schemata`,
  `pg_class/pg_namespace/pg_attribute`. Unrecognized probes return an empty
  set (drivers tolerate) with a WARN.

## Wired into the server

`network/server.py` now routes:
- `CATALOG` queries → `CatalogResponder`;
- `DATA` queries → `compiler.execute_request` (real path: MetricFlow plans +
  executes + filters) then **re-projects** MetricFlow's (dims, metrics) output
  to the client's SELECT order;
- dry-run stays on the mock-executor path for wire testing.

## Tests (46 total green)

- `test_ws2_filters.py` — 16: unit translation, injection-escaping,
  deny-by-default, golden filters through DuckDB.
- `test_ws3_catalog.py` — 12: cube derivation, pre-scoping, deterministic
  OIDs, introspection answers.
- `test_ws3_quicksight_e2e.py` — 4: full stack over a socket — discover cubes,
  discover columns, slice semi-additive metric, filtered slice.

## Deferred (documented)

- **Power BI / Npgsql** `pg_type` composite bootstrap — stub returns empty +
  logs WARN; to be filled from a captured corpus when Power BI is qualified.
- Role-based catalog visibility filtering — hook present
  (`CatalogResponder._visible_tables`), wired in WS4 (auth/ACLs).
