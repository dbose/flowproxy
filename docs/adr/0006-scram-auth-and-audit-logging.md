# ADR-0006: SCRAM-SHA-256 authentication and audit logging

Date: 2026-07-04 · Status: **Accepted**

## Context

Trust/cleartext auth will not pass a banking security review. In-protocol TLS
is declined by design (ADR-0001), so credentials must be safe on the network
leg regardless. Regulators additionally ask "who queried which metric, when,
under which definitions" — dbt Cloud provides this natively; we must replicate.

## Decision

1. Wire auth is **SCRAM-SHA-256** (PostgreSQL AuthenticationSASL flow, RFC
   5802/7677), implemented with the `scramp` library (pure-Python, small,
   the same one pg8000 uses — favorable for air-gap vetting). Cleartext auth
   is removed; trust mode survives only behind an explicit
   `FLOWPROXY_INSECURE_DEV=1` flag that logs a startup WARNING.
2. Phase 1 user store: a git/ops-managed **verifier file** (SCRAM verifiers,
   i.e. salted+iterated — no plaintext or reversible secrets at rest).
   LDAP/AD binding is Phase 2; the store is behind an interface so the swap
   is additive.
3. AuthZ config (YAML, checked into git alongside the ops config):
   - **metric ACLs**: role → allowed metrics/saved queries (deny-by-default
     optional per deployment);
   - **enforced row filters**: role → MetricFlow `where` filters appended to
     every query (e.g. cost-center scoping), merged via ADR-0005's path.
4. **Audit log**: append-only JSONL, one record per query attempt:
   `{ts, session, user, role, client_addr, raw_sql_sha256, metrics,
   dimensions, filters, manifest_sha, outcome, rows, latency_ms}`.
   Row *data* is never logged. The manifest SHA pins every answer to the
   exact YAML version that produced it — the lineage a regulator asks for.

## Consequences

- Adds `scramp` to requirements; the L1/L4 suites gain SCRAM handshake tests
  (correct + wrong password + downgrade-attempt).
- Channel binding (`SCRAM-SHA-256-PLUS`) is not offered since TLS terminates
  upstream; documented as a known property of the deployment model.
- MCP-originated queries (ADR-0008) flow through the same ACL + audit path.

## Alternatives considered

- **MD5 auth**: deprecated, disallowed by modern policy baselines.
- **Kerberos/GSSAPI now**: highest enterprise fit but heavy; deliberately
  Phase 2 behind the same user-store interface.
