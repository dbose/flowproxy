"""WS3 — catalog introspection answering (pg_catalog / information_schema).

Renders the :class:`~engine.catalog.VirtualCatalog` into the result sets BI
drivers expect when they discover schema. QuickSight's PostgreSQL JDBC driver
enumerates tables and columns through ``information_schema.tables`` /
``information_schema.columns`` and a handful of ``pg_catalog`` probes; those are
implemented here so the analyst sees the semantic cubes in the datasource pane.

Scope note (per current priority): the Npgsql/Power BI ``pg_type`` composite
bootstrap is intentionally a documented stub — it returns an empty, well-formed
set and logs a WARN, to be filled from a captured corpus when Power BI is
qualified. QuickSight's discovery path does not depend on it.

The matcher is deliberately pattern-based, not a full SQL catalog engine: it
recognizes the specific introspection shapes real drivers emit and answers
them; anything unrecognized returns an empty set with a WARN, which drivers
tolerate (they proceed to the next probe).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

import sqlglot
from sqlglot import expressions as exp

from engine.catalog import CATALOG_SCHEMA, CatalogColumn, CatalogTable, VirtualCatalog

logger = logging.getLogger("flowproxy.network.catalog")


@dataclass(frozen=True)
class CatalogResult:
    """A catalog answer, in the same column/rows shape the wire serializer uses."""

    columns: list[str]
    rows: list[tuple[Any, ...]]
    handled: bool = True

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class RowSet:
    """Underlying catalog rows keyed by REAL pg_catalog column names.

    Responders build a RowSet in the natural catalog vocabulary (``nspname``,
    ``relname``, ``oid`` ...). The projection layer then maps the client's
    SELECT list onto it, so an aliased probe like ``SELECT nspname AS
    schema_name`` gets back a single column literally named ``schema_name`` -
    which is what a JDBC driver's ``getString("schema_name")`` needs. Without
    this, drivers that read columns by name (QuickSight, most JDBC) find no
    matching column and report zero schemas/tables (SQLSTATE 02000).
    """

    columns: list[str]                    # real catalog column names
    rows: list[tuple[Any, ...]]

    def value(self, row: tuple[Any, ...], col: str) -> Any:
        try:
            return row[self.columns.index(col)]
        except ValueError:
            return None


def project(rowset: RowSet, sql: str) -> CatalogResult:
    """Map a RowSet onto the client's SELECT projection (names + aliases).

    Returns columns named exactly as the client asked (alias if given, else the
    source column). ``SELECT *`` or an unparseable projection falls back to the
    RowSet's own column order. Expressions that reference an unknown catalog
    column resolve to NULL rather than failing - drivers tolerate NULLs but not
    missing columns.
    """
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.ParseError:
        tree = None

    if not isinstance(tree, exp.Select) or any(
        isinstance(e, exp.Star) for e in tree.expressions
    ) or not tree.expressions:
        # No usable projection: return the raw shape.
        return CatalogResult(columns=list(rowset.columns), rows=list(rowset.rows))

    # Build (output_name, source_column | None) for each projected item.
    rowset_cols_lower = {c.lower(): c for c in rowset.columns}
    plan: list[tuple[str, str | None]] = []
    for e in tree.expressions:
        output_name, source = _projected_column(e)
        # Resolve the source against the RowSet, case-insensitively. For a
        # non-column projection (CASE/NULL/func) whose source is None, fall back
        # to matching the OUTPUT alias against a RowSet column - responders
        # precompute driver-shaped columns (table_type, table_cat, ...) named
        # after the alias, so e.g. "CASE ... END AS TABLE_TYPE" resolves to the
        # precomputed table_type column instead of NULL.
        resolved: str | None = None
        if source is not None and source.lower() in rowset_cols_lower:
            resolved = rowset_cols_lower[source.lower()]
        elif output_name.lower() in rowset_cols_lower:
            resolved = rowset_cols_lower[output_name.lower()]
        plan.append((output_name, resolved))

    out_cols = [name for name, _ in plan]
    out_rows: list[tuple[Any, ...]] = []
    for row in rowset.rows:
        out_rows.append(tuple(
            rowset.value(row, src) if src is not None else None
            for _, src in plan
        ))
    return CatalogResult(columns=out_cols, rows=out_rows)


def _projected_column(node: exp.Expression) -> tuple[str, str | None]:
    """Return (output_name, underlying_catalog_column | None) for a SELECT item."""
    if isinstance(node, exp.Alias):
        inner = node.this
        src = inner.name if isinstance(inner, exp.Column) else None
        return node.alias, src
    if isinstance(node, exp.Column):
        return node.name, node.name
    # A literal or function projection: name it by its SQL text, value NULL.
    return node.alias_or_name or node.sql(dialect="postgres"), None


# Type OID → information_schema data_type name (what drivers read for coercion).
_OID_TO_SQL_TYPE: dict[int, str] = {
    20: "bigint",
    701: "double precision",
    1700: "numeric",
    1043: "character varying",
    1082: "date",
    1114: "timestamp without time zone",
}
_OID_TO_UDT: dict[int, str] = {
    20: "int8", 701: "float8", 1700: "numeric",
    1043: "varchar", 1082: "date", 1114: "timestamp",
}
# OID -> java.sql.Types int, for JDBC getColumns()'s DATA_TYPE column.
# (BIGINT=-5, DOUBLE=8, NUMERIC=2, VARCHAR=12, DATE=91, TIMESTAMP=93)
_OID_TO_JDBC_TYPE: dict[int, int] = {
    20: -5, 701: 8, 1700: 2, 1043: 12, 1082: 91, 1114: 93,
}

# Real PostgreSQL defaults for the GUCs a JDBC driver probes via pg_settings
# during metadata calls. max_index_keys is the one QuickSight blocks on; the
# rest are here so we answer the whole family, not one at a time.
_PG_SETTINGS_DEFAULTS: dict[str, str] = {
    "max_index_keys": "32",
    "max_identifier_length": "63",
    "block_size": "8192",
    "integer_datetimes": "on",
    "standard_conforming_strings": "on",
    "server_version": "15.4",
    "server_encoding": "UTF8",
    "client_encoding": "UTF8",
    "DateStyle": "ISO, MDY",
    "TimeZone": "UTC",
    "max_function_args": "100",
}


# Column names the pg_attribute RowSet carries. Covers three getColumns
# variants a JDBC driver may emit, so `project()` (including SELECT * over the
# subquery) resolves whatever the query names:
#   1. raw pg_attribute / pg_class / pg_namespace / pg_type columns the real
#      windowed getColumns projects (nspname, relname, attname, attidentity, ...)
#   2. the simpler getColumns output aliases (table_schem, column_name, ...)
_PG_ATTRIBUTE_COLS: list[str] = [
    # raw catalog columns the windowed getColumns() subquery selects
    "nspname", "relname", "attrelid", "attname", "atttypid", "attnotnull",
    "atttypmod", "attlen", "typtypmod", "attnum", "attidentity", "attgenerated",
    "adsrc", "description", "typbasetype", "typtype", "attisdropped",
    # simpler getColumns output aliases
    "table_cat", "table_schem", "table_name", "column_name",
    "data_type", "type_name", "column_size", "nullable", "is_nullable",
    "ordinal_position", "remarks",
]


class CatalogResponder:
    """Answers catalog-introspection SQL from the virtual catalog.

    Rebound whenever the manifest hot-swaps, so answers always reflect the
    live catalog version.
    """

    def __init__(self, catalog: VirtualCatalog) -> None:
        self._catalog = catalog
        # Ordered matchers; first hit wins. ORDER MATTERS: a JDBC getTables()
        # query joins pg_class AND pg_namespace, so pg_class (the table list)
        # MUST be checked before pg_namespace (the schema list) - otherwise the
        # schema-list handler wins and QuickSight gets schemas mislabeled as
        # tables (empty table dropdown). pg_attribute (getColumns) likewise
        # before pg_class.
        self._matchers: list[tuple[str, Callable[[str], "RowSet | CatalogResult | None"]]] = [
            ("information_schema.tables", self._answer_is_tables),
            ("information_schema.columns", self._answer_is_columns),
            ("information_schema.schemata", self._answer_is_schemata),
            ("pg_settings", self._answer_pg_settings),
            ("pg_type", self._answer_pg_type_stub),
            ("pg_attribute", self._answer_pg_attribute),
            ("pg_class", self._answer_pg_class),
            ("pg_namespace", self._answer_pg_namespace),
        ]

    def rebind(self, catalog: VirtualCatalog) -> None:
        self._catalog = catalog

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #
    def answer(self, sql: str) -> CatalogResult:
        """Return a catalog answer, projected onto the client's SELECT list.

        Each matcher yields a RowSet in real catalog vocabulary; the projection
        layer maps it to the columns/aliases the client asked for, so JDBC
        drivers that read results by name (QuickSight) find their columns.
        """
        lowered = sql.lower()
        for needle, handler in self._matchers:
            if needle in lowered:
                result = handler(sql)
                if result is None:
                    continue
                # Stubs (pg_type) may return a CatalogResult directly; RowSets
                # get projected onto the client's SELECT list.
                if isinstance(result, RowSet):
                    projected = project(result, sql)
                else:
                    projected = result
                logger.info(
                    "catalog probe matched %r → %d rows, cols=%s",
                    needle, projected.row_count, projected.columns,
                )
                return projected
        logger.info("catalog probe unrecognized; answering empty set: %r", sql[:160])
        return CatalogResult(columns=["?column?"], rows=[], handled=False)

    # Responders below yield a RowSet in REAL catalog column names; answer()
    # projects it onto the client's SELECT list. Extra columns commonly probed
    # by drivers are included so an alias onto any of them resolves.

    # ------------------------------------------------------------------ #
    # information_schema.tables — table discovery
    # ------------------------------------------------------------------ #
    def _answer_is_tables(self, sql: str) -> RowSet:
        cols = ["table_catalog", "table_schema", "table_name", "table_type"]
        rows = [("flowproxy", t.schema, t.name, "VIEW") for t in self._visible_tables()]
        return RowSet(columns=cols, rows=rows)

    # ------------------------------------------------------------------ #
    # information_schema.columns — column discovery + types
    # ------------------------------------------------------------------ #
    def _answer_is_columns(self, sql: str) -> RowSet:
        cols = [
            "table_catalog", "table_schema", "table_name", "column_name",
            "ordinal_position", "is_nullable", "data_type", "udt_name",
        ]
        table_filter = self._extract_table_filter(sql)
        rows: list[tuple[Any, ...]] = []
        for t in self._visible_tables():
            if table_filter and t.name != table_filter:
                continue
            for c in t.columns:
                rows.append((
                    "flowproxy", t.schema, t.name, c.name,
                    c.ordinal + 1, "YES",
                    _OID_TO_SQL_TYPE.get(c.type_oid, "character varying"),
                    _OID_TO_UDT.get(c.type_oid, "varchar"),
                ))
        return RowSet(columns=cols, rows=rows)

    def _answer_is_schemata(self, sql: str) -> RowSet:
        cols = ["catalog_name", "schema_name"]
        return RowSet(columns=cols, rows=[("flowproxy", CATALOG_SCHEMA)])

    # ------------------------------------------------------------------ #
    # pg_catalog probes
    # ------------------------------------------------------------------ #
    def _answer_pg_namespace(self, sql: str) -> RowSet:
        # Include nspowner - some getSchemas variants project or filter on it.
        cols = ["oid", "nspname", "nspowner"]
        rows = [(2200, "public", 10), (2201, CATALOG_SCHEMA, 10)]
        return RowSet(columns=cols, rows=rows)

    def _answer_pg_class(self, sql: str) -> RowSet:
        """pg_class-based probes. Two shapes:

        * JDBC getTables(): SELECT ... n.nspname AS TABLE_SCHEM, c.relname AS
          TABLE_NAME, CASE relkind ... AS TABLE_TYPE FROM pg_class c JOIN
          pg_namespace n ... WHERE nspname = '<schema>'. Detected by the
          TABLE_NAME/TABLE_SCHEM projection; returns one row per cube with the
          columns getTables aliases from, so the projection maps them.
        * Bare relation list: SELECT oid, relname, relkind FROM pg_class.

        Honors a WHERE nspname = '<schema>' filter so the driver's per-schema
        listing only returns cubes when it asks for 'semantic_layer'.
        """
        schema_filter = self._extract_nspname_filter(sql)
        visible = self._visible_tables()
        if schema_filter is not None and schema_filter != CATALOG_SCHEMA:
            visible = []  # driver asked about a different schema (e.g. public)

        # Rich RowSet: real pg_catalog columns PLUS the getTables output aliases,
        # so the projection resolves whichever the driver referenced. TABLE_TYPE
        # is precomputed 'VIEW' (all cubes are views), which matches the driver's
        # CASE(relkind='v' -> 'VIEW') without us evaluating the CASE.
        cols = [
            "oid", "relname", "relnamespace", "relkind", "reltuples",
            "nspname", "table_cat", "table_schem", "table_name", "table_type",
        ]
        rows = [
            (t.table_oid, t.name, 2201, "v", 0,
             t.schema, None, t.schema, t.name, "VIEW")
            for t in visible
        ]
        return RowSet(columns=cols, rows=rows)

    @staticmethod
    def _extract_nspname_filter(sql: str) -> str | None:
        """Find the schema the driver is restricting to, from the WHERE clause.

        Must look ONLY at the WHERE clause: a getTables() query also contains
        ``n.nspname = 'information_schema'`` inside the TABLE_TYPE CASE
        expression, and a naive whole-query regex grabs that first and wrongly
        concludes the driver wants 'information_schema' (-> 0 cubes).

        Returns the target schema name, or None if the query has no positive
        nspname equality/LIKE filter in its WHERE (in which case all cubes are
        returned). Negative filters (``<>``, ``!~``, ``NOT LIKE``) are ignored -
        they exclude system schemas, not our cube schema.
        """
        candidates = CatalogResponder._positive_filter_values(sql, "nspname")
        if candidates is None:  # unparseable: fall back to a WHERE-scoped regex
            m = CatalogResponder._regex_after_where(sql, "nspname")
            return m
        # Prefer our own schema if it appears; else the first positive filter.
        if CATALOG_SCHEMA in candidates:
            return CATALOG_SCHEMA
        return candidates[0] if candidates else None

    @staticmethod
    def _positive_filter_values(sql: str, column: str) -> list[str] | None:
        """Positive =/LIKE literal values for ``column`` across ALL WHERE
        clauses in the query (handles filters nested in a subquery's WHERE, as
        the windowed getColumns() emits). Returns None if the SQL won't parse.

        Only WHERE clauses are inspected, so a ``nspname = 'information_schema'``
        living in a SELECT-list CASE expression is never mistaken for a filter.
        """
        try:
            tree = sqlglot.parse_one(sql, read="postgres")
        except sqlglot.errors.ParseError:
            return None
        values: list[str] = []
        for where in tree.find_all(exp.Where):
            for node in where.walk():
                if isinstance(node, (exp.EQ, exp.Like)):
                    col, val = node.this, node.expression
                    name = col.name if isinstance(col, exp.Column) else None
                    if name == column and isinstance(val, exp.Literal) and val.is_string:
                        values.append(val.this.replace("\\", ""))
        return values

    @staticmethod
    def _regex_after_where(sql: str, column: str) -> str | None:
        idx = sql.lower().rfind(" where ")
        scope = sql[idx:] if idx >= 0 else sql
        m = re.search(rf"{column}\s*(?:=|LIKE)\s*'([^']+)'", scope, re.IGNORECASE)
        return m.group(1).replace("\\", "") if m else None

    def _answer_pg_attribute(self, sql: str) -> RowSet:
        """pg_attribute-based probes, principally JDBC getColumns().

        Emits both the raw pg_attribute columns AND the getColumns output
        columns (table_schem, table_name, column_name, data_type, type_name,
        column_size, nullable, ordinal_position, is_nullable, ...), so the
        projection resolves whichever the driver aliased. Honors the schema
        filter (nspname, WHERE-scoped) and the per-table filter (relname), so
        getColumns for one cube returns only that cube's columns.
        """
        schema_filter = self._extract_nspname_filter(sql)
        if schema_filter is not None and schema_filter != CATALOG_SCHEMA:
            return RowSet(columns=_PG_ATTRIBUTE_COLS, rows=[])
        table_filter = self._extract_relname_filter(sql)

        rows: list[tuple[Any, ...]] = []
        for t in self._visible_tables():
            if table_filter and t.name != table_filter:
                continue
            for c in t.columns:
                ordinal = c.ordinal + 1
                udt = _OID_TO_UDT.get(c.type_oid, "varchar")
                # Build by column name (order-safe against _PG_ATTRIBUTE_COLS).
                values: dict[str, Any] = {
                    # raw catalog columns the windowed getColumns() selects
                    "nspname": t.schema, "relname": t.name,
                    "attrelid": t.table_oid, "attname": c.name,
                    "atttypid": c.type_oid, "attnotnull": False,
                    "atttypmod": -1, "attlen": -1, "typtypmod": -1,
                    "attnum": ordinal,                # already row_number()-style 1-based
                    "attidentity": None, "attgenerated": None,
                    "adsrc": None, "description": c.description,
                    "typbasetype": 0, "typtype": "b", "attisdropped": False,
                    # simpler getColumns output aliases
                    "table_cat": None, "table_schem": t.schema,
                    "table_name": t.name, "column_name": c.name,
                    "data_type": _OID_TO_JDBC_TYPE.get(c.type_oid, 12),
                    "type_name": udt, "column_size": None,
                    "nullable": 1, "is_nullable": "YES",
                    "ordinal_position": ordinal, "remarks": c.description,
                }
                rows.append(tuple(values[col] for col in _PG_ATTRIBUTE_COLS))
        return RowSet(columns=_PG_ATTRIBUTE_COLS, rows=rows)

    @staticmethod
    def _extract_relname_filter(sql: str) -> str | None:
        """The per-table filter (c.relname = '<cube>') from any WHERE clause,
        including the inner WHERE of a windowed getColumns() subquery."""
        vals = CatalogResponder._positive_filter_values(sql, "relname")
        if vals is None:
            return CatalogResponder._regex_after_where(sql, "relname")
        return vals[0] if vals else None

    def _answer_pg_settings(self, sql: str) -> RowSet:
        """pg_catalog.pg_settings lookups (SELECT setting FROM pg_settings
        WHERE name = 'X'). JDBC drivers probe these during getColumns/
        getPrimaryKeys (e.g. max_index_keys) and do rs.next(); rs.getInt(1) -
        an empty result THROWS. So every such probe must return >= 1 row.

        Known settings return real PostgreSQL defaults; an unknown setting
        returns a single row with an empty value rather than 0 rows, so the
        driver never chokes (generic - no chipping away one setting at a time).
        """
        name = self._extract_setting_name(sql)
        value = _PG_SETTINGS_DEFAULTS.get(name, "") if name is not None else ""
        # Provide the columns pg_settings probes commonly select/alias.
        cols = ["name", "setting", "unit", "category", "short_desc",
                "vartype", "source", "min_val", "max_val", "boot_val", "reset_val"]
        row = (name or "", value, None, "FlowProxy", "", "string",
               "default", None, None, value, value)
        return RowSet(columns=cols, rows=[row])

    @staticmethod
    def _extract_setting_name(sql: str) -> str | None:
        m = re.search(r"name\s*=\s*'([^']+)'", sql, re.IGNORECASE)
        return m.group(1) if m else None

    def _answer_pg_type_stub(self, sql: str) -> CatalogResult | None:
        """Power BI / Npgsql pg_type composite bootstrap — DEFERRED stub.

        Returns None so the dispatcher keeps looking (a query joining pg_type
        AND pg_class should still be answered by _answer_pg_class if that
        matcher matches). If a query is *purely* the Npgsql pg_type bootstrap,
        it falls through to an empty set; Power BI qualification will replace
        this with a corpus-driven answer.
        """
        lowered = sql.lower()
        if "pg_type" in lowered and "typname" in lowered and "pg_class" not in lowered:
            logger.warning(
                "Npgsql/Power BI pg_type bootstrap seen but not yet implemented; "
                "returning empty set (Power BI support deferred)"
            )
            return CatalogResult(columns=["oid", "typname"], rows=[], handled=False)
        return None

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _visible_tables(self) -> list[CatalogTable]:
        # Role-based visibility filtering (ADR-0006/0010) hooks in here later;
        # for now every cube is visible.
        return self._catalog.tables

    @staticmethod
    def _extract_table_filter(sql: str) -> str | None:
        m = re.search(r"table_name\s*=\s*'([^']+)'", sql, re.IGNORECASE)
        return m.group(1) if m else None
