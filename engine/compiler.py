"""Module 1 — Core MetricFlow compiler engine.

``SemanticCompiler`` turns validated ``(metrics, dimensions)`` requests into
warehouse-dialect SQL by driving the open-source ``metricflow`` query planner
against the ``target/semantic_manifest.json`` artifact produced by
``dbt parse``.

API note
--------
Public MetricFlow does not ship a ``MetricFlowClient.from_manifest_json_file``
/ ``plan_query`` surface; the supported open-source entry points are
``MetricFlowEngine`` + ``MetricFlowQueryRequest`` + ``MetricFlowEngine.explain``,
whose ``rendered_sql`` is the compiled, optimized warehouse SQL. This module
targets that real API and shields the rest of the platform from MetricFlow's
internal package churn via layered import shims.

Because building a real engine requires a dbt adapter (the adapter supplies
the SQL dialect renderer and warehouse connection), the compiler also offers
a ``dry_run`` mode that skips adapter bootstrap — used for integration-testing
the wire path with the mock executor.
"""

from __future__ import annotations

import json
import logging
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from engine.exceptions import (
    CompilerBootstrapError,
    ManifestInvalidError,
    ManifestNotFoundError,
    QueryPlanningError,
    UnknownFieldError,
)
from engine.registry import SemanticRegistry

if TYPE_CHECKING:
    from engine.manifest_store import Bundle

logger = logging.getLogger("flowproxy.engine.compiler")


# The four semantic-stack packages MUST move in lockstep (ADR-0002). We assert
# compatible *minor* versions at boot so drift fails loudly here rather than as
# an obscure planner error. Widen these ranges deliberately when upgrading —
# after re-running `dbt parse` and the L3 golden suite against the new set.
_COMPAT_MATRIX: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    # package: (min inclusive (major, minor), max exclusive (major, minor))
    "dbt-core": ((1, 11), (1, 12)),
    "dbt-semantic-interfaces": ((0, 9), (0, 10)),
    "metricflow": ((0, 211), (0, 212)),
    "dbt-metricflow": ((0, 13), (0, 14)),
}


def _assert_compat_matrix() -> None:
    """Fail fast if the installed semantic-stack versions drift out of range."""
    import importlib.metadata as md

    problems: list[str] = []
    for pkg, (lo, hi) in _COMPAT_MATRIX.items():
        try:
            raw = md.version(pkg)
        except md.PackageNotFoundError:
            problems.append(f"{pkg}: not installed")
            continue
        try:
            major, minor = (int(p) for p in raw.split(".")[:2])
        except ValueError:
            logger.warning("could not parse version %r for %s; skipping compat check", raw, pkg)
            continue
        if not (lo <= (major, minor) < hi):
            problems.append(f"{pkg}=={raw} (supported: >={lo[0]}.{lo[1]},<{hi[0]}.{hi[1]})")

    if problems:
        raise CompilerBootstrapError(
            "semantic-stack version drift: " + "; ".join(problems),
            detail="dbt-core/metricflow/dbt-metricflow/dbt-semantic-interfaces must move in "
            "lockstep (ADR-0002). Align versions, re-run `dbt parse`, and the L3 golden suite.",
        )
    logger.info("semantic-stack compatibility matrix OK")


