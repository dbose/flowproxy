"""L2 — manifest & registry tests (ADR-0003).

Prove that `dbt parse` on the committed project yields a manifest the
SemanticRegistry classifies correctly: metrics vs dimensions, entity
qualification, and time-grain suffix handling. No warehouse needed beyond
the parse.
"""

from __future__ import annotations

import json

import pytest

from engine.registry import SemanticRegistry


@pytest.fixture(scope="module")
def registry(finance_demo_built):
    from tests.conftest import MANIFEST_PATH
    from dbt_semantic_interfaces.implementations.semantic_manifest import (
        PydanticSemanticManifest,
    )

    manifest = PydanticSemanticManifest.parse_obj(json.loads(MANIFEST_PATH.read_text()))
    return SemanticRegistry.from_manifest(manifest)


def test_metrics_discovered(registry):
    for m in ("account_balance", "naive_balance_total", "total_transactions", "transaction_count"):
        assert registry.is_metric(m), f"{m} not classified as a metric"


def test_metric_not_confused_with_dimension(registry):
    assert registry.resolve_dimension("account_balance") is None


def test_bare_dimension_qualifies_to_entity(registry):
    # `region` is defined on the daily_balances model whose primary entity is
    # `account` → must resolve to account__region.
    assert registry.resolve_dimension("region") == "account__region"


def test_qualified_dimension_passthrough(registry):
    assert registry.resolve_dimension("account__region") == "account__region"


def test_metric_time_and_grain_suffix(registry):
    assert registry.resolve_dimension("metric_time") == "metric_time"
    assert registry.resolve_dimension("metric_time__month") == "metric_time__month"


def test_unknown_dimension_returns_none_with_suggestions(registry):
    assert registry.resolve_dimension("regionn") is None
    assert "account__region" in registry.suggestions("regionn") or registry.suggestions("region")
