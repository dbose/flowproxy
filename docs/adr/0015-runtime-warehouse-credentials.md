# ADR-0015: Runtime warehouse credentials from the environment

Date: 2026-07-04 · Status: **Accepted**

## Context

The deployment bundle carries no secrets (ADR-0012); it ships a
`profiles.template.yml` describing the adapter *shape*. At runtime FlowProxy
still needs read creds to *execute* MetricFlow-planned SQL against the
warehouse (Redshift/Snowflake/… — DuckDB in the demo). Those creds must come
from the enterprise secret store, never from the artifact.

## Decision

1. Warehouse execution creds are supplied via **environment variables** and/or
   a **mounted `profiles.yml`** that the platform's secret store
   (HashiCorp Vault, Kubernetes Secrets, cloud secret manager) populates —
   exactly how dbt itself resolves `profiles.yml` env-var interpolation
   (`{{ env_var('SNOWFLAKE_PASSWORD') }}`). Standard, cloud-agnostic, nothing
   FlowProxy-specific to accredit.

2. The proxy composes the runtime profile from the bundle's
   `profiles.template.yml` + injected env vars at boot / hot-swap; secrets live
   only in process memory and the secret-store mount.

3. **Least privilege:** the runtime role holds SELECT-only warehouse creds. The
   DDL/build creds used by dbt in CI are a different, higher-privilege identity
   that never reaches the runtime blast radius.

4. A pluggable `SecretProvider` abstraction (env / Vault / cloud SM) is
   **deferred** — env + mounted-file covers the standard secret-store
   integration, and adding a provider interface now is premature. The seam
   (profile composition at boot) is isolated so a provider can slot in later
   without touching the query path.

## Consequences

- No secrets in git, in the bundle, or in the artifact store — clean
  supply-chain posture for the bank.
- CI (plan-only, ADR-0013) needs no warehouse creds at all; only pre-prod UAT
  and the runtime proxy do.
- Rotating warehouse creds is a secret-store operation + a proxy restart or
  hot-swap; no rebuild.

## Alternatives considered

- **Creds in the bundle**: forces the artifact store to be secret-grade and
  couples credential rotation to manifest publishing. Rejected.
- **SecretProvider abstraction now**: more surface than Phase 1 needs; the
  env/mounted-file path is the industry-standard baseline and is not a
  throwaway.