def _parse_semantic_manifest(raw_json: str) -> Any:
    """Canonical semantic-manifest parse (parse + dbt transform).

    Uses dbt-metricflow's ``parse_manifest_from_dbt_generated_manifest`` so the
    object is byte-for-byte what MetricFlow's engine consumes. Falls back to a
    plain ``PydanticSemanticManifest.parse_raw`` only if dbt-metricflow is
    absent (e.g. a dry-run/wire-only install) — the entity/dimension structure
    the registry reads is identical between the two (verified in WS7).
    """
    try:
        from dbt_metricflow.cli.dbt_connectors.dbt_config_accessor import (  # type: ignore[import-not-found]
            parse_manifest_from_dbt_generated_manifest,
        )
    except ImportError:
        try:
            from dbt_semantic_interfaces.implementations.semantic_manifest import (
                PydanticSemanticManifest,
            )
        except ImportError as exc:
            raise CompilerBootstrapError(
                "neither dbt-metricflow nor dbt-semantic-interfaces is installed",
                detail="pip install dbt-metricflow (see requirements.txt).",
            ) from exc
        logger.warning("dbt-metricflow unavailable; using plain manifest parse (no transform)")
        try:
            return PydanticSemanticManifest.parse_raw(raw_json)
        except Exception as exc:
            raise ManifestInvalidError(
                f"semantic manifest failed schema validation: {exc}",
                detail="Re-run `dbt parse` with the pinned dbt version.",
            ) from exc

    try:
        return parse_manifest_from_dbt_generated_manifest(raw_json)
    except Exception as exc:
        raise ManifestInvalidError(
            f"semantic manifest failed schema validation / transform: {exc}",
            detail="The manifest was produced by an incompatible dbt version; re-run `dbt parse` "
            "with the dbt-core version pinned in requirements.txt.",
        ) from exc


_SKELETON_PROJECT_TEMPLATE = textwrap.dedent(
    """\
    name: flowproxy_runtime
    version: "1.0.0"
    config-version: 2
    profile: {profile_name}
    model-paths: ["models"]
    target-path: "target"
    flags:
      send_anonymous_usage_stats: false
    """
)


def _profile_name_from_template(profiles_template: str) -> str:
    """The top-level key in profiles.template.yml IS the profile name.

    The skeleton's ``dbt_project.yml`` must reference this exact name, so we
    derive it rather than hard-coding — any valid template works.
    """
    import yaml

    parsed = yaml.safe_load(profiles_template) or {}
    keys = [k for k in parsed.keys() if k != "config"]
    if not keys:
        raise CompilerBootstrapError(
            "profiles.template.yml has no profile entry",
            detail="The template must define one top-level profile name (ADR-0012).",
        )
    return keys[0]


def _materialize_skeleton(
    bundle: "Bundle",
    warehouse_env: Mapping[str, str],
    skeleton_root: Path | None,
) -> Path:
    """Write a minimal dbt skeleton that hosts the bundle's manifest + profile.

    The skeleton carries NO models/seeds/SQL — it exists only to satisfy dbt's
    config loading + adapter registration. The semantic graph comes entirely
    from the bundled manifest.
    """
    import os
    import tempfile

    if bundle.profiles_template is None:
        raise CompilerBootstrapError(
            "bundle has no profiles.template.yml; cannot compose a runtime profile",
            detail="Publish a bundle that includes profiles.template.yml (ADR-0012).",
        )

    root = skeleton_root or Path(tempfile.mkdtemp(prefix="flowproxy_runtime_"))
    (root / "models").mkdir(parents=True, exist_ok=True)
    (root / "target").mkdir(parents=True, exist_ok=True)

    # The dbt_project.yml `profile:` must match the template's top-level key.
    profile_name = _profile_name_from_template(bundle.profiles_template)
    (root / "dbt_project.yml").write_text(
        _SKELETON_PROJECT_TEMPLATE.format(profile_name=profile_name), encoding="utf-8"
    )

    # Inject warehouse creds via env-var interpolation, exactly as dbt resolves
    # profiles.yml ({{ env_var('X') }}). Secrets live only in this process's env
    # and the skeleton on the runtime host — never in the bundle (ADR-0015).
    for key, value in warehouse_env.items():
        os.environ.setdefault(key, value)
    (root / "profiles.yml").write_text(bundle.profiles_template, encoding="utf-8")

    (root / "target" / "semantic_manifest.json").write_text(
        bundle.semantic_manifest_json, encoding="utf-8"
    )
    return root


@dataclass(frozen=True)
class DimensionInfo:
    """A group-by dimension with catalog metadata (WS3/MCP)."""

    name: str                       # qualified, grain-free: account__region, metric_time
    dimension_type: str             # categorical | time | unknown
    label: str | None = None
    description: str | None = None

    @property
    def is_time(self) -> bool:
        return self.dimension_type == "time"


