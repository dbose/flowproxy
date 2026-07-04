# WS7 — Manifest integration, CI gate, hot-swap (complete)

How FlowProxy *gets* the semantic layer. Implements
[ADR-0012](adr/0012-manifest-store-abstraction.md) …
[ADR-0015](adr/0015-runtime-warehouse-credentials.md). Plan:
[ws7-plan.md](ws7-plan.md). Spike: [ws7-spike-findings.md](ws7-spike-findings.md).

## The loop, proven end-to-end

```
git (sem_*.yml) → CI validate (plan-only) → publish bundle → ManifestStore → proxy hot-swap → BI
```

Verified live: CI published a bundle to a store; the proxy booted in STORE mode,
loaded it (`5 cubes exposed`), and served correct semi-additive balances
(AMER 3000, EMEA 2000) over the wire — no dbt project checkout at runtime.

## What shipped

| Piece | Module | Notes |
|---|---|---|
| Bundle + store | `engine/manifest_store.py` | `Bundle` (manifest + metadata + profiles.template), sha256 integrity, `FsspecStore` (s3/gs/az/file/https via fsspec), `open_store()` |
| from_bundle | `engine/compiler.py` | `SemanticCompiler.from_bundle()` composes a synthetic dbt skeleton (profile name derived from the template) + injects creds; **no project checkout** |
| CI gate | `flowproxy_cli/` | `python -m flowproxy_cli validate <dir> [--publish URI]` — plan-only (MetricFlow `explain`, no warehouse), validates saved queries + auto-cubes, breaking-change diff, publishes bundle + validation report |
| Hot-swap | `engine/live_layer.py`, `network/refresh.py` | `LiveSemanticLayer` (compiler+extractor+catalog+responder) behind an atomic ref; `SemanticLayerManager` blue/green swap serialized under a lock (global-state finding); poll loop + SIGHUP + admin-reload |
| Runtime wiring | `main.py` | STORE mode (`FLOWPROXY_MANIFEST_URI`) vs DIRECT mode (back-compat); server reads the current layer per request via `layer_provider` |
| CD | `.github/workflows/semantic-layer.yml` | validate on PR; **publish only on `release: published`** |
| Image | `Dockerfile` | runtime consumes from store — no `dbt parse`, no project mount; telemetry off |

## Decisions honored

- **Full bundle** (manifest + metadata.json + profiles.template.yml) — auditable.
- **Poll + signal** refresh, atomic blue/green swap.
- **Env / mounted profiles** creds — never in the bundle (SELECT-only at runtime).
- **Offline plan-only** gate — no warehouse creds in CI.
- **Publish only on production release** — PRs validate without publishing.

## Correctness properties (tested)

- **Integrity**: a tampered bundle (bytes ≠ recorded sha256) is refused.
- **Blue/green**: a failed build keeps the current version serving — a bad
  publish never takes the proxy down.
- **Immutable snapshots**: an in-flight request's captured layer is unaffected
  by a concurrent swap.
- **Canonical parse**: one `parse_manifest_from_dbt_generated_manifest` feeds
  both registry and engine — no drift (also fixed a latent `explain` result
  attribute bug: `sql_statement.sql`, not `rendered_sql.sql_query`).

## Tests: 77 total green (+23 for WS7)

`test_ws7a_manifest_store.py` (11) · `test_ws7d_from_bundle.py` (3) ·
`test_ws7b_validate_gate.py` (4) · `test_ws7c_hotswap.py` (5).

## Config added

| Variable | Meaning |
|---|---|
| `FLOWPROXY_MANIFEST_URI` | bundle store URI (`s3://…`, `file://…`); enables STORE mode |
| `FLOWPROXY_MANIFEST_POLL_INTERVAL` | poll seconds (default 300; 0 = signal-only) |
| `FLOWPROXY_ADMIN_TOKEN` | bearer gating `POST /admin/reload` (mount pending) |

## Deferred (recorded)

- Mounting `POST /admin/reload` on the MCP/admin HTTP app (handler built in
  `network/refresh.py`; needs the HTTP surface from WS4/MCP-HTTP).
- Online (execute-a-smoke-query) CI stage — opt-in staging pipeline.
- `SecretProvider` abstraction — env/mounted-file covers Phase 1 (ADR-0015).
