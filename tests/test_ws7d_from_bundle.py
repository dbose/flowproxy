"""WS7d — SemanticCompiler.from_bundle tests (ADR-0012/0015).

Productionizes the WS7 spike: build a working compiler from a deployment
bundle + injected warehouse creds, with NO checked-out dbt project, and prove
the golden numbers come back — semi-additive guardrail intact.
"""

from __future__ import annotations

import textwrap

import pytest

from engine.compiler import SemanticCompiler
from engine.manifest_store import Bundle

pytestmark = pytest.mark.usefixtures("finance_demo_built")


@pytest.fixture
def duckdb_bundle():
    """A real bundle: finance_demo's manifest + a duckdb profile template."""
    from tests.conftest import DUCKDB_PATH, MANIFEST_PATH

    profiles_template = textwrap.dedent(
        f"""\
        flowproxy_runtime:
          target: prod
          outputs:
            prod:
              type: duckdb
              path: "{DUCKDB_PATH}"
              threads: 1
              schema: main
        """
    )
    return Bundle.build(
        MANIFEST_PATH.read_text(),
        git_sha="deadbee",
        dbt_version="1.11.12",
        metricflow_version="0.211.0",
        built_at="2026-07-04T00:00:00Z",
        profiles_template=profiles_template,
    )


def test_from_bundle_builds_working_compiler(duckdb_bundle, tmp_path):
    compiler = SemanticCompiler.from_bundle(
        duckdb_bundle, skeleton_root=tmp_path / "skeleton"
    )
    assert compiler.bundle_version == "deadbee"
    # registry populated from the bundled manifest
    assert compiler.registry.is_metric("account_balance")


def test_from_bundle_executes_golden_numbers(duckdb_bundle, tmp_path):
    """The proof: a bundle-built compiler returns the same semi-additive
    end-of-period balances as the project-built one."""
    compiler = SemanticCompiler.from_bundle(
        duckdb_bundle, skeleton_root=tmp_path / "skeleton"
    )
    result = compiler.execute_request(
        metrics=["account_balance"],
        dimensions=["metric_time__month", "account__region"],
        order_by=["metric_time__month"],
    )
    idx_r = result.columns.index("account__region")
    idx_m = result.columns.index("metric_time__month")
    idx_b = result.columns.index("account_balance")
    by_key = {(str(row[idx_m])[:7], row[idx_r]): float(row[idx_b]) for row in result.rows}
    assert by_key[("2024-01", "EMEA")] == 1500.0
    assert by_key[("2024-03", "AMER")] == 3000.0


def test_from_bundle_without_profiles_template_fails():
    from engine.exceptions import CompilerBootstrapError

    bundle = Bundle.build(
        '{"semantic_models": [], "metrics": [], "project_configuration": {}}',
        git_sha="x",
        dbt_version="1.11.12",
        metricflow_version="0.211.0",
        built_at="2026-07-04T00:00:00Z",
        # no profiles_template
    )
    with pytest.raises(CompilerBootstrapError, match="profiles.template"):
        SemanticCompiler.from_bundle(bundle)
