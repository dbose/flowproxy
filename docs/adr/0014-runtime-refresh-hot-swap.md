# ADR-0014: Runtime manifest refresh — poll + signal, atomic hot-swap

Date: 2026-07-04 · Status: **Accepted** · Depends on
[ADR-0012](0012-manifest-store-abstraction.md)

## Context

When CI publishes a new bundle (ADR-0013), a running proxy must pick it up
without dropping analyst connections and without ever serving a half-built
catalog. Warehouse execution creds are a separate concern (ADR-0015).

## Decision

1. **Refresh triggers — both:**
   - **Poll** `FLOWPROXY_MANIFEST_URI` every `FLOWPROXY_MANIFEST_POLL_INTERVAL`
     seconds (default 300; 0 disables). Hands-off, works air-gapped, no
     inbound reachability needed.
   - **Signal-now:** an authenticated admin endpoint
     (`POST /admin/reload`, on the MCP/admin port — never 5432) and `SIGHUP`,
     so CI can force an immediate swap right after publishing.

2. **Atomic blue/green swap.** Refresh builds a *new* `SemanticCompiler` +
   `SemanticRegistry` + `VirtualCatalog` **in the background**; only after it
   loads and self-validates does a single reference flip to the new set. The
   old manifest keeps serving until the instant of the flip. A failed build
   logs, alerts, and **leaves the current version running** — a bad publish
   never takes the proxy down.

3. **Versioned, integrity-checked.** Each active manifest carries its git SHA +
   sha256 (from `metadata.json`). The swap is skipped if the resolved version
   equals the running one (idempotent polls). The SHA is stamped into every
   audit record (ADR-0006), so answers are traceable to the exact YAML.

4. **In-flight queries** finish against the manifest they started on (the
   reference is captured per request), so a swap never corrupts a running
   query's plan.

## Consequences

- The server holds a single `atomic reference` to a `LiveSemanticLayer`
  (compiler + registry + catalog + MCP tool bindings) rebound on swap; all
  request handlers read it once at request start.
- Poll + signal share one `refresh()` codepath (resolve → build → validate →
  flip), so there is one place to test and audit.
- The catalog OIDs are deterministic per manifest (ADR-0004), so a swap changes
  OIDs only when the manifest actually changes — reconnecting BI clients see a
  coherent catalog for a given deployed version.
- Memory: two manifests briefly coexist during a swap; bounded and short.

## Alternatives considered

- **Restart-to-reload**: simplest, but drops every analyst connection on each
  YAML change — unacceptable for an interactive BI fleet.
- **Poll-only** or **signal-only**: poll needs no inbound path (good for air
  gap) but adds latency to a deploy; signal is instant but needs CI→proxy
  reachability. Offering both removes the tradeoff.
