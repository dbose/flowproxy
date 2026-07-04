# ADR-0013: CI deploy gate — offline plan-only validation

Date: 2026-07-04 · Status: **Accepted** · Depends on
[ADR-0012](0012-manifest-store-abstraction.md)

## Context

The value of a deploy gate is catching a broken `sem_*.yml` *before* it reaches
production — a renamed dimension a saved query depends on, a deleted metric, an
unreachable join path. This mirrors `lightdash deploy` from CI. The gate runs
in the build environment (the one place with the full dbt project); we want it
to run **anywhere CI runs**, including without production warehouse credentials.

## Decision

CI runs a **plan-only, offline** gate — no warehouse connection required:

1. **`dbt parse`** → `semantic_manifest.json` (dbt already validates the
   MetricFlow YAML schema here).
2. **Plan every governed query** through MetricFlow's `explain` (compile SQL,
   do **not** execute):
   - every **saved query** in the manifest;
   - every **auto-cube**: for each cube, resolve `valid_group_bys(metrics)` and
     plan a representative query over its metrics + reachable dimensions.
   A planning failure = broken join path / renamed dimension / missing metric →
   **gate fails**, deploy blocked. FlowProxy already has `explain`-based
   `compile_request` and `valid_group_bys` to do exactly this.
3. **Breaking-change diff (advisory):** compare against the currently-deployed
   manifest (fetched via `ManifestStore`); flag removed metrics/dimensions that
   existing saved queries reference. Warn or fail per policy.
4. **Publish** the bundle (ADR-0012) to the artifact store under `<gitsha>` and
   update the `production`/`latest` pointer.
5. Emit a **validation report** into `metadata.json`: what was planned, pass/
   fail per object, dbt + MetricFlow versions, timing.

Offline planning uses `FLOWPROXY_COMPILER_MODE` against a DuckDB or
adapter-less MetricFlow setup — enough to render SQL. It deliberately does NOT
catch dialect/runtime warehouse errors (wrong function name on Redshift, a
permission error); those are covered by pre-prod UAT, not the CI gate.

## Consequences

- The gate is a CLI (`python -m flowproxy.cli validate <project_dir>`), usable
  in any CI system (GitHub Actions, GitLab CI, Jenkins) and runnable locally.
- Fast and hermetic: no prod creds in CI, no warehouse round-trips, so it can
  run on every PR.
- The same plan-only pass **doubles as a plan-cache warmer** input for the
  runtime (ADR-0007): the set of validated queries is exactly what to pre-plan.
- Residual risk (dialect/runtime errors) is documented and pushed to UAT — an
  explicit, logged boundary, not a silent gap.

## Alternatives considered

- **Online validation (execute a smoke query per cube)**: higher fidelity,
  catches dialect issues — but needs a warehouse connection in CI, which for a
  bank means prod-adjacent creds in the build system. Rejected for Phase 1;
  may be added as an opt-in extra gate stage in a staging pipeline.
- **No gate (publish on green build)**: reintroduces the "discover the break at
  production boot" failure this workstream exists to remove.
