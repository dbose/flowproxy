"""WS6 — embedded MCP server tests (ADR-0008).

The headline requirement: an LLM querying via MCP gets numbers IDENTICAL to the
wire path, because both flow through the same SemanticCompiler pipeline. Plus:
the dbt-mcp tool contract (names/shapes), and guardrail enforcement (an LLM
cannot bypass semi-additivity or reach invalid dimensions).
"""

from __future__ import annotations

import pytest

from mcpserver.server import FlowProxyMCP

pytestmark = pytest.mark.usefixtures("finance_demo_built")


@pytest.fixture(scope="module")
def mcp(compiler):
    return FlowProxyMCP(compiler).mcp


async def _call(mcp, name: str, **arguments):
    """Invoke a tool and return its structured output (dict/list)."""
    result = await mcp.call_tool(name, arguments)
    # FastMCP returns (content_blocks, structured_output) or structured directly.
    if isinstance(result, tuple):
        return result[1]
    return result


# --------------------------------------------------------------------------- #
# Tool contract — matches dbt-mcp
# --------------------------------------------------------------------------- #
async def test_tools_registered(mcp):
    tools = {t.name for t in await mcp.list_tools()}
    assert tools == {"list_metrics", "get_dimensions", "query_metrics"}


async def test_list_metrics(mcp):
    out = await _call(mcp, "list_metrics")
    metrics = out["result"] if isinstance(out, dict) and "result" in out else out
    names = {m["name"] for m in metrics}
    assert {"account_balance", "total_transactions"} <= names
    abal = next(m for m in metrics if m["name"] == "account_balance")
    assert abal["label"] == "Account Balance (EOP)"
    assert abal["type"] == "simple"


async def test_get_dimensions_scoped_to_metric(mcp):
    out = await _call(mcp, "get_dimensions", metrics=["account_balance"])
    dims = out["result"] if isinstance(out, dict) and "result" in out else out
    names = {d["name"] for d in dims}
    assert "account__region" in names
    # Guardrail: transaction dims are NOT reachable from a balance metric.
    assert "transaction__transaction_type" not in names
    # Time dims advertise queryable granularities for the LLM.
    mt = next(d for d in dims if d["name"] == "metric_time")
    assert "month" in mt["queryable_granularities"]


# --------------------------------------------------------------------------- #
# query_metrics — execution + the consistency guarantee
# --------------------------------------------------------------------------- #
async def test_query_metrics_semiadditive(mcp):
    out = await _call(
        mcp, "query_metrics",
        metrics=["account_balance"],
        group_by=["metric_time__month", "account__region"],
        order_by=["metric_time__month"],
    )
    rows = out["rows"]
    by_key = {(r["metric_time__month"][:7], r["account__region"]): float(r["account_balance"]) for r in rows}
    # Same end-of-period golden numbers as the wire/L3 path.
    assert by_key[("2024-01", "EMEA")] == 1500.0
    assert by_key[("2024-03", "AMER")] == 3000.0


async def test_query_metrics_with_where(mcp):
    out = await _call(
        mcp, "query_metrics",
        metrics=["total_transactions"],
        group_by=["account__region"],
        where=["{{ Dimension('account__region') }} = 'AMER'"],
    )
    rows = out["rows"]
    assert len(rows) == 1
    assert float(rows[0]["total_transactions"]) == 1800.0


async def test_mcp_numbers_equal_wire_numbers(mcp, compiler):
    """The ADR-0008 invariant: MCP path ≡ compiler path, provably."""
    wire = compiler.execute_request(
        ["account_balance"], ["metric_time__month", "account__region"],
        order_by=["metric_time__month"],
    )
    wire_vals = sorted(
        float(row[wire.columns.index("account_balance")]) for row in wire.rows
    )

    out = await _call(
        mcp, "query_metrics",
        metrics=["account_balance"],
        group_by=["metric_time__month", "account__region"],
        order_by=["metric_time__month"],
    )
    mcp_vals = sorted(float(r["account_balance"]) for r in out["rows"])
    assert mcp_vals == wire_vals


# --------------------------------------------------------------------------- #
# Guardrails surface as structured errors, not crashes
# --------------------------------------------------------------------------- #
async def test_unknown_metric_returns_error(mcp):
    out = await _call(mcp, "query_metrics", metrics=["does_not_exist"])
    assert "error" in out
    assert out["sqlstate"] == "42703"


async def test_invalid_join_path_returns_error(mcp):
    # transaction_type is not reachable from account_balance → guarded error.
    out = await _call(
        mcp, "query_metrics",
        metrics=["account_balance"],
        group_by=["transaction__transaction_type"],
    )
    assert "error" in out
