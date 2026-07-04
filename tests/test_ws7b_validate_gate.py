"""WS7b — CI deploy gate tests (ADR-0013).

Plan-only validation against the real finance_demo project (passes), a broken
manifest (fails the gate), and the publish path (validate → bundle → store).
"""

from __future__ import annotations

import json

import pytest

from engine.manifest_store import Bundle, open_store
from flowproxy_cli.validate import validate_project

pytestmark = pytest.mark.usefixtures("finance_demo_built")


def test_gate_passes_on_valid_project(finance_demo_built):
    from tests.conftest import MANIFEST_PATH

    report, _ = validate_project(finance_demo_built, MANIFEST_PATH)
    assert report.ok
    # Every saved query and cube planned.
    assert set(report.saved_queries.values()) == {"ok"}
    assert set(report.cubes.values()) == {"ok"}
    assert "daily_balances" in report.cubes
    assert "monthly_balance_by_region" in report.saved_queries


def test_gate_fails_on_broken_saved_query(finance_demo_built, tmp_path):
    """Inject a saved query referencing an unreachable dimension → gate fails."""
    from tests.conftest import MANIFEST_PATH

    manifest = json.loads(MANIFEST_PATH.read_text())
    # Add a saved query grouping a balance metric by a transaction-only dim
    # (unreachable join path → planning must fail).
    manifest["saved_queries"].append({
        "name": "broken_query",
        "query_params": {
            "metrics": ["account_balance"],
            "group_by": ["Dimension('transaction__transaction_type')"],
            "where": [],
        },
        "metadata": None,
    })
    broken = tmp_path / "semantic_manifest.json"
    broken.write_text(json.dumps(manifest))

    report, _ = validate_project(finance_demo_built, broken)
    assert not report.ok
    assert report.saved_queries["broken_query"] != "ok"


def test_gate_publishes_bundle_on_success(finance_demo_built, tmp_path):
    from tests.conftest import MANIFEST_PATH

    report, compiler = validate_project(finance_demo_built, MANIFEST_PATH)
    assert report.ok

    bundle = Bundle.build(
        MANIFEST_PATH.read_text(),
        git_sha="ci1234",
        dbt_version="1.11.12",
        metricflow_version="0.211.0",
        built_at="2026-07-04T00:00:00Z",
        profiles_template="flowproxy_runtime:\n  target: prod\n",
        validation_report=report.as_dict(),
    )
    store = open_store(str(tmp_path / "prod"))
    version = store.publish(bundle)
    assert version == "ci1234"

    # The published bundle carries the validation report — auditable.
    fetched = store.resolve_latest()
    assert fetched.metadata.validation_report["ok"] is True
    assert "daily_balances" in fetched.metadata.validation_report["cubes"]


def test_breaking_change_diff_flags_removed_metric(finance_demo_built, tmp_path):
    from tests.conftest import MANIFEST_PATH

    # "Deployed" bundle has an extra metric the new manifest lacks.
    deployed_manifest = json.loads(MANIFEST_PATH.read_text())
    deployed_manifest["metrics"].append({
        "name": "legacy_metric", "type": "simple",
        "type_params": {"measure": {"name": "month_end_balance"}},
    })
    store = open_store(str(tmp_path / "prod"))
    store.publish(Bundle.build(
        json.dumps(deployed_manifest),
        git_sha="old", dbt_version="1.11.12", metricflow_version="0.211.0",
        built_at="2026-07-01T00:00:00Z", profiles_template="x:\n",
    ))

    report, _ = validate_project(finance_demo_built, MANIFEST_PATH, deployed_store=store)
    assert "metric removed: legacy_metric" in report.breaking_changes
