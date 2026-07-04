# ADR-0008: Embedded MCP server for LLM access to the semantic layer

Date: 2026-07-04 · Status: **Accepted**

## Context

Success criterion #2: "an LLM querying the semantic layer." dbt Labs' dbt-MCP
server defines the reference tool surface for this — its Semantic Layer tool
group is `list_metrics`, `get_dimensions`, `query_metrics` — but that package
fulfills those tools by calling dbt Cloud SL APIs (GraphQL/JDBC), which do not
exist in our air-gapped, dbt-core-only deployment. An LLM answering
"what was MRR by region last quarter?" must go through the *same* guardrails
(non-additive windows, metric ACLs, row filters, audit) as an analyst —
an LLM with raw warehouse SQL access would be a governance bypass.

## Decision

1. FlowProxy **embeds its own MCP server** (official `mcp` Python SDK /
   FastMCP), exposing the dbt-MCP-compatible Semantic Layer tool trio:
   - `list_metrics()` → name, type, label, description from the registry;
   - `get_dimensions(metrics)` → dimensions legally group-able for those
     metrics (MetricFlow linkable-elements resolution — not the full
     dimension list, the *valid* one);
   - `query_metrics(metrics, group_by, where?, order_by?, limit?)` →
     executes through the identical pipeline as wire queries
     (validate → plan → cache → execute → audit) and returns rows.
   Tool names/shapes intentionally match dbt-MCP so prompts, agent configs,
   and skills written against dbt's server work unmodified.
2. The MCP server runs **in-process**, calling `SemanticRegistry` /
   `SemanticCompiler` / `WarehouseExecutor` directly — no loopback SQL hop,
   one code path, one audit trail. MCP calls carry a service identity and are
   subject to ADR-0006 ACLs and row filters like any session.
3. Transports: **stdio** (local agent / CLI) and **streamable HTTP** on a
   separate port (default 8181, never 5432) for the bank's internal agent
   gateway. Anonymous access is not offered on the HTTP transport.
4. The LLM runtime itself is out of scope — any on-prem/private-cloud model
   with MCP client support works; the acceptance test uses a scripted MCP
   client (deterministic), plus a manual demo runbook with a real assistant.

## Consequences

- Adds `mcp` to requirements; new module `mcpserver/` beside `network/`.
- `get_dimensions` requires exposing MetricFlow's valid-group-by resolution
  through the compiler — also reusable later for better SQL error messages.
- CI gains an L-MCP test layer: scripted client calls all three tools against
  the DuckDB fixture and asserts the semi-additive golden numbers match the
  wire-path results exactly (one engine, provably consistent answers).

## Alternatives considered

- **Run dbt Labs' dbt-mcp package alongside**: expects dbt Cloud SL
  endpoints; bridging it would bolt a second query path onto the proxy,
  splitting guardrails and audit. Rejected.
- **Generic text-to-SQL tool against the PG port**: the LLM would emit
  arbitrary SQL; the whole point of the semantic layer is that consumers
  request *metrics*, not SQL. Rejected.
