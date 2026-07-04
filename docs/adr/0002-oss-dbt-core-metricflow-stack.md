# ADR-0002: OSS dbt-core + MetricFlow stack (no dbt Cloud, no Fusion)

Date: 2026-07-04 · Status: **Accepted**

## Context

The semantic definitions live in `sem_*.yml` files checked into an internal
git repo, parsed by dbt into `target/semantic_manifest.json`. dbt Labs offers
three engines: dbt-core (Python, Apache-2.0), the Fusion engine (Rust,
ELv2 source-available, Semantic Layer features gated to the dbt platform),
and dbt Cloud (hosted — unusable in an air-gapped bank).

## Decision

1. **dbt-core 1.11.x** is the parser/orchestrator. **metricflow** is the query
   planner, **dbt-semantic-interfaces** the manifest schema.
2. The four packages (`dbt-core`, `dbt-semantic-interfaces`, `metricflow`,
   `dbt-metricflow`) are pinned in lockstep; the compiler **asserts the
   compatibility matrix at boot** and refuses to start on drift, rather than
   failing obscurely at plan time.
3. dbt Cloud SL API docs (JDBC `semantic_layer.query()` syntax, metadata
   surface) are treated as a **behavioral spec to emulate**, never as
   endpoints to call.
4. Fusion is out of scope: its SL capabilities are platform-gated and ELv2
   licensing needs separate legal review; we re-evaluate if/when its semantic
   planner becomes usable standalone.
5. dbt anonymous telemetry is force-disabled (`DO_NOT_TRACK=1`,
   `flags.send_anonymous_usage_stats: false`) — mandatory for air-gap.

## Consequences

- **Licensing**: dbt-core / dbt-semantic-interfaces are Apache-2.0;
  **MetricFlow is BUSL-1.1**. Internal self-hosted use is within the license's
  intent (the restriction targets offering a competing hosted service), but
  legal sign-off is a Phase 1 exit criterion, not an afterthought.
- Upgrades are atomic across the four packages + a `dbt parse` re-run;
  the CI deploy gate (Phase 1 plan, WS7) enforces this.
- MetricFlow guardrails defined in YAML (`non_additive_dimension`, join
  paths, metric filters) are authoritative: the proxy never bypasses the
  planner with raw SQL passthrough. The YAML in git *is* the guardrail.

## Alternatives considered

- **dbt Cloud SL APIs**: hosted-only; excluded by the air gap.
- **Fusion engine**: see decision (4).
- **Cube.dev**: separate modeling language; the mandate is dbt-native YAML.
