"""WS7c — runtime hot-swap tests (ADR-0014).

Exercises SemanticLayerManager blue/green swap semantics against real bundles
built from finance_demo: initial load, no-op when unchanged, swap on new
version, and safety when a build fails (current keeps serving).
"""

from __future__ import annotations

import textwrap

import pytest

from engine.live_layer import SemanticLayerManager
from engine.manifest_store import Bundle, open_store

pytestmark = pytest.mark.usefixtures("finance_demo_built")


@pytest.fixture
def profiles_template():
    from tests.conftest import DUCKDB_PATH

    return textwrap.dedent(
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


def _bundle(git_sha: str, profiles_template: str) -> Bundle:
    from tests.conftest import MANIFEST_PATH

    return Bundle.build(
        MANIFEST_PATH.read_text(),
        git_sha=git_sha,
        dbt_version="1.11.12",
        metricflow_version="0.211.0",
        built_at="2026-07-04T00:00:00Z",
        profiles_template=profiles_template,
    )


def test_load_then_query(tmp_path, profiles_template):
    store = open_store(str(tmp_path / "prod"))
    store.publish(_bundle("v1", profiles_template))
    mgr = SemanticLayerManager(store)
    layer = mgr.load()
    assert layer.version == "v1"
    # The live layer is fully wired: compiler + catalog + responder.
    assert layer.compiler.registry.is_metric("account_balance")
    assert layer.catalog.table("daily_balances") is not None


def test_refresh_noop_when_version_unchanged(tmp_path, profiles_template):
    store = open_store(str(tmp_path / "prod"))
    store.publish(_bundle("v1", profiles_template))
    mgr = SemanticLayerManager(store)
    mgr.load()
    assert mgr.refresh() is False          # same version → no swap
    assert mgr.current.version == "v1"


def test_refresh_swaps_on_new_version(tmp_path, profiles_template):
    store = open_store(str(tmp_path / "prod"))
    store.publish(_bundle("v1", profiles_template))
    mgr = SemanticLayerManager(store)
    mgr.load()
    assert mgr.current.version == "v1"

    store.publish(_bundle("v2", profiles_template))   # CI publishes a new bundle
    assert mgr.refresh() is True                       # blue/green swap
    assert mgr.current.version == "v2"


def test_failed_build_keeps_current(tmp_path, profiles_template):
    """A bad publish must NOT take the proxy down — current keeps serving."""
    store = open_store(str(tmp_path / "prod"))
    store.publish(_bundle("v1", profiles_template))
    mgr = SemanticLayerManager(store)
    mgr.load()

    # Publish a broken bundle (manifest that won't parse) under a new version.
    broken = Bundle.build(
        "{ this is not valid json",
        git_sha="v2-broken", dbt_version="1.11.12", metricflow_version="0.211.0",
        built_at="2026-07-04T00:00:00Z", profiles_template=profiles_template,
    )
    # Bypass build-time verify by publishing raw bytes with a matching sha.
    store.publish(broken)

    assert mgr.refresh() is False           # build failed → no swap
    assert mgr.current.version == "v1"      # old version still live
    # And it still works.
    r = mgr.current.compiler.execute_request(["account_balance"], ["account__region"])
    assert r.row_count > 0


def test_in_flight_layer_reference_is_stable(tmp_path, profiles_template):
    """A handler that captured `current` keeps using it across a swap."""
    store = open_store(str(tmp_path / "prod"))
    store.publish(_bundle("v1", profiles_template))
    mgr = SemanticLayerManager(store)
    mgr.load()
    captured = mgr.current            # simulate a request capturing the layer

    store.publish(_bundle("v2", profiles_template))
    mgr.refresh()
    assert mgr.current.version == "v2"     # manager advanced
    assert captured.version == "v1"        # captured reference unchanged (immutable)
