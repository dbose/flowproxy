"""Module 2 — SQL extraction interceptor.

QuickSight speaks plain PostgreSQL to us, e.g.::

    SELECT "<metric>", "<dimension>" FROM "<cube>" GROUP BY 2

This module parses that dialect with ``sqlglot``, identifies the virtual
cube in the FROM clause, unwraps each projection down to its column token
(through aliases, casts and aggregate wrappers QuickSight likes to add),
and classifies every token as a MetricFlow *metric* or *dimension* using
the boot-time :class:`~engine.registry.SemanticRegistry`.

It also classifies non-data traffic (driver handshake noise such as
``SET extra_float_digits = 3`` or pg_catalog introspection) so the network
layer can answer those locally instead of invoking MetricFlow.
"""

from __future__ import annotations

import datetime as dt
import enum
import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import expressions as exp

from engine.exceptions import UnknownFieldError, UnsupportedQueryError
from engine.filters import DEFAULT_MAX_ROWS, ClauseTranslator
from engine.registry import SemanticRegistry

logger = logging.getLogger("flowproxy.engine.parser")


class QueryKind(enum.Enum):
    """Coarse routing decision for an inbound SQL statement."""

    DATA = "data"              # semantic query -> MetricFlow
    SET = "set"                # SET / RESET session parameters
    SHOW = "show"              # SHOW <guc>
    TRANSACTION = "transaction"  # BEGIN / COMMIT / ROLLBACK
    CATALOG = "catalog"        # pg_catalog / information_schema introspection
    SCALAR = "scalar"          # SELECT version(), SELECT 1, current_schema()...
    EMPTY = "empty"


@dataclass(frozen=True)
class ExtractedQuery:
    """The semantic payload distilled from a raw PostgreSQL query string."""

    cube: str
    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    # WS2: filters/order/limit translated into MetricFlow constructs.
    where_constraints: list[str] = field(default_factory=list)
    time_constraint_start: "dt.datetime | None" = None
    time_constraint_end: "dt.datetime | None" = None
    order_by: list[str] = field(default_factory=list)
    limit: int | None = None

    @property
    def projection(self) -> list[str]:
        """Output column order as the client SELECTed it."""
        return [*self.dimensions, *self.metrics]


# Aggregate wrappers BI tools commonly emit around measures. We unwrap them:
# aggregation semantics live in the MetricFlow metric definition, not the SQL.
_AGG_NODES: tuple[type[exp.Expression], ...] = (
    exp.Sum,
    exp.Avg,
    exp.Min,
    exp.Max,
    exp.Count,
    exp.AnyValue,
)

_CATALOG_MARKERS: tuple[str, ...] = (
    "pg_catalog",
    "information_schema",
    "pg_type",
    "pg_class",
    "pg_namespace",
    "pg_attribute",
    "pg_settings",
    "pg_database",
)


