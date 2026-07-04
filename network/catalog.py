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


class CatalogResponder:
    """Answers catalog-introspection SQL from the virtual catalog.

    Rebound whenever the manifest hot-swaps, so answers always reflect the
    live catalog version.
    """

    def __init__(self, catalog: VirtualCatalog) -> None:
        self._catalog = catalog
        # Ordered matchers; first hit wins.
        self._matchers: list[tuple[str, Callable[[str], CatalogResult | None]]] = [
            ("information_schema.tables", self._answer_is_tables),
            ("information_schema.columns", self._answer_is_columns),
            ("information_schema.schemata", self._answer_is_schemata),
            ("pg_type", self._answer_pg_type_stub),
            ("pg_namespace", self._answer_pg_namespace),
            ("pg_class", self._answer_pg_class),
            ("pg_attribute", self._answer_pg_attribute),
        ]

    def rebind(self, catalog: VirtualCatalog) -> None:
        self._catalog = catalog

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #
    def answer(self, sql: str) -> CatalogResult:
        """Return a catalog answer, or an unhandled empty result."""
        lowered = sql.lower()
        for needle, handler in self._matchers:
            if needle in lowered:
                result = handler(sql)
                if result is not None:
                    logger.info(
                        "catalog probe matched %r → %d rows", needle, result.row_count
                    )
                    return result
        logger.info("catalog probe unrecognized; answering empty set: %r", sql[:160])
        return CatalogResult(columns=["?column?"], rows=[], handled=False)

    # ------------------------------------------------------------------ #
    # information_schema.tables — QuickSight's primary table discovery
    # ------------------------------------------------------------------ #
    def _answer_is_tables(self, sql: str) -> CatalogResult:
        cols = ["table_catalog", "table_schema", "table_name", "table_type"]
        rows = [
            ("flowproxy", t.schema, t.name, "VIEW")
            for t in self._visible_tables()
        ]
        return CatalogResult(columns=cols, rows=rows)

    # ------------------------------------------------------------------ #
    # information_schema.columns — column discovery + types
    # ------------------------------------------------------------------ #
    def _answer_is_columns(self, sql: str) -> CatalogResult:
        cols = [
            "table_catalog", "table_schema", "table_name", "column_name",
            "ordinal_position", "is_nullable", "data_type", "udt_name",
        ]
        # Honor a table filter if the driver scoped the probe to one cube.
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
        return CatalogResult(columns=cols, rows=rows)

    def _answer_is_schemata(self, sql: str) -> CatalogResult:
        cols = ["catalog_name", "schema_name"]
        return CatalogResult(columns=cols, rows=[("flowproxy", CATALOG_SCHEMA)])

    # ------------------------------------------------------------------ #
    # pg_catalog probes
    # ------------------------------------------------------------------ #
    def _answer_pg_namespace(self, sql: str) -> CatalogResult:
        cols = ["oid", "nspname"]
        return CatalogResult(columns=cols, rows=[(2200, "public"), (2201, CATALOG_SCHEMA)])

    def _answer_pg_class(self, sql: str) -> CatalogResult:
        # relkind 'v' = view; drivers list these as selectable relations.
        cols = ["oid", "relname", "relnamespace", "relkind"]
        rows = [(t.table_oid, t.name, 2201, "v") for t in self._visible_tables()]
        return CatalogResult(columns=cols, rows=rows)

    def _answer_pg_attribute(self, sql: str) -> CatalogResult:
        cols = ["attrelid", "attname", "atttypid", "attnum", "attnotnull"]
        rows: list[tuple[Any, ...]] = []
        for t in self._visible_tables():
            for c in t.columns:
                rows.append((t.table_oid, c.name, c.type_oid, c.ordinal + 1, False))
        return CatalogResult(columns=cols, rows=rows)

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
