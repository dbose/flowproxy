# WS7 Spike — adapter-from-bundle bootstrap (findings)

**Question:** can FlowProxy build a working MetricFlow execution engine from a
bundled `semantic_manifest.json` + an injected profile, with **no checked-out
dbt project** (models/seeds/SQL)? This was the highest-risk unknown in WS7.

**Answer: YES — proven.** The spike built an engine from a bundle + a synthetic
4-file skeleton and executed a golden query returning exact numbers
(EMEA 1500/1200/2000, AMER 4000/4500/3000 — semi-additive guardrail intact).

## How it works (the runtime consumer recipe)

Two independent pieces, neither needing the real project:

1. **The semantic manifest** parses straight from the bundled JSON bytes via
   `parse_manifest_from_dbt_generated_manifest(json_str)` — no project dir.
   (This applies `transform_dbt_generated_manifest`; see hardening note below.)

2. **The warehouse adapter** is built from a **minimal synthetic project
   skeleton** composed at runtime from the bundle's `profiles.template.yml` +
   injected creds:
   ```
   <runtime_skeleton>/
   ├── dbt_project.yml     # 8 lines: name, profile, model-paths, telemetry off
   ├── profiles.yml        # profiles.template.yml + injected warehouse creds
   ├── models/             # empty
   └── target/semantic_manifest.json   # the bundled manifest
   ```
   Then: `dbtProjectMetadata.load_from_paths(skeleton, skeleton)` →
   `get_adapter_by_type(profile.credentials.type)` →
   `AdapterBackedSqlClient(adapter)` → `MetricFlowEngine(lookup, sql_client)`.

The skeleton exists only to satisfy dbt's config loading + adapter
registration; it carries **no models, seeds, or SQL** — the semantic graph
comes entirely from the bundled manifest.

## Critical side effect to respect

`dbtProjectMetadata.load_from_paths` internally runs `dbt debug`, which
**mutates global dbt library state** (adapter registration). Implications for
WS7c hot-swap:
- adapter bootstrap is **not** freely re-entrant across arbitrary threads;
- the blue/green swap must re-run this carefully (rebuild the engine, don't
  assume clean global state), and serialize adapter (re)registration.
This is the main thing to design around in the hot-swap, and why building the
new `LiveSemanticLayer` in a controlled background step (not N concurrent
rebuilds) matters.

## Hardening item found (not a bug today)

Our compiler's `_load_manifest` uses plain `PydanticSemanticManifest.parse_obj`;
the MetricFlow engine path uses `parse_manifest_from_dbt_generated_manifest`
(parse + `transform_dbt_generated_manifest`). Compared side-by-side on the
fixture, the entity/dimension/primary_entity structure our **registry** reads is
**identical** — so the registry is correct today. But the two code paths should
converge on the single canonical parse to avoid future drift. WS7 will switch
`_load_manifest` to `parse_manifest_from_dbt_generated_manifest` and feed the
same object to both the registry and the engine.

## Consequences for the WS7 build

- **WS7a/WS7d confirmed feasible**: the consumer needs only the bundle + creds;
  the skeleton is a small runtime-composed helper, not a checked-out project.
- **Production Dockerfile can drop dbt-project mounting** for the runtime image:
  it composes the skeleton from the bundle instead. (dbt-core is still installed
  because the adapter machinery needs it; but no project checkout.)
- **WS7c hot-swap** must treat adapter (re)registration as global-state-mutating
  and serialize it.

Spike artifacts were temporary and have been cleaned up; this recipe becomes
`engine/compiler.py`'s `from_bundle(...)` constructor in WS7d.
