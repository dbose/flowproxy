"""Standalone MCP server entrypoint.

    uv run python -m mcpserver                    # stdio (local agent)
    FLOWPROXY_MCP_TRANSPORT=streamable-http \
    uv run python -m mcpserver                    # HTTP (agent gateway)

Shares the SemanticCompiler bootstrap with the PostgreSQL proxy (same manifest,
same guardrails). Runs on its own port (default 8181) — never 5432.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from engine.compiler import SemanticCompiler
from engine.exceptions import FlowProxyError
from mcpserver.server import FlowProxyMCP


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("FLOWPROXY_LOG_LEVEL", "INFO").upper(),
        stream=sys.stderr,  # stdout is the MCP stdio channel — keep logs off it
        format="%(asctime)s level=%(levelname)s logger=%(name)s msg=%(message)s",
    )
    logger = logging.getLogger("flowproxy.mcp.main")

    manifest_path = Path(os.environ.get("FLOWPROXY_MANIFEST", "target/semantic_manifest.json"))
    dbt_project_dir_env = os.environ.get("FLOWPROXY_DBT_PROJECT_DIR")
    dbt_project_dir = Path(dbt_project_dir_env) if dbt_project_dir_env else None
    dry_run = os.environ.get("FLOWPROXY_COMPILER_MODE", "metricflow").lower() == "dryrun"

    try:
        compiler = SemanticCompiler(manifest_path, dbt_project_dir, dry_run=dry_run)
    except FlowProxyError as exc:
        logger.critical("MCP boot failed: %s (%s)", exc.message, exc.detail)
        raise SystemExit(1) from exc

    server = FlowProxyMCP(compiler)
    transport = os.environ.get("FLOWPROXY_MCP_TRANSPORT", "stdio")

    if transport == "streamable-http":
        server.mcp.settings.host = os.environ.get("FLOWPROXY_MCP_HOST", "0.0.0.0")
        server.mcp.settings.port = int(os.environ.get("FLOWPROXY_MCP_PORT", "8181"))
        logger.info(
            "starting MCP server (streamable-http) on %s:%s",
            server.mcp.settings.host,
            server.mcp.settings.port,
        )
    else:
        logger.info("starting MCP server (stdio)")

    server.mcp.run(transport=transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
