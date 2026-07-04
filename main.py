"""FlowProxy entrypoint.

Boot order:
    1. structured logging
    2. SemanticCompiler  (loads target/semantic_manifest.json, builds registry)
    3. SQLExtractor      (bound to the registry)
    4. WarehouseExecutor (mock by default; swap for a real driver)
    5. PostgresProxyServer.serve_forever() on :5432
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from engine.compiler import SemanticCompiler
from engine.exceptions import FlowProxyError
from engine.parser import SQLExtractor
from executor.warehouse import MockWarehouseExecutor, WarehouseExecutor
from network.server import PostgresProxyServer


def configure_logging() -> None:
    level = os.environ.get("FLOWPROXY_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(asctime)s.%(msecs)03dZ level=%(levelname)s logger=%(name)s msg=%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def build_executor() -> WarehouseExecutor:
    """Executor factory. Extend with real warehouse drivers via FLOWPROXY_EXECUTOR."""
    kind = os.environ.get("FLOWPROXY_EXECUTOR", "mock").lower()
    if kind == "mock":
        return MockWarehouseExecutor(row_count=int(os.environ.get("FLOWPROXY_MOCK_ROWS", "5")))
    raise SystemExit(
        f"unknown FLOWPROXY_EXECUTOR={kind!r}; implement a WarehouseExecutor "
        "subclass in executor/warehouse.py and register it here"
    )


async def amain() -> None:
    logger = logging.getLogger("flowproxy.main")

    max_rows = int(os.environ.get("FLOWPROXY_MAX_ROWS", "1000000"))
    executor = build_executor()
    manifest_uri = os.environ.get("FLOWPROXY_MANIFEST_URI")

    server_kwargs = dict(
        host=os.environ.get("FLOWPROXY_HOST", "0.0.0.0"),
        port=int(os.environ.get("FLOWPROXY_PORT", "5432")),
        password=os.environ.get("FLOWPROXY_PASSWORD") or None,
    )

    if manifest_uri:
        # STORE MODE (WS7): consume a bundle from the ManifestStore + hot-swap.
        await _serve_store_mode(manifest_uri, executor, max_rows, server_kwargs, logger)
    else:
        # DIRECT MODE (back-compat): a local manifest path + dbt project dir.
        await _serve_direct_mode(executor, max_rows, server_kwargs, logger)


async def _serve_store_mode(manifest_uri, executor, max_rows, server_kwargs, logger) -> None:
    import asyncio

    from engine.live_layer import SemanticLayerManager
    from engine.manifest_store import open_store
    from network.refresh import RefreshCoordinator

    logger.info("booting flowproxy in STORE mode: uri=%s", manifest_uri)
    store = open_store(manifest_uri)
    manager = SemanticLayerManager(store, warehouse_env=dict(os.environ))
    layer = manager.load()
    logger.info(
        "loaded bundle %s: %d cubes exposed under 'semantic_layer'",
        layer.version, len(layer.catalog.tables),
    )

    poll = float(os.environ.get("FLOWPROXY_MANIFEST_POLL_INTERVAL", "300"))
    coordinator = RefreshCoordinator(manager, poll_interval=poll)
    coordinator.install_signal_handler()
    coordinator.start()

    server = PostgresProxyServer(
        manager.current.compiler,        # unused in provider mode, kept for API
        manager.current.extractor,
        executor,
        catalog_responder=manager.current.catalog_responder,
        layer_provider=lambda: manager.current,   # ← per-request hot-swap read
        **server_kwargs,
    )
    try:
        await server.serve_forever()
    finally:
        await coordinator.stop()


async def _serve_direct_mode(executor, max_rows, server_kwargs, logger) -> None:
    manifest_path = Path(os.environ.get("FLOWPROXY_MANIFEST", "target/semantic_manifest.json"))
    dbt_project_dir_env = os.environ.get("FLOWPROXY_DBT_PROJECT_DIR")
    dbt_project_dir = Path(dbt_project_dir_env) if dbt_project_dir_env else None
    dry_run = os.environ.get("FLOWPROXY_COMPILER_MODE", "metricflow").lower() == "dryrun"

    logger.info(
        "booting flowproxy in DIRECT mode: manifest=%s compiler_mode=%s",
        manifest_path, "dryrun" if dry_run else "metricflow",
    )
    compiler = SemanticCompiler(manifest_path, dbt_project_dir, dry_run=dry_run)
    extractor = SQLExtractor(compiler.registry, max_rows=max_rows)
    catalog_responder = _build_catalog(compiler, manifest_path)

    server = PostgresProxyServer(
        compiler,
        extractor,
        executor,
        catalog_responder=catalog_responder,
        **server_kwargs,
    )
    await server.serve_forever()


def _build_catalog(compiler: SemanticCompiler, manifest_path: Path):
    """Construct the virtual catalog responder from the loaded manifest."""
    import hashlib

    from engine.catalog import CatalogBuilder
    from network.catalog import CatalogResponder

    sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()[:12]
    catalog = CatalogBuilder(compiler, sha).build()
    logging.getLogger("flowproxy.main").info(
        "virtual catalog ready: %d cubes exposed under schema 'semantic_layer'",
        len(catalog.tables),
    )
    return CatalogResponder(catalog)


def main() -> None:
    configure_logging()
    try:
        import uvloop  # type: ignore[import-not-found]

        uvloop.install()
        logging.getLogger("flowproxy.main").info("uvloop event loop installed")
    except ImportError:
        pass

    try:
        asyncio.run(amain())
    except FlowProxyError as exc:
        logging.getLogger("flowproxy.main").critical("boot failed: %s (%s)", exc.message, exc.detail)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        logging.getLogger("flowproxy.main").info("shutdown requested")


if __name__ == "__main__":
    main()
