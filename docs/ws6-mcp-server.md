# WS6 — Embedded MCP server for LLM access (complete)

Implements [ADR-0008](adr/0008-embedded-mcp-server.md).

## Success criterion #2, delivered

An LLM (via any MCP client) queries the dbt-core semantic layer through the
dbt-mcp-compatible tool trio, with the **same guardrails** as a BI analyst.
Verified end-to-end: a real `mcp` stdio client launched the server, called
`list_metrics` and `query_metrics`, and got EMEA 1500 / AMER 4000 — the
semi-additive `MAX(balance_date)` window visible in the executed SQL.

## Tools (mirror dbt-labs/dbt-mcp)

| Tool | Signature | Returns |
|---|---|---|
| `list_metrics` | `()` | metric name, type, label, description |
| `get_dimensions` | `(metrics)` | dimensions **valid** for those metrics (+ time grains) |
| `query_metrics` | `(metrics, group_by?, where?, order_by?, limit?)` | columns + rows |

Names/shapes match dbt-mcp so prompts and agents written for dbt Labs' server
work unmodified — but the implementation calls FlowProxy's own
`SemanticCompiler` in-process (no dbt Cloud SL API), so it runs on dbt-core +
MetricFlow in an air-gap.

## Why in-process matters (the guardrail argument)

`query_metrics` calls the identical `compiler.execute_request` used by the
PostgreSQL wire path — one pipeline: validate → plan (MetricFlow) → execute.
Consequences, all tested:

- an LLM **cannot** bypass semi-additivity (`account_balance` returns
  end-of-period, never a sum);
- an LLM **cannot** reach an invalid dimension — `get_dimensions` returns only
  reachable ones, and an invalid `group_by` yields a structured error, not
  wrong data;
- MCP numbers **provably equal** wire numbers
  (`test_mcp_numbers_equal_wire_numbers`);
- errors surface as `{error, detail, sqlstate}`, not crashes.

## Run it

```bash
# stdio (local agent / Claude Desktop style)
FLOWPROXY_MANIFEST=… FLOWPROXY_DBT_PROJECT_DIR=… \
uv run python -m mcpserver

# streamable-HTTP (internal agent gateway) — port 8181, never 5432
FLOWPROXY_MCP_TRANSPORT=streamable-http uv run python -m mcpserver
```

Point any MCP-capable assistant at it; ask "what was the account balance by
region in March?" → it calls `list_metrics` → `get_dimensions` →
`query_metrics` and answers from governed data.

## Tests (8, all green)

`test_ws6_mcp.py`: tool contract, list/get/query behavior, the
MCP≡wire consistency invariant, and guarded errors for unknown metrics /
invalid join paths.

## Deferred

- MCP auth (bearer on the HTTP transport) + per-identity ACLs land with WS4;
  the tool layer already routes through the compiler, so ACL enforcement is
  additive at that seam.
