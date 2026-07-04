# ADR-0007: Plan caching keyed by manifest hash

Date: 2026-07-04 · Status: **Accepted**

## Context

MetricFlow planning is synchronous, CPU-bound Python costing ~100ms–seconds.
Dashboards re-issue identical queries constantly (auto-refresh, multiple
viewers). Planning is a pure function of
`(semantic manifest, canonical query request)` — ideal cache shape.

## Decision

1. **Plan cache (Phase 1, always on)**: LRU mapping
   `(manifest_sha256, canonical_request)` → compiled warehouse SQL, where
   `canonical_request` = sorted metrics + sorted group-bys + normalized
   filters/order/limit. Configurable size (default 1024 entries); hit/miss
   counters exposed in logs. Manifest hot-swap changes the key prefix, so
   invalidation is implicit and atomic — stale plans can never serve a new
   manifest.
2. **Result cache (Phase 1, flag-gated, default OFF)**: TTL cache over
   executed results for saved-query-shaped requests only. Off by default
   because result staleness policy is a business decision per deployment;
   the enforced-row-filter set (ADR-0006) is part of the key so users can
   never share cached rows across authorization scopes.
3. Planning moves off the event loop into a bounded worker pool; the cache
   sits in front of the pool (single-flight per key, so N concurrent
   identical dashboard tiles trigger one plan).

## Consequences

- After warm-up, steady-state latency ≈ warehouse execution time; the Python
  planner leaves the hot path — this materially defers the Rust question
  (ADR-0009).
- Pre-warming: on manifest deploy, the CI gate's saved-query dry-run doubles
  as a plan-cache warmer.

## Alternatives considered

- **External cache (Redis)**: another stateful service to accredit in an
  air-gapped estate; in-process LRU suffices at Phase 1 scale.
- **Pre-aggregation materialization (Cube.dev style)**: high value, large
  scope; explicitly Phase 3 — plan caching is the 80/20.
