"""WS3 — the virtual catalog model (ADR-0010 / ADR-0011).

Projects the semantic manifest into **explorable cubes** an analyst sees as
tables in QuickSight:

  * one auto-cube per semantic model (``semantic_layer.<model>``), pre-scoped
    to the metrics anchored on that model plus every dimension reachable for
    them (so most invalid slices are impossible to build);
  * ``semantic_layer.all_metrics`` — the cross-model wide table;
  * one cube per saved query (certified, always-valid).

This module is pure model — no wire protocol. ``network/catalog.py`` renders
these objects into pg_catalog / information_schema / JDBC introspection
answers. Every column carries a PostgreSQL type OID so BI drivers coerce
values correctly, and OIDs are deterministic per manifest so pooled/reconnecting
clients never see drift within a deployed manifest version.
"""

from __future__ import annotations

import logging
import zlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Sequence

from engine.registry import METRIC_TIME

if TYPE_CHECKING:
    from engine.compiler import DimensionInfo, MetricInfo, SemanticCompiler

logger = logging.getLogger("flowproxy.engine.catalog")

CATALOG_SCHEMA: str = "semantic_layer"
ALL_METRICS_TABLE: str = "all_metrics"

# PostgreSQL type OIDs (kept in sync with network/protocol.py).
OID_INT8: int = 20
OID_FLOAT8: int = 701
OID_NUMERIC: int = 1700
OID_VARCHAR: int = 1043
OID_DATE: int = 1082
OID_TIMESTAMP: int = 1114

# Base OID floor for synthetic relation/type OIDs. Real pg system OIDs live
# below 16384 (FirstNormalObjectId); we place synthetic ones far above to avoid
# any collision with emulated catalog rows.
_SYNTHETIC_OID_BASE: int = 100_000


def _stable_oid(manifest_sha: str, *parts: str) -> int:
    """Deterministic synthetic OID from the manifest hash + object path."""
    key = "\x1f".join((manifest_sha, *parts)).encode("utf-8")
    return _SYNTHETIC_OID_BASE + (zlib.crc32(key) & 0x7FFFFFFF) % 900_000_000


@dataclass(frozen=True)
class CatalogColumn:
    """One column of a virtual cube."""

    name: str                      # what the analyst sees / QuickSight SELECTs
    semantic_name: str             # metric name or qualified dimension name
    is_metric: bool
    type_oid: int
    ordinal: int
    label: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class CatalogTable:
    """A virtual cube exposed as a table in the ``semantic_layer`` schema."""

    name: str
    schema: str
    table_oid: int
    kind: str                       # 'model' | 'all_metrics' | 'saved_query'
    columns: list[CatalogColumn] = field(default_factory=list)
    description: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"

    def column(self, name: str) -> CatalogColumn | None:
        for c in self.columns:
            if c.name == name:
                return c
        return None


@dataclass(frozen=True)
class VirtualCatalog:
    """The full set of cubes derived from one manifest version."""

    manifest_sha: str
    tables: list[CatalogTable] = field(default_factory=list)

    def table(self, name: str) -> CatalogTable | None:
        for t in self.tables:
            if t.name == name or t.qualified == name:
                return t
        return None


# --------------------------------------------------------------------------- #
# Type mapping
# --------------------------------------------------------------------------- #
def _metric_oid(metric_type: str) -> int:
    # Simple/ratio/derived metrics are numeric; NUMERIC preserves exact
    # decimals (balances, money) better than FLOAT8 for a bank.
    return OID_NUMERIC


