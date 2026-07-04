# ADR-0011: Auto-cube exploration as the QuickSight analyst experience

Date: 2026-07-04 · Status: **Accepted** · Refines
[ADR-0010](0010-full-semantic-exposure-catalog.md) with the exploration UX and
the pre-scoping rule.

## Context

The north star: an analyst opens QuickSight, sees the dbt-core semantic models
(defined in `sem_*.yml`) as explorable cubes, and slices/dices them with native
drag-and-drop — while MetricFlow guardrails (semi-additivity, valid join paths,
metric ACLs) are enforced automatically.

Research into how established dbt SL integrations expose exploration (2026):

- **dbt Cloud JDBC** (Tableau, Sigma, Hex): drag-and-drop becomes a
  `semantic_layer.query(metrics=[...], group_by=[...])` table-valued call over
  an **Arrow Flight SQL** driver; MetricFlow plans joins/aggregations.
- **Lightdash**: reads the dbt project, renders an **explore-per-model** UI,
  and generates optimized SQL behind the scenes — no client driver.

QuickSight has **neither** a dbt SL driver nor a way for an analyst to hand-type
a table-valued call in its UI. It speaks stock PostgreSQL and, from its
drag-and-drop canvas, emits `SELECT <cols> FROM <table> GROUP BY ...`. Therefore
the dbt-Cloud TVF pattern is architecturally unavailable for native QuickSight
exploration; the auto-cube-as-table pattern is the only one that yields real
drag-and-drop — and it is exactly Lightdash's proven explore-per-model shape.

## Decision

1. **Each dbt semantic model is published as an explorable cube** — a virtual
   table `semantic_layer.<model>` whose columns are that model's metrics plus
   **every dimension reachable for those metrics** via MetricFlow's
   linkable-elements resolution (`compiler.valid_group_bys`). The analyst drags
   any subset onto a visual; QuickSight's GROUP-BY SQL is re-planned through
   MetricFlow. Guardrails hold because the proxy never passes BI SQL through —
   it re-derives the query from metric/dimension *names*.

2. **Pre-scoping (per the product decision)**: a cube exposes ONLY fields that
   form valid combinations for that cube's metrics. Because
   `valid_group_bys(metrics)` already returns exactly the reachable dimensions,
   most invalid slices are **impossible to build** in QuickSight — the field
   simply isn't in the cube. This is the primary guardrail against broken
   join-path queries, chosen over relying on runtime errors.

3. **QuickSight ignores client-side aggregation**: when an analyst sets a field
   to SUM/AVG in QuickSight, the emitted aggregate wrapper is unwrapped by the
   parser (already implemented) and the **metric's own aggregation wins**. A
   semi-additive balance stays semi-additive even if the analyst picks "Sum".

4. **`all_metrics` wide table** remains for cross-model, Tableau-"ALL"-style
   exploration. It is the one surface where an invalid metric×dimension pick is
   still possible; it falls back to a guided error (ADR-0010) listing valid
   alternatives. Documented as the "expert" surface; per-model cubes are the
   recommended default.

5. **Saved-query cubes** remain as certified, always-valid shortcuts.

## Consequences

- The catalog is a pure projection of the manifest + `valid_group_bys` — no
  hand config, and it updates atomically on manifest hot-swap.
- Pre-scoping makes each cube's column set metric-dependent, so a cube's
  columns = union of its metrics' reachable dimensions (a metric that reaches
  fewer dimensions still lists the model-level union; runtime guardrail covers
  the rare intra-cube invalid pair, e.g. two metrics with disjoint reach).
- No Arrow Flight SQL, no custom QuickSight driver, no SPICE requirement —
  Direct Query against the PostgreSQL datasource is the whole integration.
- Column ordering within a cube: entity-grouped dimensions, then metrics
  (ADR-0010) — keeps QuickSight's field list navigable.

## Alternatives considered

- **`semantic_layer.query()` TVF for QuickSight analysts**: no UI path in
  QuickSight to author it; would demote analysts to SQL authors. Rejected for
  the native-exploration goal. (May still be offered later for programmatic /
  dbt-SL-compatible tooling — orthogonal to this decision.)
- **Runtime error only, no pre-scoping**: valid but pushes trial-and-error
  onto the analyst; the PO chose pre-scoping as the primary mechanism.
