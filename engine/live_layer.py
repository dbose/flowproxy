"""WS7c — the hot-swappable live semantic layer (ADR-0014).

Bundles everything derived from one manifest version — compiler, registry,
virtual catalog, catalog responder — behind a single atomically-swappable
reference. Request handlers read the current ``LiveSemanticLayer`` once at
request start, so an in-flight query always completes against the manifest it
began on, and a swap never yields a half-built catalog.

Refresh is serialized: building a new layer runs dbt's adapter bootstrap, which
mutates global dbt library state (WS7 spike finding), so concurrent rebuilds are
unsafe. A single lock guards construction; the swap itself is one reference
assignment.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from engine.catalog import CatalogBuilder, VirtualCatalog
from engine.compiler import SemanticCompiler
from engine.manifest_store import Bundle, ManifestStore
from engine.parser import SQLExtractor
from network.catalog import CatalogResponder

logger = logging.getLogger("flowproxy.live_layer")


@dataclass(frozen=True)
class LiveSemanticLayer:
    """An immutable snapshot of everything derived from one manifest version.

    Holds the compiler, the SQL extractor (bound to this manifest's registry),
    the virtual catalog, and its responder — so a request can capture one
    consistent set and finish against it even across a hot-swap.
    """

    version: str
    manifest_sha: str
    compiler: SemanticCompiler
    extractor: SQLExtractor
    catalog: VirtualCatalog
    catalog_responder: CatalogResponder

    @classmethod
    def from_bundle(
        cls, bundle: Bundle, *, warehouse_env: Mapping[str, str] | None = None,
        skeleton_root: Path | None = None, max_rows: int = 1_000_000,
    ) -> "LiveSemanticLayer":
        compiler = SemanticCompiler.from_bundle(
            bundle, warehouse_env=warehouse_env, skeleton_root=skeleton_root
        )
        manifest_sha = hashlib.sha256(bundle.semantic_manifest_json.encode()).hexdigest()[:12]
        catalog = CatalogBuilder(compiler, manifest_sha).build()
        return cls(
            version=bundle.version,
            manifest_sha=manifest_sha,
            compiler=compiler,
            extractor=SQLExtractor(compiler.registry, max_rows=max_rows),
            catalog=catalog,
            catalog_responder=CatalogResponder(catalog),
        )


class SemanticLayerManager:
    """Holds the current LiveSemanticLayer and performs atomic blue/green swaps.

    Thread-safe: ``current`` is read lock-free (a single attribute read);
    ``refresh`` serializes construction under a lock so the global-state-mutating
    adapter bootstrap never races.
    """

    def __init__(
        self,
        store: ManifestStore,
        *,
        warehouse_env: Mapping[str, str] | None = None,
    ) -> None:
        self._store = store
        self._warehouse_env = dict(warehouse_env or {})
        self._current: LiveSemanticLayer | None = None
        self._build_lock = threading.Lock()

    @property
    def current(self) -> LiveSemanticLayer:
        """The live layer. Read once per request; never None after load()."""
        layer = self._current
        if layer is None:
            raise RuntimeError("semantic layer not loaded; call load() first")
        return layer

    def load(self) -> LiveSemanticLayer:
        """Initial synchronous load at boot."""
        with self._build_lock:
            bundle = self._store.resolve_latest()
            layer = LiveSemanticLayer.from_bundle(bundle, warehouse_env=self._warehouse_env)
            self._current = layer
            logger.info("loaded semantic layer version=%s sha=%s", layer.version, layer.manifest_sha)
            return layer

    def refresh(self, *, force: bool = False) -> bool:
        """Check the store; if a new version exists, build + atomically swap.

        Returns True if a swap happened. Builds the new layer BEFORE swapping
        (blue/green) — the current layer keeps serving until the flip, and a
        failed build leaves the current one running.
        """
        with self._build_lock:  # serialize: adapter bootstrap mutates global state
            try:
                latest_version = self._store.resolve_version()
            except Exception as exc:
                logger.warning("refresh: could not resolve store version: %s", exc)
                return False

            current = self._current
            if not force and current is not None and latest_version == current.version:
                logger.debug("refresh: already on version %s; no swap", latest_version)
                return False

            logger.info("refresh: building new layer (version %s)", latest_version)
            try:
                bundle = self._store.resolve_latest()
                new_layer = LiveSemanticLayer.from_bundle(
                    bundle, warehouse_env=self._warehouse_env
                )
            except Exception:
                logger.exception(
                    "refresh: build FAILED; keeping current version %s",
                    current.version if current else "<none>",
                )
                return False

            old_version = current.version if current else "<none>"
            self._current = new_layer  # atomic swap (single reference assignment)
            logger.info(
                "refresh: swapped %s → %s (blue/green)", old_version, new_layer.version
            )
            return True