@dataclass(frozen=True)
class MetricInfo:
    """A metric with catalog metadata (WS3/MCP)."""

    name: str
    metric_type: str                # simple | ratio | derived | cumulative | unknown
    label: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class CompiledResult:
    """Rows returned by MetricFlow's plan-and-execute path.

    ``columns`` are in MetricFlow's output order (dimensions then metrics),
    which may differ from the client's SELECT order; the network layer
    re-projects to the requested order before serializing.
    """

    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)


class SemanticCompiler:
    """Compiles metric/dimension requests into warehouse SQL via MetricFlow.

    Lifecycle:
        1. Parse + validate ``semantic_manifest.json`` (dbt-semantic-interfaces).
        2. Build the :class:`SemanticRegistry` used by the SQL interceptor.
        3. Bootstrap a ``MetricFlowEngine`` backed by the project's dbt adapter
           (skipped in ``dry_run`` mode).
    """

    def __init__(
        self,
        manifest_path: Path,
        dbt_project_dir: Path | None = None,
        *,
        dry_run: bool = False,
    ) -> None:
        self._manifest_path: Path = manifest_path
        self._dbt_project_dir: Path | None = dbt_project_dir
        self._dry_run: bool = dry_run

        _assert_compat_matrix()

        self._manifest: Any = self._load_manifest()
        self.registry: SemanticRegistry = SemanticRegistry.from_manifest(self._manifest)

        self._engine: Any | None = None
        self._request_cls: Any | None = None
        if not dry_run:
            self._engine, self._request_cls = self._build_engine()
            logger.info("MetricFlow engine online (manifest=%s)", manifest_path)
        else:
            logger.warning("compiler running in DRY-RUN mode: SQL is stubbed, not planned by MetricFlow")

    # ------------------------------------------------------------------ #
    # Bundle constructor (WS7d) — runtime consumer, no checked-out project
    # ------------------------------------------------------------------ #
    @classmethod
    def from_bundle(
        cls,
        bundle: "Bundle",
        *,
        warehouse_env: Mapping[str, str] | None = None,
        skeleton_root: Path | None = None,
        dry_run: bool = False,
    ) -> "SemanticCompiler":
        """Build a compiler from a deployment bundle — no checked-out dbt project.

        Composes a minimal synthetic dbt skeleton (``dbt_project.yml`` +
        ``profiles.yml`` from the bundle's ``profiles.template.yml`` with
        ``warehouse_env`` injected + the bundled ``semantic_manifest.json``),
        then bootstraps the engine from it (WS7 spike recipe, ADR-0012/0015).

        Warehouse SELECT-only creds are supplied via ``warehouse_env`` (from the
        enterprise secret store); they are written only into the skeleton's
        ``profiles.yml`` under ``skeleton_root``, never into the bundle.

        Adapter registration mutates global dbt state (WS7 finding); callers
        performing hot-swaps must serialize construction (ADR-0014).
        """
        root = _materialize_skeleton(bundle, warehouse_env or {}, skeleton_root)
        manifest_path = root / "target" / "semantic_manifest.json"
        compiler = cls(manifest_path, root, dry_run=dry_run)
        compiler._bundle_version = bundle.version
        logger.info("compiler built from bundle %s (skeleton=%s)", bundle.version, root)
        return compiler

    @property
    def is_dry_run(self) -> bool:
        return self._dry_run

    @property
    def bundle_version(self) -> str | None:
        """Version id of the bundle this compiler was built from (or None)."""
        return getattr(self, "_bundle_version", None)

    @property
    def manifest(self) -> Any:
        """The in-memory PydanticSemanticManifest (used by the catalog builder)."""
        return self._manifest

    # ------------------------------------------------------------------ #
    # Manifest loading
    # ------------------------------------------------------------------ #
    def _load_manifest(self) -> Any:
        """Read and validate ``target/semantic_manifest.json``."""
        if not self._manifest_path.is_file():
            raise ManifestNotFoundError(
                f"semantic manifest not found at {self._manifest_path}",
                detail="Run `dbt parse` in the dbt project to regenerate target/semantic_manifest.json.",
            )
        try:
            raw_text = self._manifest_path.read_text(encoding="utf-8")
            json.loads(raw_text)  # validate JSON shape early for a clear error
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestInvalidError(
                f"semantic manifest at {self._manifest_path} is unreadable: {exc}",
                detail="The file may be truncated; re-run `dbt parse`.",
            ) from exc

        # Parse via the SAME canonical path MetricFlow's engine uses (parse +
        # transform), so the registry and the engine see one identical manifest
        # object — no second parse, no drift (WS7 finding, ADR-0012).
        manifest = _parse_semantic_manifest(raw_text)
        logger.info(
            "manifest loaded (canonical parse): %d semantic models, %d metrics",
            len(manifest.semantic_models),
            len(manifest.metrics),
        )
        return manifest

    # ------------------------------------------------------------------ #
    # Engine bootstrap
    # ------------------------------------------------------------------ #
    def _build_engine(self) -> tuple[Any, Any]:
        """Construct ``MetricFlowEngine`` + the request class.

        MetricFlow has moved these classes across packages between releases
        (``metricflow`` -> ``metricflow_semantics`` split), so imports are
        layered oldest-compatible-last.
        """
        try:
            from metricflow.engine.metricflow_engine import (  # type: ignore[import-not-found]
                MetricFlowEngine,
                MetricFlowQueryRequest,
            )
        except ImportError as exc:
            raise CompilerBootstrapError(
                "the `metricflow` package is not importable",
                detail="pip install metricflow / dbt-metricflow (see requirements.txt).",
            ) from exc

        from metricflow_semantics.model.semantic_manifest_lookup import (  # type: ignore[import-not-found]
            SemanticManifestLookup,
        )

        # Build the adapter (SQL dialect renderer + warehouse connection) from
        # the project; the engine is built from OUR already-loaded, canonically
        # parsed self._manifest — the same object the registry uses. One parse,
        # zero drift (WS7 finding).
        sql_client = self._build_sql_client()
        lookup = SemanticManifestLookup(self._manifest)

        try:
            engine = MetricFlowEngine(semantic_manifest_lookup=lookup, sql_client=sql_client)
        except Exception as exc:
            raise CompilerBootstrapError(
                f"MetricFlowEngine failed to initialize: {exc}",
                detail="Verify the dbt adapter credentials in profiles.yml and that the manifest "
                "matches the installed MetricFlow version.",
            ) from exc
        return engine, MetricFlowQueryRequest

    def _build_sql_client(self) -> Any:
        """Build the dbt warehouse adapter and wrap it as a MetricFlow SqlClient.

        Mirrors what the ``mf`` / ``dbt sl`` CLI does internally: load the dbt
        project (``dbt_project.yml`` + ``profiles.yml``), materialize its
        adapter (DuckDB/Snowflake/Redshift/...), and wrap it in an
        ``AdapterBackedSqlClient`` so MetricFlow renders the correct dialect.

        Returns the ``AdapterBackedSqlClient``. (The manifest is loaded
        separately via the canonical parse — see :func:`_parse_semantic_manifest`.)
        """
        if self._dbt_project_dir is None:
            raise CompilerBootstrapError(
                "dbt_project_dir is required to build the warehouse SQL client",
                detail="Set FLOWPROXY_DBT_PROJECT_DIR, or start with FLOWPROXY_COMPILER_MODE=dryrun.",
            )
        try:
            from dbt_metricflow.cli.dbt_connectors.adapter_backed_client import (  # type: ignore[import-not-found]
                AdapterBackedSqlClient,
            )
            from dbt_metricflow.cli.dbt_connectors.dbt_config_accessor import (  # type: ignore[import-not-found]
                dbtProjectMetadata,
                get_adapter_by_type,
            )
        except ImportError as exc:
            raise CompilerBootstrapError(
                "dbt-metricflow connector layer is not importable",
                detail="pip install dbt-metricflow plus the warehouse adapter (dbt-duckdb / dbt-snowflake / dbt-redshift).",
            ) from exc

        project_dir = self._dbt_project_dir
        try:
            # load_from_paths runs `dbt debug`, which registers the adapter
            # (global-state side effect); get_adapter_by_type then returns it.
            project_metadata = dbtProjectMetadata.load_from_paths(
                profiles_path=project_dir, project_path=project_dir
            )
            adapter = get_adapter_by_type(project_metadata.profile.credentials.type)
            return AdapterBackedSqlClient(adapter)
        except Exception as exc:
            raise CompilerBootstrapError(
                f"failed to bootstrap the dbt warehouse adapter: {exc}",
                detail=f"Check dbt_project.yml/profiles.yml under {project_dir}.",
            ) from exc

    # ------------------------------------------------------------------ #
    # Compilation
    # ------------------------------------------------------------------ #
    def compile_request(self, metrics: list[str], dimensions: list[str]) -> str:
        """Compile a metric/dimension request into optimized warehouse SQL.

        Raises:
            UnknownFieldError:  a name is not defined in the manifest.
            QueryPlanningError: MetricFlow could not plan the query (most
                                commonly an unreachable dimension join path).
        """
        started = time.perf_counter()
        canonical_metrics, canonical_dims = self._validate(metrics, dimensions)
        logger.info(
            "compile_request: metrics=%s group_by=%s",
            canonical_metrics,
            canonical_dims,
        )

        if self._dry_run:
            sql = self._render_dry_run_sql(canonical_metrics, canonical_dims)
        else:
            sql = self._plan_with_metricflow(canonical_metrics, canonical_dims)

        logger.info(
            "compile_request: planned in %.1f ms (%d chars of SQL)",
            (time.perf_counter() - started) * 1000,
            len(sql),
        )
        logger.debug("compiled warehouse SQL:\n%s", sql)
        return sql

    # ------------------------------------------------------------------ #
    # Shared semantic-introspection API (WS2 error hints / WS3 catalog / MCP)
    # ------------------------------------------------------------------ #
    def valid_group_bys(self, metrics: Sequence[str]) -> list["DimensionInfo"]:
        """Dimensions that can legally group the given metrics.

        This is MetricFlow's linkable-elements resolution — the *reachable*
        dimensions across the entity join graph, not just locally-defined
        ones. Returns qualified names (``account__region``, ``metric_time``)
        with labels/descriptions/types for catalog metadata.

        Consumed by:
          * WS2 — "did you mean" hints when a group-by is unreachable;
          * WS3 — columns of each auto-cube in the virtual catalog;
          * MCP — the ``get_dimensions`` tool.

        In dry-run mode (no engine) this falls back to the registry's full
        dimension set, which is a superset — adequate for wire-path tests.
        """
        for m in metrics:
            if not self.registry.is_metric(m):
                raise UnknownFieldError(
                    f"unknown metric {m!r}",
                    detail=f"Available metrics: {sorted(self.registry.metrics)[:20]}",
                )

        if self._dry_run or self._engine is None:
            return [
                DimensionInfo(name=name, dimension_type="unknown")
                for name in sorted(self.registry.qualified_dimensions)
            ]

        try:
            dims = self._engine.simple_dimensions_for_metrics(list(metrics))
        except Exception as exc:
            raise self._planning_error(exc) from exc

        out: list[DimensionInfo] = []
        for d in dims:
            out.append(
                DimensionInfo(
                    name=d.granularity_free_dunder_name,
                    dimension_type=str(getattr(d.type, "value", d.type)).lower(),
                    label=getattr(d, "label", None),
                    description=getattr(d, "description", None),
                )
            )
        out.sort(key=lambda di: di.name)
        return out

    def metric_catalog(self) -> list["MetricInfo"]:
        """All metrics with label/type/description for catalog + MCP list_metrics."""
        if self._dry_run or self._engine is None:
            return [MetricInfo(name=n, metric_type="unknown") for n in sorted(self.registry.metrics)]
        out: list[MetricInfo] = []
        for m in self._engine.list_metrics(include_dimensions=False):
            out.append(
                MetricInfo(
                    name=m.name,
                    metric_type=str(getattr(m.type, "value", m.type)).lower(),
                    label=getattr(m, "label", None),
                    description=getattr(m, "description", None),
                )
            )
        out.sort(key=lambda mi: mi.name)
        return out

    def execute_request(
        self,
        metrics: list[str],
        dimensions: list[str],
        *,
        where_constraints: Sequence[str] | None = None,
        time_constraint_start: datetime | None = None,
        time_constraint_end: datetime | None = None,
        order_by: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> "CompiledResult":
        """Plan AND execute a request, returning warehouse rows.

        MetricFlow's ``.query()`` renders dialect SQL and runs it through the
        dbt adapter in one step, so on the real path the compiler owns
        execution — there is no separate executor hop and thus no risk of the
        proxy re-running raw SQL that bypasses the semantic guardrails.

        Returns column names (in MetricFlow's output order) plus row tuples.
        In dry-run mode this raises, since there is no warehouse to hit — use
        the mock executor path in the server for dry-run.
        """
        if self._dry_run:
            raise CompilerBootstrapError(
                "execute_request is unavailable in dry-run mode",
                detail="Dry-run has no warehouse; the server routes dry-run through the mock executor.",
            )
        assert self._engine is not None

        canonical_metrics, canonical_dims = self._validate(metrics, dimensions)
        started = time.perf_counter()
        logger.info(
            "execute_request: metrics=%s group_by=%s where=%s time=[%s,%s] order_by=%s limit=%s",
            canonical_metrics,
            canonical_dims,
            list(where_constraints or []),
            time_constraint_start,
            time_constraint_end,
            list(order_by or []),
            limit,
        )
        try:
            request = self._build_request(
                canonical_metrics,
                canonical_dims,
                where_constraints=where_constraints,
                time_constraint_start=time_constraint_start,
                time_constraint_end=time_constraint_end,
                order_by=order_by,
                limit=limit,
            )
            query_result = self._engine.query(mf_request=request)
        except Exception as exc:
            raise self._planning_error(exc) from exc

        table = query_result.result_df
        columns: list[str] = list(table.column_names)
        n_cols = table.column_count
        rows: list[tuple[Any, ...]] = [
            tuple(table.get_cell_value(r, c) for c in range(n_cols))
            for r in range(table.row_count)
        ]
        logger.info(
            "execute_request: %d rows x %d cols in %.1f ms",
            len(rows),
            len(columns),
            (time.perf_counter() - started) * 1000,
        )
        return CompiledResult(columns=columns, rows=rows)

    def _validate(
        self, metrics: Sequence[str], dimensions: Sequence[str]
    ) -> tuple[list[str], list[str]]:
        if not metrics:
            raise UnknownFieldError(
                "query selects no known metrics",
                detail=f"Available metrics: {sorted(self.registry.metrics)[:20]}",
            )

        for metric in metrics:
            if not self.registry.is_metric(metric):
                close = self.registry.suggestions(metric)
                raise UnknownFieldError(
                    f"unknown metric {metric!r}",
                    detail=f"Did you mean: {close}?" if close else "Check the MetricFlow YAML metric definitions.",
                )

        resolved_dims: list[str] = []
        for dim in dimensions:
            resolved = self.registry.resolve_dimension(dim)
            if resolved is None:
                close = self.registry.suggestions(dim)
                raise UnknownFieldError(
                    f"unknown dimension {dim!r}",
                    detail=f"Did you mean: {close}?" if close else "Check the semantic model dimension definitions.",
                )
            resolved_dims.append(resolved)
        return list(metrics), resolved_dims

    def _build_request(
        self,
        metrics: list[str],
        dimensions: list[str],
        *,
        where_constraints: Sequence[str] | None = None,
        time_constraint_start: "datetime | None" = None,
        time_constraint_end: "datetime | None" = None,
        order_by: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> Any:
        """Construct a ``MetricFlowQueryRequest`` (real-API ``create``)."""
        assert self._request_cls is not None
        return self._request_cls.create(
            metric_names=tuple(metrics),
            group_by_names=tuple(dimensions),
            where_constraints=tuple(where_constraints) if where_constraints else None,
            time_constraint_start=time_constraint_start,
            time_constraint_end=time_constraint_end,
            order_by_names=tuple(order_by) if order_by else None,
            limit=limit,
        )

    def _plan_with_metricflow(self, metrics: list[str], dimensions: list[str]) -> str:
        assert self._engine is not None
        try:
            request = self._build_request(metrics, dimensions)
            explain_result = self._engine.explain(mf_request=request)
            # MetricFlow 0.211: explain result exposes sql_statement.sql
            # (plan-only — no warehouse connection required).
            sql: str = explain_result.sql_statement.sql
        except Exception as exc:
            raise self._planning_error(exc) from exc

        if not sql or not sql.strip():
            raise QueryPlanningError(
                "MetricFlow returned an empty SQL plan",
                detail="This usually indicates a manifest/engine version mismatch.",
            )
        return sql

    # ------------------------------------------------------------------ #
    # Plan-only validation (WS7b CI gate) — no warehouse execution
    # ------------------------------------------------------------------ #
    def saved_query_names(self) -> list[str]:
        return [sq.name for sq in getattr(self._manifest, "saved_queries", None) or []]

    def plan_only(
        self,
        metrics: list[str],
        dimensions: list[str],
        *,
        where_constraints: Sequence[str] | None = None,
    ) -> str:
        """Plan a request to SQL WITHOUT executing (offline CI validation).

        Uses MetricFlow ``explain``; a planning failure (broken join path,
        missing metric, renamed dimension) raises ``QueryPlanningError``.
        """
        assert self._engine is not None, "plan_only requires a real engine (not dry-run)"
        try:
            request = self._build_request(
                metrics, dimensions, where_constraints=where_constraints
            )
            explain_result = self._engine.explain(mf_request=request)
            return explain_result.sql_statement.sql
        except Exception as exc:
            raise self._planning_error(exc) from exc

    def plan_saved_query(self, name: str) -> str:
        """Plan a saved query by name (offline). Raises on any planning failure."""
        assert self._engine is not None and self._request_cls is not None
        try:
            request = self._request_cls.create(saved_query_name=name)
            explain_result = self._engine.explain(mf_request=request)
            return explain_result.sql_statement.sql
        except Exception as exc:
            raise self._planning_error(exc) from exc

    @staticmethod
    def _planning_error(exc: Exception) -> QueryPlanningError:
        message = str(exc)
        join_hint = "join" in message.lower() or "linkable" in message.lower()
        return QueryPlanningError(
            f"MetricFlow could not plan the query: {message}",
            detail=(
                "The requested dimensions are not reachable from the metrics' semantic "
                "models — verify entity keys and join paths in the MetricFlow YAML."
                if join_hint
                else "Enable DEBUG logging for the full MetricFlow planner trace."
            ),
        )

    @staticmethod
    def _render_dry_run_sql(metrics: list[str], dimensions: list[str]) -> str:
        """Deterministic placeholder SQL for wire-path integration tests."""
        select_cols = ", ".join([*dimensions, *metrics]) or "1"
        group_by = f"\nGROUP BY {', '.join(str(i + 1) for i in range(len(dimensions)))}" if dimensions else ""
        return textwrap.dedent(
            f"""\
            -- flowproxy dry-run plan (MetricFlow engine bypassed)
            SELECT {select_cols}
            FROM <semantic_layer>{group_by}
            """
        )
