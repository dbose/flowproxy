# WS7 Plan — Manifest integration, CI deploy gate, runtime hot-swap

How FlowProxy *gets* the semantic layer: the producer/consumer split between CI
and runtime, mediated by a versioned artifact bundle in a pluggable store.

Backed by [ADR-0012](adr/0012-manifest-store-abstraction.md) …
[ADR-0015](adr/0015-runtime-warehouse-credentials.md).

## The flow

```
  git (sem_*.yml)
        │
        ▼   CI build environment (full dbt project + adapter-less MetricFlow)
  ┌─────────────────────────────────────────────────────────┐
  │  flowproxy validate  (WS7a, offline plan-only gate)      │
  │   1. dbt parse → semantic_manifest.json                  │
  │   2. plan every saved query + auto-cube (explain only)   │  ← blocks deploy on break
  │   3. breaking-change diff vs deployed (advisory)         │
  │   4. build bundle + metadata.json (sha, report)          │
  │   5. publish → ManifestStore, bump `production` pointer   │
  └───────────────────────────┬─────────────────────────────┘
                              │  bundle (no secrets)
                              ▼
             ManifestStore  (fsspec: s3:// gs:// az:// file:// https://)
                              │
                              ▼   runtime (SELECT-only warehouse creds from secret store)
  ┌─────────────────────────────────────────────────────────┐
  │  FlowProxy proxy  (WS7b, runtime consumer)               │
  │   • boot: resolve_latest() → build LiveSemanticLayer     │
  │   • poll every N s  +  POST /admin/reload / SIGHUP       │
  │   • atomic blue/green swap; old serves until flip        │
  └─────────────────────────────────────────────────────────┘
```

## Decisions locked (PO, 2026-07-04)

| Fork | Decision |
|---|---|
| Bundle scope | **Full bundle** — manifest + `metadata.json` (git sha, checksum, validation report) + `profiles.template.yml`. Self-describing + auditable. |
| Refresh model | **Both** — interval poll (default) + admin endpoint/SIGHUP to force-now. |
| Warehouse creds | **Env / mounted `profiles.yml`** from the secret store; never in the bundle. `SecretProvider` abstraction deferred. |
| CI validation | **Offline, plan-only** — parse + `explain` every governed query; no warehouse needed. Online smoke = future opt-in. |

## Workstreams

| WS | Deliverable | ADR |
|---|---|---|
| WS7a | `engine/manifest_store.py` — `ManifestStore` + `FsspecStore` + `Bundle`/`metadata` model + integrity check | 0012 |
| WS7b | `flowproxy/cli.py validate` — offline plan-only gate (saved queries + auto-cubes) + breaking-change diff + bundle publish + validation report | 0013 |
| WS7c | Runtime refresh — `LiveSemanticLayer` (compiler+registry+catalog+MCP) behind an atomic ref; poll loop; `POST /admin/reload` + SIGHUP; blue/green swap | 0014 |
| WS7d | Runtime profile composition from `profiles.template.yml` + injected env; least-privilege SELECT creds; boot + hot-swap paths | 0015 |
| WS7e | Example CI pipelines (GitHub Actions + GitLab CI) invoking `flowproxy validate`; Dockerfile split (build-stage parse vs runtime-consume) | 0012,0013 |

Sequencing: **WS7a** (store) → **WS7b** (gate, uses store to diff) →
**WS7c** (hot-swap, uses store) ∥ **WS7d** (creds) → **WS7e** (glue).

## Config surface (new)

| Variable | Default | Meaning |
|---|---|---|
| `FLOWPROXY_MANIFEST_URI` | — | `s3://…` / `gs://…` / `file://…` / `https://…` bundle or dir. Supersedes `FLOWPROXY_MANIFEST` (which stays as `file://` shorthand). |
| `FLOWPROXY_MANIFEST_POLL_INTERVAL` | `300` | Seconds between store polls; `0` disables. |
| `FLOWPROXY_ADMIN_PORT` | `8181` | Admin/MCP port for `POST /admin/reload` (never 5432). |
| `FLOWPROXY_ADMIN_TOKEN` | — | Bearer token gating the reload endpoint. |
| (warehouse) | — | Adapter creds via env / mounted `profiles.yml` (ADR-0015). |

## Backward compatibility

`FLOWPROXY_MANIFEST=/path/to/semantic_manifest.json` keeps working: the
resolver treats a bare-JSON `file://` path as a degenerate single-file bundle
(no metadata, no integrity check, dev-only). The demo/local Docker entrypoint
retains boot-time `dbt parse`; production images use the consume-only path.

## Out of scope (recorded)

- Online (execute-a-smoke-query) CI stage — future opt-in in a staging pipeline.
- `SecretProvider` abstraction (Vault/cloud SM) — env/mounted-file covers Phase 1.
- Rollback UX beyond "point `production` at a previous bundle" (store-native).
