"""WS3 — virtual catalog + introspection tests (ADR-0010 / ADR-0011).

Proves the analyst-facing exploration surface: cubes derived from the manifest,
pre-scoped to valid fields, and discoverable via the introspection SQL a BI
driver emits.
"""

from __future__ import annotations

import hashlib

import pytest

from engine.catalog import ALL_METRICS_TABLE, CATALOG_SCHEMA, CatalogBuilder
from network.catalog import CatalogResponder


@pytest.fixture(scope="module")
def catalog(compiler):
    from tests.conftest import MANIFEST_PATH

    sha = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()[:12]
    return CatalogBuilder(compiler, sha).build()


@pytest.fixture(scope="module")
def responder(catalog):
    return CatalogResponder(catalog)


# --------------------------------------------------------------------------- #
# Catalog model
# --------------------------------------------------------------------------- #
def test_one_cube_per_semantic_model_with_metrics(catalog):
    names = {t.name for t in catalog.tables}
    # daily_balances + transactions have metrics → cubes. accounts is a pure
    # dimension model → NOT a cube.
    assert "daily_balances" in names
    assert "transactions" in names
    assert "accounts" not in names


def test_all_metrics_wide_table_present(catalog):
    t = catalog.table(ALL_METRICS_TABLE)
    assert t is not None
    metric_cols = {c.name for c in t.columns if c.is_metric}
    assert {"account_balance", "total_transactions", "transaction_count"} <= metric_cols


def test_saved_query_cubes_present(catalog):
    names = {t.name for t in catalog.tables}
    assert "monthly_balance_by_region" in names
    assert "monthly_transactions_by_region" in names


def test_prescoping_excludes_unreachable_dimensions(catalog):
    """The daily_balances cube must NOT expose transaction-only dimensions.

    This is the ADR-0011 guardrail: an analyst literally cannot drag
    transaction_type onto a balance cube because it isn't a column there.
    """
    balances = catalog.table("daily_balances")
    dim_names = {c.name for c in balances.columns if not c.is_metric}
    assert "account__region" in dim_names            # reachable via account entity
    assert "account__balance_date" in dim_names      # local time dim
    assert "transaction__transaction_type" not in dim_names  # unreachable → excluded


def test_column_types_and_order(catalog):
    balances = catalog.table("daily_balances")
    # Dimensions come before metrics.
    kinds = [c.is_metric for c in balances.columns]
    assert kinds == sorted(kinds)  # all False (dims) then all True (metrics)
    # Metric → numeric OID; time dim → timestamp OID.
    abal = balances.column("account_balance")
    assert abal.is_metric and abal.type_oid == 1700
    bdate = balances.column("account__balance_date")
    assert bdate.type_oid == 1114


def test_oids_deterministic_for_same_manifest(compiler):
    from tests.conftest import MANIFEST_PATH

    sha = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()[:12]
    a = CatalogBuilder(compiler, sha).build()
    b = CatalogBuilder(compiler, sha).build()
    assert {t.name: t.table_oid for t in a.tables} == {t.name: t.table_oid for t in b.tables}


# --------------------------------------------------------------------------- #
# Introspection responder (QuickSight discovery path)
# --------------------------------------------------------------------------- #
def test_information_schema_tables_lists_cubes(responder):
    r = responder.answer(
        "SELECT table_catalog, table_schema, table_name, table_type "
        "FROM information_schema.tables WHERE table_schema = 'semantic_layer'"
    )
    assert r.handled
    names = {row[2] for row in r.rows}
    assert "daily_balances" in names and ALL_METRICS_TABLE in names
    assert all(row[3] == "VIEW" for row in r.rows)


def test_information_schema_columns_typed(responder):
    r = responder.answer(
        "SELECT table_name, column_name, ordinal_position, data_type, udt_name "
        "FROM information_schema.columns WHERE table_name = 'daily_balances'"
    )
    assert r.handled
    # Projection honored: exactly the SELECTed columns, in order.
    assert r.columns == ["table_name", "column_name", "ordinal_position", "data_type", "udt_name"]
    ci = {name: i for i, name in enumerate(r.columns)}
    by_col = {row[ci["column_name"]]: (row[ci["data_type"]], row[ci["udt_name"]]) for row in r.rows}
    assert by_col["account_balance"] == ("numeric", "numeric")
    assert by_col["account__balance_date"] == ("timestamp without time zone", "timestamp")
    # Only that cube's columns returned (table filter honored).
    assert {row[ci["table_name"]] for row in r.rows} == {"daily_balances"}


def test_getschemas_alias_projection(responder):
    """The QuickSight/JDBC getSchemas probe aliases nspname AS schema_name.

    The driver reads getString("schema_name"), so the result MUST expose a
    column literally named schema_name - not the raw (oid, nspname) shape.
    Regression for the blank-schema-dropdown / SQLSTATE 02000 bug.
    """
    r = responder.answer(
        "SELECT nspname AS schema_name FROM pg_namespace "
        "WHERE nspname NOT LIKE 'pg\\_%' ORDER BY schema_name"
    )
    assert r.handled
    assert r.columns == ["schema_name"]              # aliased, single column
    names = {row[0] for row in r.rows}
    assert CATALOG_SCHEMA in names                    # 'semantic_layer' visible


def test_projection_reorders_and_selects(responder):
    """A probe selecting a subset in a different order gets exactly that."""
    r = responder.answer("SELECT relkind, relname FROM pg_class")
    assert r.columns == ["relkind", "relname"]        # order honored
    assert all(row[0] == "v" for row in r.rows)       # relkind first now
    assert "daily_balances" in {row[1] for row in r.rows}


def test_pg_class_lists_relations(responder):
    r = responder.answer("SELECT oid, relname, relnamespace, relkind FROM pg_class")
    assert r.handled
    assert all(row[3] == "v" for row in r.rows)
    assert "transactions" in {row[1] for row in r.rows}


def test_pg_namespace_exposes_schema(responder):
    r = responder.answer("SELECT oid, nspname FROM pg_namespace")
    assert CATALOG_SCHEMA in {row[1] for row in r.rows}


def test_unrecognized_probe_returns_unhandled_empty(responder):
    r = responder.answer("SELECT current_setting('max_index_keys')")
    assert not r.handled
    assert r.rows == []


def test_powerbi_pg_type_bootstrap_is_deferred_stub(responder):
    r = responder.answer(
        "SELECT t.oid, t.typname FROM pg_type t WHERE t.typname IS NOT NULL"
    )
    # Deferred: empty + unhandled so it's visibly a stub, not a real answer.
    assert not r.handled
    assert r.rows == []
