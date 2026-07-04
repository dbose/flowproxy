# ADR-0010: Full semantic-layer exposure as the default analyst catalog

Date: 2026-07-04 · Status: **Accepted** · Supersedes the *exposure policy* of
ADR-0004 (its catalog mechanics — pg_catalog emulation, deterministic OIDs,
manifest-driven answers — stand unchanged).

## Context

ADR-0004 proposed saved-queries-only in production, auto-cubes behind a flag.
The product owner rejected this (2026-07-04): analysts must get the UX of
dbt Cloud SL integrations and of Lightdash-on-a-dbt-project — the **entire
semantic graph explorable directly from the BI tool**, no pre-curation step
between a merged `sem_*.yml` and an analyst seeing the new field.

## Decision

Default exposure, all on, no flags:

1. **One auto-cube per semantic model** — `semantic_layer.<model_name>`:
   the metrics anchored to that model's measures + every dimension
   **reachable via MetricFlow's linkable-elements resolution** (entity join
   walk), not just local dimensions. This is Lightdash's explore-per-model
   shape and guarantees every column combination within one cube is valid.
2. **One wide table** — `semantic_layer.all_metrics`: every metric + the
   union of dimensions. This is dbt Cloud's single-semantic-datasource shape
   (Tableau/Sheets-style pick-and-drag). Invalid metric×dimension picks are
   *inherent* to this shape and fail at query time exactly as dbt Cloud does:
   a clean ErrorResponse naming the unreachable dimension **and listing valid
   alternatives** (reusing the `get_dimensions` resolution from ADR-0008).
3. **Saved-query tables remain** (`semantic_layer.<saved_query>`) as governed,
   always-valid shortcuts — recommended for certified dashboards, no longer
   the only door.
4. **YAML descriptions/labels are published** through `pg_description` /
   `information_schema` remarks so BI tools render metric tooltips —
   the discoverability half of the Lightdash UX.
5. **Governance shifts from curation to authorization**: the catalog is
   identity-aware — metric ACLs (ADR-0006) filter what each role *sees*, not
   just what it may run. Catalog answers are therefore keyed by
   `(manifest_sha, role)`.

## Consequences

- Reachable-dimension resolution moves forward in the schedule: it was a WS6
  (`get_dimensions`) concern; auto-cubes need it in WS3. It becomes a shared
  compiler API (`valid_group_bys(metrics) -> [dimension]`) consumed by the
  catalog, the MCP tool, and error hints — one implementation, three users.
- The CI validation gate (WS7) cannot dry-run the combinatorial wide table;
  it validates each auto-cube's metric set individually plus every saved
  query fully. The wide table's residual failure mode is the guarded
  query-time error, which is the accepted dbt Cloud UX.
- `all_metrics` may reach hundreds of columns; column ordering is specified
  (dimensions grouped by entity, then metrics alphabetically) so the picker
  stays navigable. QuickSight and Power BI both tolerate wide tables.
- Per-role catalogs interact with connection pooling: role is bound at
  session auth, so pooled BI connections under one service login see one
  catalog — document that per-analyst visibility requires per-analyst logins.

## Alternatives considered

- **Saved-queries-only (ADR-0004 as written)**: safest surface, but inserts
  a curation gate between YAML merge and analyst — rejected by the PO as
  breaking dbt-Cloud/Lightdash UX parity.
- **Auto-cube per metric**: hundreds of near-duplicate tables; unusable picker.
