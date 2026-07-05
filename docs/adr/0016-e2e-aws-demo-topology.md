# ADR-0016: End-to-end AWS demo topology (DuckDB, EC2, QuickSight)

Date: 2026-07-04 · Status: **Accepted** · Scope: a working end-to-end demo,
not a production reference. Production shape is documented alongside each
decision as the migration target.

## Context

The pieces (CI gate, bundle store, `from_bundle` runtime, QuickSight wire path)
are each proven in isolation. This ADR wires them into one live demo on AWS:
CI publishes a bundle to S3, a FlowProxy runs on AWS, pulls the latest bundle,
and Amazon QuickSight connects and slices the semantic layer.

Three architectural facts force the decisions below:

1. **DuckDB is a local file, not a networked warehouse.** There is no
   "connect to DuckDB with credentials over the network." The `.duckdb` file
   must physically exist on the proxy host's disk at runtime.
2. **QuickSight needs a raw TCP path to port 5432.** The PostgreSQL wire
   protocol is not HTTP, so HTTP-only PaaS (App Runner, Lightsail containers)
   cannot host it.
3. **Auth is cleartext-password today** (SCRAM/ACLs are WS4, unbuilt).

## Decisions

### 1. DuckDB delivery: S3 sidecar object (not in the bundle)

CI builds `finance_demo` into `finance_demo.duckdb` and uploads it to S3 as a
**separate object**, next to the bundle. The bundle stays metadata-only
(ADR-0012 stays intact - no data in the bundle). The proxy host downloads the
`.duckdb` to local disk on boot and points the DuckDB adapter at that path via
`FLOWPROXY_DUCKDB_PATH`.

- Keeps the bundle pure; the `.duckdb` is demo warehouse *data*, not semantic
  metadata, and belongs on a different lifecycle.
- Mirrors how a real (networked) warehouse is referenced: the bundle's
  `profiles.template.yml` names a location; runtime resolves it.
- **Production migration:** swap the DuckDB profile for a Snowflake/Redshift
  profile in `profiles.template.yml`; the sidecar download step disappears
  entirely (a real warehouse is already networked). Nothing else changes.

### 2. Compute: single EC2 instance for the demo

One EC2 instance runs the container via `docker run`, with an **instance
profile IAM role** granting read on the S3 bucket (bundle + `.duckdb`). A
security group opens TCP 5432 to QuickSight's published IP ranges (and 22 to the
operator). No long-lived AWS keys anywhere.

- Fastest path to a genuinely working demo; QuickSight connects to the
  instance's public IP (or an Elastic IP).
- **Production migration (documented, not built):** ECS Fargate task behind a
  Network Load Balancer exposing 5432, task role for S3, service auto-scaling,
  health check via the container's existing `pg_isready` probe. The image and
  entrypoint are identical; only the placement changes.

### 3. Access: public endpoint + password for the demo

The proxy is reachable on a public IP; `FLOWPROXY_PASSWORD` is set; QuickSight
uses a standard PostgreSQL data source with SSL disabled at the app (terminate
TLS at the LB in the production shape). Auth is cleartext-password - acceptable
for a time-boxed demo with a throwaway credential and a locked-down security
group, explicitly **not** production-grade.

- **Production migration (documented, not built):** QuickSight **VPC
  connection** to a private NLB (no public exposure), TLS terminated at the NLB,
  and SCRAM + per-role metric ACLs from WS4. The security-group-to-IP-range
  approach becomes a private ENI.

### 4. DuckDB catalog-name invariant (found in the dress rehearsal)

DuckDB derives its **catalog (database) name from the file's basename**.
MetricFlow's compiled SQL references that catalog by name (`finance_demo.main.
fct_...`). Therefore the sidecar file, wherever it is downloaded to at runtime,
**must keep the basename it had when the manifest was built** - `finance_demo.
duckdb`. Rename it to `warehouse.duckdb` and every query fails with
`Catalog "finance_demo" does not exist`. The entrypoint derives the local
basename from the sidecar URI to preserve this. (With a real warehouse this
does not arise - the catalog/database is a connection property, not a filename.)

## Consequences

- CI gains AWS credentials (via GitHub OIDC to an IAM role) and two upload
  steps: the bundle and the `.duckdb` sidecar. Publishing still happens **only
  on release** (ADR-0013 unchanged).
- The container entrypoint gains a boot step: download the `.duckdb` sidecar to
  local disk before starting the proxy in STORE mode.
- A demo has real, if small, AWS cost (one t3.small-ish instance, S3 storage,
  data transfer). The runbook includes teardown.
- The demo proves the full loop; the three "production migration" notes above
  are the honest delta between demo and prod, each a config change rather than a
  re-architecture.

## Alternatives considered

- **DuckDB file inside the bundle:** simplest, but violates "bundle carries no
  data" and bloats every hot-swap with the full dataset. Rejected.
- **`dbt build` in the container on boot:** re-introduces the boot-time dbt run
  WS7 deliberately removed, and needs the project + warehouse-DDL in the runtime
  image. Rejected.
- **App Runner / Lightsail:** HTTP-only; cannot serve the raw PostgreSQL wire
  protocol. Dead end, recorded so nobody tries it.
