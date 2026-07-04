"""WS2 — clause translation tests (ADR-0005).

Two layers:
  * unit — sqlglot → MetricFlow constructs, no warehouse (registry from manifest)
  * golden — filters pushed end-to-end through DuckDB, assert exact numbers
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
import sqlglot
from sqlglot import expressions as exp

from engine.exceptions import UnsupportedQueryError
from engine.filters import ClauseTranslator
from engine.registry import SemanticRegistry


@pytest.fixture(scope="module")
def registry(finance_demo_built):
    from tests.conftest import MANIFEST_PATH
    from dbt_semantic_interfaces.implementations.semantic_manifest import (
        PydanticSemanticManifest,
    )

    manifest = PydanticSemanticManifest.parse_obj(json.loads(MANIFEST_PATH.read_text()))
    return SemanticRegistry.from_manifest(manifest)


@pytest.fixture
def translate(registry):
    tr = ClauseTranslator(registry, max_rows=10_000)

    def _translate(sql: str):
        select = sqlglot.parse_one(sql, read="postgres")
        assert isinstance(select, exp.Select)
        return tr.translate(select)

    return _translate


# --------------------------------------------------------------------------- #
# Unit: WHERE → where_constraints / time_constraint
# --------------------------------------------------------------------------- #
def test_categorical_equality(translate):
    c = translate('SELECT account_balance FROM cube WHERE region = \'EMEA\'')
    assert c.where_constraints == ["{{ Dimension('account__region') }} = 'EMEA'"]
    assert c.time_constraint_start is None


def test_categorical_in_list(translate):
    c = translate('SELECT account_balance FROM cube WHERE region IN (\'EMEA\', \'AMER\')')
    assert c.where_constraints == ["{{ Dimension('account__region') }} IN ('EMEA', 'AMER')"]


def test_not_equal_and_like(translate):
    c = translate("SELECT account_balance FROM cube WHERE region <> 'EMEA' AND account_name LIKE 'Acme%'")
    assert "{{ Dimension('account__region') }} <> 'EMEA'" in c.where_constraints
    assert "{{ Dimension('account__account_name') }} LIKE 'Acme%'" in c.where_constraints


def test_time_inequality_becomes_time_constraint(translate):
    c = translate("SELECT account_balance FROM cube WHERE metric_time >= '2024-02-01'")
    assert c.time_constraint_start == dt.datetime(2024, 2, 1)
    assert c.where_constraints == []


def test_between_on_time(translate):
    c = translate("SELECT account_balance FROM cube WHERE metric_time BETWEEN '2024-01-01' AND '2024-03-31'")
    assert c.time_constraint_start == dt.datetime(2024, 1, 1)
    assert c.time_constraint_end == dt.datetime(2024, 3, 31)


def test_sql_injection_literal_is_escaped(translate):
    # A single quote in the literal must be doubled, never break out of the string.
    c = translate("SELECT account_balance FROM cube WHERE region = 'E''MEA'")
    assert c.where_constraints == ["{{ Dimension('account__region') }} = 'E''MEA'"]
    # And a classic injection payload stays inside the quotes.
    c2 = translate("SELECT account_balance FROM cube WHERE region = 'x'' OR 1=1--'")
    assert c2.where_constraints == ["{{ Dimension('account__region') }} = 'x'' OR 1=1--'"]


# --------------------------------------------------------------------------- #
# Unit: ORDER BY / LIMIT
# --------------------------------------------------------------------------- #
def test_order_by_name_and_ordinal(translate):
    c = translate('SELECT region, account_balance FROM cube ORDER BY region DESC, 2')
    assert c.order_by == ["-account__region", "account_balance"]


def test_limit_clamped(translate):
    c = translate("SELECT account_balance FROM cube LIMIT 999999")
    assert c.limit == 10_000  # max_rows cap


def test_no_limit_defaults_to_max(translate):
    c = translate("SELECT account_balance FROM cube")
    assert c.limit == 10_000


# --------------------------------------------------------------------------- #
# Deny-by-default: unsupported shapes rejected, never silently dropped
# --------------------------------------------------------------------------- #
def test_or_rejected(translate):
    with pytest.raises(UnsupportedQueryError, match="OR"):
        translate("SELECT account_balance FROM cube WHERE region = 'EMEA' OR region = 'AMER'")


def test_between_on_categorical_rejected(translate):
    with pytest.raises(UnsupportedQueryError, match="BETWEEN"):
        translate("SELECT account_balance FROM cube WHERE region BETWEEN 'A' AND 'Z'")


def test_unknown_filter_column_rejected(translate):
    with pytest.raises(UnsupportedQueryError, match="not a known dimension"):
        translate("SELECT account_balance FROM cube WHERE nonsense = 1")


def test_function_predicate_rejected(translate):
    with pytest.raises(UnsupportedQueryError):
        translate("SELECT account_balance FROM cube WHERE upper(region) = 'EMEA'")


# --------------------------------------------------------------------------- #
# Golden: filters pushed through DuckDB → exact numbers (ADR-0005 + ADR-0003)
# --------------------------------------------------------------------------- #
def test_where_filter_golden(compiler):
    """region='EMEA' filter → only EMEA's semi-additive balances."""
    result = compiler.execute_request(
        metrics=["account_balance"],
        dimensions=["metric_time__month"],
        where_constraints=["{{ Dimension('account__region') }} = 'EMEA'"],
        order_by=["metric_time__month"],
    )
    vals = [float(r[result.columns.index("account_balance")]) for r in result.rows]
    assert vals == [1500.0, 1200.0, 2000.0]  # EMEA only, not summed with AMER


def test_time_constraint_golden(compiler):
    """time window Feb only → single month row."""
    result = compiler.execute_request(
        metrics=["total_transactions"],
        dimensions=["metric_time__month"],
        time_constraint_start=dt.datetime(2024, 2, 1),
        time_constraint_end=dt.datetime(2024, 2, 29),
    )
    assert result.row_count == 1
    assert float(result.rows[0][result.columns.index("total_transactions")]) == 650.0


def test_full_pipeline_extract_then_execute(compiler):
    """End-to-end: raw QuickSight-style SQL → extractor → compiler → numbers."""
    from engine.parser import SQLExtractor

    extractor = SQLExtractor(compiler.registry, max_rows=10_000)
    sql = (
        'SELECT "region", "total_transactions" FROM "all_metrics" '
        "WHERE region = 'AMER' ORDER BY 1 LIMIT 50"
    )
    q = extractor.extract(sql)
    assert q.where_constraints == ["{{ Dimension('account__region') }} = 'AMER'"]

    result = compiler.execute_request(
        metrics=q.metrics,
        dimensions=q.dimensions,
        where_constraints=q.where_constraints,
        time_constraint_start=q.time_constraint_start,
        time_constraint_end=q.time_constraint_end,
        order_by=q.order_by,
        limit=q.limit,
    )
    amer = float(result.rows[0][result.columns.index("total_transactions")])
    assert amer == 1800.0  # AMER total: 500 + 400 + 900
