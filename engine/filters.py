"""Module (WS2) — WHERE / ORDER BY / LIMIT translation.

Turns sqlglot-parsed clauses from a QuickSight / Power BI query into MetricFlow
query-request constructs — **never** into raw SQL strings spliced with user
input (ADR-0005). Every literal is type-validated against the target
dimension and rendered through a safe quoter; every predicate shape outside
the supported grammar is rejected (deny-by-default) rather than silently
dropped, which would return over-broad data.

Output contract (:class:`TranslatedClauses`):
  * ``where_constraints``     -> MetricFlow ``where_constraints`` (templated)
  * ``time_constraint_start`` / ``time_constraint_end`` -> MetricFlow time window
  * ``order_by``              -> MetricFlow ``order_by_names``
  * ``limit``                 -> MetricFlow ``limit`` (also clamped server-side)
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Sequence

from sqlglot import expressions as exp

from engine.exceptions import UnsupportedQueryError
from engine.registry import TIME_GRAINS, SemanticRegistry

logger = logging.getLogger("flowproxy.engine.filters")

# Server-side hard cap on returned rows regardless of client LIMIT (ADR-0005).
DEFAULT_MAX_ROWS: int = 1_000_000


@dataclass(frozen=True)
class TranslatedClauses:
    """MetricFlow-native representation of WHERE/ORDER/LIMIT."""

    where_constraints: list[str] = field(default_factory=list)
    time_constraint_start: dt.datetime | None = None
    time_constraint_end: dt.datetime | None = None
    order_by: list[str] = field(default_factory=list)
    limit: int | None = None


class ClauseTranslator:
    """Translates sqlglot WHERE/ORDER/LIMIT into MetricFlow constructs."""

    def __init__(self, registry: SemanticRegistry, *, max_rows: int = DEFAULT_MAX_ROWS) -> None:
        self._registry = registry
        self._max_rows = max_rows

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    def translate(self, select: exp.Select) -> TranslatedClauses:
        where = self._translate_where(select.args.get("where"))
        order_by = self._translate_order(select.args.get("order"), select)
        limit = self._translate_limit(select.args.get("limit"))

        result = TranslatedClauses(
            where_constraints=where.where_constraints,
            time_constraint_start=where.time_constraint_start,
            time_constraint_end=where.time_constraint_end,
            order_by=order_by,
            limit=limit,
        )
        logger.info(
            "translated clauses: where=%s time=[%s,%s] order_by=%s limit=%s",
            result.where_constraints,
            result.time_constraint_start,
            result.time_constraint_end,
            result.order_by,
            result.limit,
        )
        return result

    # ------------------------------------------------------------------ #
    # WHERE
    # ------------------------------------------------------------------ #
    def _translate_where(self, where: exp.Where | None) -> TranslatedClauses:
        if where is None:
            return TranslatedClauses()

        constraints: list[str] = []
        time_lo: dt.datetime | None = None
        time_hi: dt.datetime | None = None

        # Only a conjunction (AND-tree) of simple predicates is supported. An
        # OR at the top level mixes rows in ways MetricFlow filters can't
        # express safely, so we reject it rather than approximate.
        for predicate in self._split_and(where.this):
            for kind, payload in self._translate_predicate(predicate):
                if kind == "where":
                    constraints.append(payload)  # type: ignore[arg-type]
                elif kind == "time_lo":
                    time_lo = self._max_dt(time_lo, payload)  # type: ignore[arg-type]
                elif kind == "time_hi":
                    time_hi = self._min_dt(time_hi, payload)  # type: ignore[arg-type]

        return TranslatedClauses(
            where_constraints=constraints,
            time_constraint_start=time_lo,
            time_constraint_end=time_hi,
        )

    def _split_and(self, node: exp.Expression) -> list[exp.Expression]:
        if isinstance(node, exp.And):
            return self._split_and(node.left) + self._split_and(node.right)
        if isinstance(node, exp.Paren):
            return self._split_and(node.this)
        if isinstance(node, exp.Or):
            raise UnsupportedQueryError(
                "OR conditions in WHERE are not supported by the semantic proxy",
                detail="Split the query, or express the filter as an IN-list on one dimension.",
            )
        return [node]

    def _translate_predicate(self, node: exp.Expression) -> list[tuple[str, object]]:
        """Map one predicate to a list of (kind, payload) contributions.

        kind ∈ {'where' -> template str, 'time_lo'/'time_hi' -> datetime}.
        BETWEEN on a time dimension yields two bounds; hence a list.
        """
        # BETWEEN → two time bounds (only supported on time dimensions).
        if isinstance(node, exp.Between):
            col, grain = self._column_of(node.this)
            resolved, is_time = self._resolve(col, grain)
            if not is_time:
                raise UnsupportedQueryError(
                    f"BETWEEN is only supported on time dimensions, not {col!r}",
                )
            lo = self._as_datetime(node.args["low"])
            hi = self._as_datetime(node.args["high"])
            return [("time_lo", lo), ("time_hi", hi)]

        if not isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Like)):
            raise UnsupportedQueryError(
                f"unsupported filter predicate: {node.sql(dialect='postgres')!r}",
                detail="Supported: =, <>, <, <=, >, >=, IN, LIKE, BETWEEN on a single dimension.",
            )

        col, grain = self._column_of(node.this)
        resolved, is_time = self._resolve(col, grain)

        # Time comparisons with an inequality become time constraints, which
        # MetricFlow optimizes (partition pruning) better than a where filter.
        if is_time and isinstance(node, (exp.GT, exp.GTE, exp.LT, exp.LTE)):
            when = self._as_datetime(node.expression)
            return [("time_lo" if isinstance(node, (exp.GT, exp.GTE)) else "time_hi", when)]

        return [("where", self._render_where(node, resolved, is_time, grain))]

    # ------------------------------------------------------------------ #
    # Rendering a single categorical/time equality/IN/LIKE predicate
    # ------------------------------------------------------------------ #
    def _render_where(self, node: exp.Expression, resolved: str, is_time: bool, grain: str | None) -> str:
        ref = self._dimension_ref(resolved, is_time, grain)

        if isinstance(node, exp.In):
            values = [self._quote(self._literal_value(e)) for e in node.expressions]
            if not values:
                raise UnsupportedQueryError("empty IN list")
            return f"{ref} IN ({', '.join(values)})"

        op = {
            exp.EQ: "=", exp.NEQ: "<>", exp.GT: ">", exp.GTE: ">=",
            exp.LT: "<", exp.LTE: "<=", exp.Like: "LIKE",
        }[type(node)]
        value = self._quote(self._literal_value(node.expression))
        return f"{ref} {op} {value}"

    @staticmethod
    def _dimension_ref(resolved: str, is_time: bool, grain: str | None) -> str:
        """Build the MetricFlow Jinja reference for a dimension in a where clause."""
        if is_time:
            return f"{{{{ TimeDimension('{resolved}', '{grain or 'day'}') }}}}"
        return f"{{{{ Dimension('{resolved}') }}}}"

    # ------------------------------------------------------------------ #
    # ORDER BY / LIMIT
    # ------------------------------------------------------------------ #
    def _translate_order(self, order: exp.Order | None, select: exp.Select) -> list[str]:
        if order is None:
            return []
        out: list[str] = []
        for item in order.expressions:
            assert isinstance(item, exp.Ordered)
            target = item.this
            # ORDER BY <ordinal> → resolve against the projection.
            if isinstance(target, exp.Literal) and target.is_int:
                idx = int(target.name) - 1
                projections = select.expressions
                if not 0 <= idx < len(projections):
                    raise UnsupportedQueryError(f"ORDER BY position {idx + 1} out of range")
                col, grain = self._column_of(projections[idx])
            else:
                col, grain = self._column_of(target)
            resolved = self._resolve_order_target(col, grain)
            out.append(f"-{resolved}" if item.args.get("desc") else resolved)
        return out

    def _resolve_order_target(self, col: str, grain: str | None) -> str:
        """ORDER BY may reference a metric OR a dimension."""
        if self._registry.is_metric(col):
            return col
        return self._resolve_name(col, grain)

    def _translate_limit(self, limit: exp.Limit | None) -> int | None:
        if limit is None:
            return self._max_rows if self._max_rows else None
        expr = limit.expression
        if not isinstance(expr, exp.Literal) or not expr.is_int:
            raise UnsupportedQueryError("LIMIT must be an integer literal")
        n = int(expr.name)
        if n < 0:
            raise UnsupportedQueryError("LIMIT must be non-negative")
        return min(n, self._max_rows)

    # ------------------------------------------------------------------ #
    # Column / literal helpers
    # ------------------------------------------------------------------ #
    def _column_of(self, node: exp.Expression) -> tuple[str, str | None]:
        """Peel casts/DATE_TRUNC to a (column_name, grain) pair."""
        grain: str | None = None
        while True:
            if isinstance(node, (exp.Paren, exp.Cast)):
                node = node.this
            elif isinstance(node, exp.DateTrunc):
                unit = node.unit
                grain = (unit.name if isinstance(unit, exp.Expression) else str(unit)).lower()
                node = node.this
            else:
                break
        if isinstance(node, exp.Column):
            base, sep, tail = node.name.rpartition("__")
            if sep and tail.lower() in TIME_GRAINS:
                return base, tail.lower()
            return node.name, grain
        raise UnsupportedQueryError(
            f"filter/order target must be a column, got {node.sql(dialect='postgres')!r}",
        )

    def _resolve(self, col: str, grain: str | None) -> tuple[str, bool]:
        """Resolve a column to (grain-free qualified name, is_time).

        The registry is authoritative about which dimensions are time — no
        name-suffix guessing.
        """
        resolved = self._registry.resolve_dimension(f"{col}__{grain}" if grain else col)
        if resolved is None:
            raise UnsupportedQueryError(
                f"cannot filter/order by {col!r}: not a known dimension",
                detail="Filters must reference a dimension exposed by the semantic layer.",
            )
        # Strip any grain suffix to the base qualified name for the ref/type.
        base, sep, tail = resolved.rpartition("__")
        grain_free = base if sep and tail in TIME_GRAINS else resolved
        return grain_free, self._registry.is_time_dimension(grain_free)

    def _resolve_name(self, col: str, grain: str | None) -> str:
        resolved, _ = self._resolve(col, grain)
        return resolved

    @staticmethod
    def _literal_value(node: exp.Expression) -> str | int | float:
        if isinstance(node, exp.Literal):
            if node.is_string:
                return node.this
            return float(node.this) if "." in node.this else int(node.this)
        if isinstance(node, exp.Boolean):
            return str(node.this)
        raise UnsupportedQueryError(
            f"filter value must be a literal, got {node.sql(dialect='postgres')!r}",
            detail="Bound parameters and expressions are not supported in filters.",
        )

    def _as_datetime(self, node: exp.Expression) -> dt.datetime:
        raw = self._literal_value(node)
        if not isinstance(raw, str):
            raise UnsupportedQueryError("time bound must be a date/timestamp literal")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return dt.datetime.strptime(raw, fmt)
            except ValueError:
                continue
        raise UnsupportedQueryError(f"unrecognized date/timestamp literal {raw!r}")

    @staticmethod
    def _quote(value: str | int | float) -> str:
        """Safely render a literal for a MetricFlow where template.

        Strings are single-quoted with internal quotes doubled; numbers pass
        through as digits. This is the only place user literal text enters the
        query, and it is validated + escaped — no raw splicing (ADR-0005).
        """
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return str(value)
        escaped = value.replace("'", "''")
        if len(escaped) > 512:
            raise UnsupportedQueryError("filter literal exceeds 512 characters")
        return f"'{escaped}'"

    @staticmethod
    def _max_dt(a: dt.datetime | None, b: dt.datetime) -> dt.datetime:
        return b if a is None else max(a, b)

    @staticmethod
    def _min_dt(a: dt.datetime | None, b: dt.datetime) -> dt.datetime:
        return b if a is None else min(a, b)
