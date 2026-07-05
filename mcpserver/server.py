"""WS6 — embedded MCP server (ADR-0008).

Exposes the dbt-mcp-compatible Semantic Layer tool trio so an LLM can query the
dbt-core semantic layer directly:

    list_metrics()                         -> inventory of metrics
    get_dimensions(metrics)                -> dimensions valid for those metrics
    query_metrics(metrics, group_by, ...)  -> execute and return rows

Tool names and shapes intentionally mirror dbt Labs' dbt-mcp
(https://github.com/dbt-labs/dbt-mcp) so prompts/agents written against that
server work unmodified. BUT the implementation calls FlowProxy's own
``SemanticCompiler`` in-process — no dbt Cloud SL API — so it runs against
dbt-core + MetricFlow in an air-gapped deployment, and flows through the SAME
guardrail pipeline as a wire-protocol query (validate → plan → execute). An LLM
therefore cannot bypass semi-additivity, join-path validity, or (later) ACLs.

Transports: ``stdio`` (local agent) and ``streamable-http`` (internal agent
gateway) — never on the PostgreSQL port.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from engine.compiler import SemanticCompiler
from engine.exceptions import FlowProxyError

logger = logging.getLogger("flowproxy.mcp")


class FlowProxyMCP:
    """Wraps a :class:`SemanticCompiler` in dbt-mcp-compatible MCP tools."""

    def __init__(self, compiler: SemanticCompiler, *, name: str = "flowproxy-semantic-layer") -> None:
        self._compiler = compiler
        self.mcp = FastMCP(name)
        self._register_tools()

    # ------------------------------------------------------------------ #
    # Tool registration
    # ------------------------------------------------------------------ #
    def _register_tools(self) -> None:
        mcp = self.mcp
        compiler = self._compiler

        @mcp.tool(
            name="list_metrics",
            description=(
                "Provides an inventory of all available metrics in the dbt Semantic "
                "Layer. Returns metric names, types, labels, and descriptions. Call "
                "this first to discover what can be queried."
            ),
        )
        def list_metrics() -> list[dict[str, Any]]:
            metrics = compiler.metric_catalog()
            logger.info("mcp.list_metrics -> %d metrics", len(metrics))
            return [
                {
                    "name": m.name,
                    "type": m.metric_type,
                    "label": m.label,
                    "description": m.description,
                }
                for m in metrics
            ]

        @mcp.tool(
            name="get_dimensions",
            description=(
                "Identifies the dimensions available to group or filter the specified "
                "metrics. Only dimensions reachable for those metrics (valid join "
                "paths) are returned — use these names verbatim as group_by / where "
                "targets in query_metrics."
            ),
        )
        def get_dimensions(metrics: list[str]) -> list[dict[str, Any]]:
            try:
                dims = compiler.valid_group_bys(metrics)
            except FlowProxyError as exc:
                return [{"error": exc.message, "detail": exc.detail}]
            logger.info("mcp.get_dimensions(%s) -> %d dimensions", metrics, len(dims))
            return [
                {
                    "name": d.name,
                    "type": d.dimension_type,
                    "label": d.label,
                    "description": d.description,
                    "queryable_granularities": (
                        ["day", "week", "month", "quarter", "year"] if d.is_time else None
                    ),
                }
                for d in dims
            ]

        @mcp.tool(
            name="query_metrics",
            description=(
                "Executes a query against metrics in the dbt Semantic Layer and "
                "returns the result rows. `metrics` is a list of metric names. "
                "`group_by` is a list of dimension names (from get_dimensions); for a "
                "time grain use the `name__grain` form, e.g. 'metric_time__month'. "
                "`where` is an optional list of MetricFlow filter expressions of the "
                "form \"{{ Dimension('<entity>__<dim>') }} = '<value>'\". `order_by` "
                "prefixes a name with '-' for descending. `limit` caps rows. The "
                "metric's own aggregation and any semi-additive rules are always "
                "enforced."
            ),
        )
        def query_metrics(
            metrics: list[str],
            group_by: list[str] | None = None,
            where: list[str] | None = None,
            order_by: list[str] | None = None,
            limit: int | None = None,
        ) -> dict[str, Any]:
            logger.info(
                "mcp.query_metrics metrics=%s group_by=%s where=%s order_by=%s limit=%s",
                metrics, group_by, where, order_by, limit,
            )
            try:
                result = compiler.execute_request(
                    metrics,
                    group_by or [],
                    where_constraints=where or None,
                    order_by=order_by or None,
                    limit=limit,
                )
            except FlowProxyError as exc:
                logger.warning("mcp.query_metrics failed: %s", exc.message)
                return {
                    "error": exc.message,
                    "detail": exc.detail,
                    "sqlstate": exc.sqlstate,
                }
            # LLM-friendly shape: list of {column: value} dicts.
            rows = [dict(zip(result.columns, [_jsonable(v) for v in row])) for row in result.rows]
            logger.info("mcp.query_metrics -> %d rows", len(rows))
            return {"columns": result.columns, "rows": rows, "row_count": result.row_count}


def _jsonable(value: Any) -> Any:
    """Coerce warehouse cell values into JSON-serializable primitives."""
    import datetime as dt
    import decimal

    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    return value


def build_mcp(compiler: SemanticCompiler) -> FastMCP:
    """Convenience factory returning the ready-to-run FastMCP instance."""
    return FlowProxyMCP(compiler).mcp
