# ADR-0009: Rust wire-protocol edge deferred to Phase 2

Date: 2026-07-04 · Status: **Accepted**

## Context

Rust was proposed for the core server layers on performance and
memory-safety grounds. Per-query cost decomposes as: wire framing (µs) +
sqlglot parse (ms) + MetricFlow planning (100ms–s, Python, irreplaceable) +
warehouse execution (dominant). A Rust rewrite does not touch either dominant
term, and the plan cache (ADR-0007) removes planning from the steady-state
path anyway. The strongest Rust argument is memory safety at the untrusted
network edge — real, but addressable in Phase 1 by other means.

## Decision

1. **Phase 1 ships pure Python** (asyncio + uvloop). Correctness and
   governance risk live in semantics, catalog, and auth — not in socket
   throughput.
2. The eventual architecture, if triggered, is a **hybrid**: a Rust edge
   (tokio + `pgwire`) owning TLS/SCRAM/framing/catalog/caching/limits, with
   the Python MetricFlow planner as a stateless sidecar behind gRPC/UDS.
   Phase 1 module boundaries (protocol ↔ session ↔ pipeline ↔ planner) are
   kept clean specifically so this split is a re-homing, not a rewrite.
3. **Re-evaluation triggers** (measured, not vibes): sustained concurrent
   connections > ~2k per node; p99 handshake latency SLO misses; a security
   review formally requiring memory-safe parsing at the perimeter; or fuzzing
   uncovering parser fragility that bounded-length checks don't close.
4. Phase 1 mitigations at the edge: hard bounds on all length fields
   (exists), per-IP connection caps, statement timeouts, a fuzz-test suite
   over the frame parser in CI, and non-root, read-only-FS containers.

## Consequences

- One language, one toolchain to accredit in the air-gapped supply chain for
  Phase 1 — a material reduction in security-review surface.
- We accept the GIL ceiling on planning throughput, mitigated by the worker
  pool + cache; horizontal scale-out (stateless proxy behind the NLB) is the
  documented growth path before any rewrite.

## Alternatives considered

- **Rust-first now**: rewrites the part that is neither the bottleneck nor
  the risk while the semantics are still stabilizing. Classic premature
  optimization; rejected.
- **Go edge**: same analysis as Rust with a weaker memory-safety story at
  the parser; if we split, we split to Rust.