class SQLExtractor:
    """Stateless-per-query extractor bound to one semantic registry."""

    def __init__(self, registry: SemanticRegistry, *, max_rows: int = DEFAULT_MAX_ROWS) -> None:
        self._registry: SemanticRegistry = registry
        self._translator = ClauseTranslator(registry, max_rows=max_rows)

    # ------------------------------------------------------------------ #
    # Classification
    # ------------------------------------------------------------------ #
    def classify(self, raw_sql: str) -> QueryKind:
        stripped = raw_sql.strip().rstrip(";").strip()
        if not stripped:
            return QueryKind.EMPTY

        lowered = stripped.lower()
        first_word = lowered.split(None, 1)[0]

        if first_word in {"set", "reset"}:
            return QueryKind.SET
        if first_word == "show":
            return QueryKind.SHOW
        if first_word in {"begin", "start", "commit", "rollback", "end", "abort", "discard"}:
            return QueryKind.TRANSACTION
        if any(marker in lowered for marker in _CATALOG_MARKERS):
            return QueryKind.CATALOG
        if first_word == "select" and " from " not in f" {lowered} ":
            # SELECT version(), SELECT current_schema(), SELECT 1 — no table.
            return QueryKind.SCALAR
        return QueryKind.DATA

    # ------------------------------------------------------------------ #
    # Extraction
    # ------------------------------------------------------------------ #
    def extract(self, raw_sql: str) -> ExtractedQuery:
        """Distill ``(metrics, dimensions)`` from a raw PostgreSQL query.

        Raises:
            UnsupportedQueryError: unparseable SQL or a shape (star select,
                joins, subqueries, non-SELECT) that has no semantic mapping.
            UnknownFieldError: a column that is neither metric nor dimension.
        """
        logger.debug("extract: raw sql=%r", raw_sql)
        try:
            tree = sqlglot.parse_one(raw_sql, read="postgres")
        except sqlglot.errors.ParseError as exc:
            raise UnsupportedQueryError(
                f"could not parse SQL: {exc}",
                detail="FlowProxy accepts single SELECT statements over one virtual cube.",
            ) from exc

        if not isinstance(tree, exp.Select):
            raise UnsupportedQueryError(
                f"only SELECT statements are supported (got {type(tree).__name__})",
            )

        cube = self._extract_cube(tree)
        metrics, dimensions = self._classify_projections(tree)

        # A semantic query must request at least one metric. QuickSight (and
        # other BI tools) can emit COUNT(<dimension>) when a visual has no
        # measure - our parser correctly unwraps the COUNT (aggregation lives in
        # the metric definition, not the SQL), leaving zero metrics. MetricFlow
        # cannot plan a metric-less query, so guide the analyst rather than
        # emit an opaque SQL exception. Matches dbt Cloud's behavior.
        if not metrics:
            available = sorted(self._registry.metrics)
            hint = ", ".join(available[:8]) + ("..." if len(available) > 8 else "")
            raise UnsupportedQueryError(
                "this visual has no metric - the semantic layer answers metrics, "
                "not counts of dimensions",
                detail=(
                    f"Add a metric to the visual's Value field (available: {hint}). "
                    "COUNT of a dimension is not supported; define a count metric in "
                    "the semantic model if you need one."
                ),
            )

        clauses = self._translator.translate(tree)  # WS2: WHERE/ORDER/LIMIT

        result = ExtractedQuery(
            cube=cube,
            metrics=metrics,
            dimensions=dimensions,
            where_constraints=clauses.where_constraints,
            time_constraint_start=clauses.time_constraint_start,
            time_constraint_end=clauses.time_constraint_end,
            order_by=clauses.order_by,
            limit=clauses.limit,
        )
        logger.info(
            "extract: cube=%r metrics=%s dimensions=%s where=%s time=[%s,%s] order_by=%s limit=%s",
            result.cube,
            result.metrics,
            result.dimensions,
            result.where_constraints,
            result.time_constraint_start,
            result.time_constraint_end,
            result.order_by,
            result.limit,
        )
        return result

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_cube(tree: exp.Select) -> str:
        tables = list(tree.find_all(exp.Table))
        if not tables:
            raise UnsupportedQueryError("query has no FROM clause; cannot identify a cube")
        if len(tables) > 1:
            raise UnsupportedQueryError(
                f"multi-table queries are not supported (saw {[t.name for t in tables]})",
                detail="Joins are resolved by MetricFlow from the semantic graph — "
                "query a single virtual cube instead.",
            )
        return tables[0].name  # sqlglot strips the double quotes

    def _classify_projections(self, tree: exp.Select) -> tuple[list[str], list[str]]:
        metrics: list[str] = []
        dimensions: list[str] = []

        for projection in tree.expressions:
            token = self._unwrap(projection)
            if token is None:
                continue  # literal constants contribute nothing semantic

            name, grain = token
            if grain is None and self._registry.is_metric(name):
                if name not in metrics:
                    metrics.append(name)
                continue

            lookup_name = f"{name}__{grain}" if grain else name
            resolved = self._registry.resolve_dimension(lookup_name)
            if resolved is not None:
                if resolved not in dimensions:
                    dimensions.append(resolved)
                continue

            close = self._registry.suggestions(name)
            raise UnknownFieldError(
                f"column {name!r} is neither a metric nor a dimension in the semantic manifest",
                detail=f"Did you mean: {close}?" if close else "Check the MetricFlow YAML definitions.",
            )

        if not metrics and not dimensions:
            raise UnsupportedQueryError("query projects no semantic fields")
        return metrics, dimensions

    def _unwrap(self, node: exp.Expression) -> tuple[str, str | None] | None:
        """Peel a projection down to ``(column_name, optional_time_grain)``.

        Handles: aliases, casts, aggregate wrappers, DATE_TRUNC. Returns
        ``None`` for pure literals; raises for stars and expressions with
        no single underlying column.
        """
        grain: str | None = None

        while True:
            if isinstance(node, exp.Alias):
                node = node.this
            elif isinstance(node, exp.Cast):
                node = node.this
            elif isinstance(node, exp.Paren):
                node = node.this
            elif isinstance(node, _AGG_NODES):
                inner = node.this
                if inner is None or isinstance(inner, exp.Star):
                    # COUNT(*) has no column token — cannot map to a metric name.
                    raise UnsupportedQueryError(
                        "bare COUNT(*) cannot be mapped; select a named metric instead",
                    )
                node = inner
            elif isinstance(node, exp.DateTrunc):
                unit = node.unit
                grain = (unit.name if isinstance(unit, exp.Expression) else str(unit)).lower()
                node = node.this
            else:
                break

        if isinstance(node, exp.Column):
            return node.name, grain
        if isinstance(node, exp.Star):
            raise UnsupportedQueryError(
                "SELECT * is not supported on virtual cubes",
                detail="Enumerate the metrics and dimensions explicitly.",
            )
        if isinstance(node, exp.Literal):
            logger.debug("extract: skipping literal projection %r", node.sql())
            return None
        raise UnsupportedQueryError(
            f"cannot map expression {node.sql(dialect='postgres')!r} onto the semantic layer",
            detail="Only plain columns, aggregates of columns, and DATE_TRUNC are supported.",
        )
