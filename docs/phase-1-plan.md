# Phase 1 Plan — Harden the Python stack

Decisions backing this plan: [ADR-0001 … ADR-0009](adr/README.md).

## Success criteria (exit gates)

1. **Analyst path**: a real dbt-core 1.11 project with `sem_*.yml` semantic
   models, checked into git, is queryable from Amazon QuickSight through the
   stock PostgreSQL connector — tables discoverable in the UI, filters and
   date ranges working, `non_additive_dimension` guardrails provably enforced.
2. **LLM path**: an MCP client (scripted in CI; real assistant in the demo
   runbook) answers metric questions via `list_metrics` / `get_dimensions` /
   `query_metrics`, with answers numerically identical to the wire path.

## Workstreams

| WS | Deliverable | ADR | Depends on |
|---|---|---|---|
| WS1 ✅ | `test_projects/finance_demo/` — real dbt 1.11 project (seeds, models, `sem_*.yml` with semi-additive `month_end_balance`, joins, saved queries) + dbt-duckdb harness + **L3 golden-numbers suite** (14 tests green). See [ws1-golden-numbers.md](ws1-golden-numbers.md) | 0003 | — |
| WS2 ✅ | Filter/ORDER/LIMIT translation (`engine/filters.py`) + compiler request params. 16 tests green. See [ws2-ws3-filters-catalog.md](ws2-ws3-filters-catalog.md) | 0005 | WS1 |
| WS3 ✅ | Virtual catalog (`engine/catalog.py` + `network/catalog.py`): auto-cube per model + `all_metrics` + saved-query cubes, pre-scoped to valid fields; deterministic OIDs; information_schema/pg_catalog emulation for QuickSight discovery. Shared `valid_group_bys` API. 16 tests + 4 socket-level QuickSight E2E green. Power BI/Npgsql `pg_type` deferred (stub). | 0004, 0010, 0011 | WS1 |
| WS4 | SCRAM-SHA-256 auth, verifier file store, metric ACLs, enforced row filters, JSONL audit log | 0006 | WS2 (filter merge path) |
| WS5 | Plan cache (LRU, manifest-hash keyed, single-flight) + bounded planner pool; flag-gated result cache | 0007 | WS2 |
| WS6 ✅ | Embedded MCP server (`mcpserver/`): dbt-mcp-compatible list_metrics/get_dimensions/query_metrics, stdio + streamable-HTTP, MCP≡wire consistency proven. 8 tests + live stdio-client E2E green. See [ws6-mcp-server.md](ws6-mcp-server.md) | 0008 | WS1, WS2 |
| WS7 ✅ | Manifest store (fsspec bundle) + offline plan-only CI gate + `from_bundle` runtime consumer + blue/green hot-swap (poll+signal) + GitHub Actions CD (publish on release only). Proven CI→store→runtime→BI loop live. See [ws7-complete.md](ws7-complete.md) | 0002, 0012–0015 | WS3 |

Sequencing: **WS1 first** — it forces real-adapter wiring and becomes the
substrate every other workstream tests against. WS2/WS3 parallelize after it;
WS4–WS7 layer on top.

## Version alignment

- Bump pins to dbt-core **1.11.x** + matching `metricflow` / `dbt-metricflow`
  / `dbt-semantic-interfaces`; verify the exact compat matrix against the
  internal mirror at implementation time (offline assumption — do not trust
  requirements.txt comments). Add the boot-time lockstep assertion (ADR-0002).
- Add: `dbt-duckdb`, `duckdb`, `scramp`, `mcp`. Test-only: driver corpus
  tooling, fuzz harness for the frame parser (ADR-0009 mitigation).

## Acceptance runbooks (manual, pre-prod)

- **QuickSight**: VPC connection → PostgreSQL datasource → verify
  `semantic_layer.*` tables appear → build an analysis on the saved-query
  cube → apply a relative-date filter → confirm monthly semi-additive metric
  equals end-of-month seeds; confirm audit records with manifest SHA.
- **Power BI**: Npgsql connect (DirectQuery) → navigator shows virtual
  tables → same assertions. (Power BI is qualified via L4 corpora in CI;
  full UAT may trail QuickSight.)
- **LLM**: point an MCP-capable assistant at the streamable-HTTP endpoint →
  "what was MRR by region in March?" → verify tool call sequence, ACL
  enforcement (deny an unauthorized metric), and numeric match to psql.

## Out of scope (recorded, not forgotten)

- Kerberos/LDAP (Phase 2, behind the user-store interface — ADR-0006)
- Rust edge (triggers defined in ADR-0009)
- Arrow Flight SQL / GraphQL API parity endpoints (ADR-0001)
- Pre-aggregation materialization (ADR-0007)
- Snowflake/Redshift dialect UAT automation

## Resolved decisions (product owner, 2026-07-04)

1. **User store**: SCRAM verifier file for Phase 1; LDAP/AD deliberately
   deferred — revisit at Phase 2 planning.
2. **Result cache**: default-off confirmed.
3. **Catalog exposure**: saved-queries-only REJECTED — analysts get full
   semantic-graph exposure (dbt Cloud / Lightdash UX parity). See
   [ADR-0010](adr/0010-full-semantic-exposure-catalog.md): auto-cube per
   semantic model + `all_metrics` wide table + saved queries, role-filtered
   catalog, query-time guardrail errors with valid-alternative hints.