def _dimension_oid(dim_type: str, name: str) -> int:
    if dim_type == "time" or name == METRIC_TIME:
        # Grain-free time dimensions render as timestamps (QuickSight then
        # offers its own date hierarchy on top).
        return OID_TIMESTAMP
    return OID_VARCHAR


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
class CatalogBuilder:
    """Builds a :class:`VirtualCatalog` from the compiler's semantic view."""

    def __init__(self, compiler: "SemanticCompiler", manifest_sha: str) -> None:
        self._compiler = compiler
        self._sha = manifest_sha

    def build(self) -> VirtualCatalog:
        tables: list[CatalogTable] = []
        tables.extend(self._model_cubes())
        tables.append(self._all_metrics_cube())
        tables.extend(self._saved_query_cubes())
        logger.info(
            "virtual catalog built: %d cubes (%s)",
            len(tables),
            ", ".join(t.name for t in tables),
        )
        return VirtualCatalog(manifest_sha=self._sha, tables=tables)

    # ------------------------------------------------------------------ #
    # Auto-cube per semantic model (ADR-0011: pre-scoped to valid fields)
    # ------------------------------------------------------------------ #
    def _model_cubes(self) -> list[CatalogTable]:
        manifest = self._compiler.manifest  # in-memory PydanticSemanticManifest
        metrics_by_model = self._metrics_by_model(manifest)

        cubes: list[CatalogTable] = []
        for model in manifest.semantic_models:
            model_metrics = sorted(metrics_by_model.get(model.name, []))
            if not model_metrics:
                # A pure dimension model (e.g. `accounts`) has no metrics of its
                # own; it contributes dimensions to other cubes, not a cube.
                logger.debug("model %r has no metrics; not exposed as a cube", model.name)
                continue
            cubes.append(self._build_cube(model.name, "model", model_metrics))
        return cubes

    def _metrics_by_model(self, manifest) -> dict[str, list[str]]:
        """Map semantic model name → metrics whose measures it defines."""
        measure_to_model: dict[str, str] = {}
        for model in manifest.semantic_models:
            for measure in model.measures:
                measure_to_model[measure.name] = model.name

        out: dict[str, list[str]] = {}
        for metric in manifest.metrics:
            for measure_name in self._metric_measures(metric):
                model_name = measure_to_model.get(measure_name)
                if model_name:
                    out.setdefault(model_name, []).append(metric.name)
                    break  # anchor a metric to one model (its first measure)
        return out

    @staticmethod
    def _metric_measures(metric) -> list[str]:
        tp = getattr(metric, "type_params", None)
        if tp is None:
            return []
        names: list[str] = []
        measure = getattr(tp, "measure", None)
        if measure is not None:
            names.append(measure.name)
        for m in getattr(tp, "measures", None) or []:
            names.append(m.name)
        return names

    # ------------------------------------------------------------------ #
    # all_metrics wide table
    # ------------------------------------------------------------------ #
    def _all_metrics_cube(self) -> CatalogTable:
        all_metrics = [m.name for m in self._compiler.metric_catalog()]
        return self._build_cube(ALL_METRICS_TABLE, "all_metrics", sorted(all_metrics))

    # ------------------------------------------------------------------ #
    # Saved-query cubes
    # ------------------------------------------------------------------ #
    def _saved_query_cubes(self) -> list[CatalogTable]:
        manifest = self._compiler.manifest
        cubes: list[CatalogTable] = []
        for sq in getattr(manifest, "saved_queries", None) or []:
            qp = getattr(sq, "query_params", None)
            metrics = list(getattr(qp, "metrics", None) or []) if qp else []
            if not metrics:
                continue
            cubes.append(
                self._build_cube(
                    sq.name,
                    "saved_query",
                    sorted(metrics),
                    description=getattr(sq, "description", None),
                )
            )
        return cubes

    # ------------------------------------------------------------------ #
    # Shared cube construction
    # ------------------------------------------------------------------ #
    def _build_cube(
        self,
        name: str,
        kind: str,
        metrics: Sequence[str],
        *,
        description: str | None = None,
    ) -> CatalogTable:
        metric_infos = {m.name: m for m in self._compiler.metric_catalog()}
        # Pre-scope: only dimensions reachable for THIS cube's metrics
        # (MetricFlow linkable-elements resolution) become columns (ADR-0011).
        dims = self._compiler.valid_group_bys(list(metrics))

        columns: list[CatalogColumn] = []
        ordinal = 0
        # Dimensions first (grouped by entity via sorted qualified name), then
        # metrics — keeps QuickSight's field list navigable (ADR-0010).
        for d in dims:
            columns.append(
                CatalogColumn(
                    name=d.name,
                    semantic_name=d.name,
                    is_metric=False,
                    type_oid=_dimension_oid(d.dimension_type, d.name),
                    ordinal=ordinal,
                    label=d.label,
                    description=d.description,
                )
            )
            ordinal += 1
        for metric_name in metrics:
            mi = metric_infos.get(metric_name)
            columns.append(
                CatalogColumn(
                    name=metric_name,
                    semantic_name=metric_name,
                    is_metric=True,
                    type_oid=_metric_oid(mi.metric_type if mi else "simple"),
                    ordinal=ordinal,
                    label=mi.label if mi else None,
                    description=mi.description if mi else None,
                )
            )
            ordinal += 1

        return CatalogTable(
            name=name,
            schema=CATALOG_SCHEMA,
            table_oid=_stable_oid(self._sha, CATALOG_SCHEMA, name),
            kind=kind,
            columns=columns,
            description=description,
        )
