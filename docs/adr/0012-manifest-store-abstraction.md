# ADR-0012: Manifest store abstraction and deployment bundle

Date: 2026-07-04 · Status: **Accepted**

## Context

Today `FLOWPROXY_MANIFEST` is a local file path and the Docker entrypoint runs
`dbt parse` on boot. This conflates two separable concerns:

- **Producing** the semantic manifest — needs the full dbt project + warehouse
  adapter creds; a *build-time* activity.
- **Consuming** it — needs only the validated JSON; a *runtime* activity.

Coupling them is wrong for an air-gapped bank: the runtime proxy would need
warehouse-DDL creds and the dbt toolchain merely to serve queries, a bad YAML
edit would surface at production boot rather than in CI, and a boot-time
re-parse could drift from what CI validated. Enterprises publish build
artifacts to an artifact store (S3 / GCS / Azure Blob / Artifactory / MinIO) —
FlowProxy must consume from there, without assuming any one vendor.

## Decision

1. **Producer/consumer split.** CI produces + validates + publishes a bundle;
   the proxy consumes it at runtime. The proxy never runs `dbt parse` in
   production and never needs warehouse-DDL privileges — only the read creds to
   *execute* queries (ADR-0013).

2. **Deployment bundle** (compressed tar), published per git SHA / content
   checksum:
   ```
   flowproxy-bundle-<gitsha>.tar.zst
   ├── semantic_manifest.json     # the MetricFlow semantic graph (consumed)
   ├── manifest.json              # full dbt manifest (lineage/debug; optional)
   ├── metadata.json              # git sha, dbt version, build time, sha256, validation report
   └── profiles.template.yml      # adapter SHAPE only — secrets injected at runtime
   ```
   The bundle is **pure metadata**: warehouse credentials never travel in it, so
   it can live in a lower-privilege store. `metadata.json` makes every deployed
   artifact self-describing and auditable (which YAML version produced which
   answers — the regulator question, ADR-0006).

3. **`ManifestStore` abstraction** — one interface, pluggable backends. The
   default object-store backend uses **`fsspec`**, so `s3://`, `gs://`, `az://`,
   `file://`, `http(s)://` (Artifactory/Nexus) are the same code path selected
   by URL scheme in config, not by a rebuild:
   ```
   ManifestStore (abstract): resolve_latest() -> Bundle, fetch(version) -> Bundle
   ├── FsspecStore     # s3/gs/az/file/http via fsspec — covers every cloud + local
   └── (future)        # a bespoke backend only if a store needs auth fsspec can't do
   ```
   `FLOWPROXY_MANIFEST` (local path) becomes the degenerate `file://` case —
   backward compatible.

4. **Config:**
   ```
   FLOWPROXY_MANIFEST_URI = s3://bank-artifacts/flowproxy/production/  # dir or bundle
   FLOWPROXY_MANIFEST_POLL_INTERVAL = 300   # seconds; 0 disables polling
   ```
   Storage backend credentials (read-only to the artifact store) come from the
   environment / instance role, same as any fsspec client.

## Consequences

- Adds `fsspec` (+ the extras the deployment needs: `s3fs` / `gcsfs` / etc.,
  vendored through the internal mirror) as a runtime dependency.
- The proxy's boot path branches: `file://` may point at a raw
  `semantic_manifest.json` (dev/today) OR a bundle; object/HTTP URIs always
  point at a bundle. A resolver normalizes both to an in-memory manifest.
- Bundle integrity is checked on fetch (sha256 in `metadata.json` vs bytes);
  a mismatch refuses to load — supply-chain hardening for the air gap.
- Hot-swap (ADR-0014) consumes `ManifestStore`; poll + signal both call
  `resolve_latest()`.

## Alternatives considered

- **Bake the manifest into the image**: immutable but forces a rebuild+redeploy
  for every YAML change; loses the fast CI-deploy loop analysts expect.
- **Boot-time `dbt parse` in production** (today's Docker path): keeps
  warehouse-DDL creds + the dbt toolchain in the runtime blast radius, and
  validates too late. Retained only for the local dev/demo entrypoint.
- **Per-vendor SDKs (boto3/google-cloud-storage)**: N backends to build and
  accredit; `fsspec` collapses them to one.
