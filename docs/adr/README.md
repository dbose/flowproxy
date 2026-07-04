# Architecture Decision Records

| ADR | Title | Status |
|---|---|---|
| [0001](0001-postgres-wire-protocol-as-bi-interface.md) | PostgreSQL wire protocol as the primary BI interface | Accepted |
| [0002](0002-oss-dbt-core-metricflow-stack.md) | OSS dbt-core + MetricFlow stack (no dbt Cloud, no Fusion) | Accepted |
| [0003](0003-duckdb-golden-numbers-test-strategy.md) | DuckDB-backed golden-numbers integration testing | Accepted |
| [0004](0004-virtual-catalog-from-saved-queries.md) | Virtual catalog: saved queries as discoverable tables | Partially superseded by 0010 |
| [0005](0005-where-clause-to-metricflow-filters.md) | WHERE/ORDER/LIMIT translation into MetricFlow constructs | Accepted |
| [0006](0006-scram-auth-and-audit-logging.md) | SCRAM-SHA-256 authentication and audit logging | Accepted |
| [0007](0007-plan-and-result-caching.md) | Plan caching keyed by manifest hash | Accepted |
| [0008](0008-embedded-mcp-server.md) | Embedded MCP server for LLM access | Accepted |
| [0009](0009-rust-edge-deferred.md) | Rust wire-protocol edge deferred to Phase 2 | Accepted |
| [0010](0010-full-semantic-exposure-catalog.md) | Full semantic-layer exposure as the default analyst catalog | Accepted |
| [0011](0011-auto-cube-exploration-quicksight.md) | Auto-cube exploration as the QuickSight analyst experience | Accepted |
| [0012](0012-manifest-store-abstraction.md) | Manifest store abstraction and deployment bundle | Accepted |
| [0013](0013-ci-deploy-gate.md) | CI deploy gate — offline plan-only validation | Accepted |
| [0014](0014-runtime-refresh-hot-swap.md) | Runtime manifest refresh — poll + signal, atomic hot-swap | Accepted |
| [0015](0015-runtime-warehouse-credentials.md) | Runtime warehouse credentials from the environment | Accepted |

Conventions: one decision per file, immutable once accepted (supersede, don't edit).
Format: Context → Decision → Consequences → Alternatives considered.
