"""WS7a — ManifestStore + Bundle tests (ADR-0012).

No warehouse or dbt project needed — these exercise bundle packing/integrity and
the fsspec-backed store over local + in-memory filesystems.
"""

from __future__ import annotations

import hashlib

import pytest

from engine.exceptions import FlowProxyError
from engine.manifest_store import (
    Bundle,
    BundleIntegrityError,
    FsspecStore,
    ManifestStoreError,
    open_store,
)

SAMPLE_MANIFEST = '{"semantic_models": [], "metrics": [], "project_configuration": {}}'
PROFILES_TEMPLATE = "flowproxy_runtime:\n  target: prod\n  outputs:\n    prod:\n      type: duckdb\n"


def _bundle() -> Bundle:
    return Bundle.build(
        SAMPLE_MANIFEST,
        git_sha="abc1234",
        dbt_version="1.11.12",
        metricflow_version="0.211.0",
        built_at="2026-07-04T00:00:00Z",
        profiles_template=PROFILES_TEMPLATE,
        validation_report={"saved_queries": "ok", "cubes": 3},
    )


# --------------------------------------------------------------------------- #
# Bundle model
# --------------------------------------------------------------------------- #
def test_bundle_roundtrip_through_tar():
    original = _bundle()
    restored = Bundle.from_tar_bytes(original.to_tar_bytes())
    assert restored.semantic_manifest_json == SAMPLE_MANIFEST
    assert restored.metadata.git_sha == "abc1234"
    assert restored.profiles_template == PROFILES_TEMPLATE
    assert restored.metadata.validation_report == {"saved_queries": "ok", "cubes": 3}


def test_bundle_version_is_git_sha():
    assert _bundle().version == "abc1234"


def test_bundle_computes_manifest_sha256():
    b = _bundle()
    assert b.metadata.manifest_sha256 == hashlib.sha256(SAMPLE_MANIFEST.encode()).hexdigest()


def test_integrity_check_rejects_tampering():
    b = _bundle()
    # Tamper: swap the manifest but keep the old sha in metadata.
    tampered = Bundle(
        semantic_manifest_json='{"semantic_models": [], "metrics": [{"name": "sneaky"}], "project_configuration": {}}',
        metadata=b.metadata,  # stale sha256
        profiles_template=b.profiles_template,
    )
    with pytest.raises(BundleIntegrityError):
        tampered.verify()


def test_from_tar_bytes_verifies_integrity():
    b = _bundle()
    tar = b.to_tar_bytes()
    # from_tar_bytes calls verify() — a consistent bundle round-trips fine.
    assert Bundle.from_tar_bytes(tar).version == "abc1234"


def test_reproducible_bundle_bytes():
    # Deterministic mtime → identical bytes for identical content (CI caching).
    assert _bundle().to_tar_bytes() == _bundle().to_tar_bytes()


# --------------------------------------------------------------------------- #
# FsspecStore — in-memory + local
# --------------------------------------------------------------------------- #
def test_publish_and_resolve_memory_fs():
    store = FsspecStore("memory://flowproxy/production/")
    version = store.publish(_bundle())
    assert version == "abc1234"
    assert store.resolve_version() == "abc1234"
    fetched = store.resolve_latest()
    assert fetched.semantic_manifest_json == SAMPLE_MANIFEST
    assert fetched.metadata.git_sha == "abc1234"


def test_publish_and_resolve_local_dir(tmp_path):
    store = open_store(str(tmp_path / "prod"))
    store.publish(_bundle())
    assert (tmp_path / "prod" / "current.tar").exists()
    assert (tmp_path / "prod" / "VERSION").read_text().strip() == "abc1234"
    assert store.resolve_latest().version == "abc1234"


def test_resolve_missing_bundle_raises(tmp_path):
    store = open_store(str(tmp_path / "empty"))
    with pytest.raises(ManifestStoreError):
        store.resolve_version()
    with pytest.raises(ManifestStoreError):
        store.resolve_latest()


# --------------------------------------------------------------------------- #
# Back-compat: a bare semantic_manifest.json path
# --------------------------------------------------------------------------- #
def test_raw_manifest_json_backcompat(tmp_path):
    raw = tmp_path / "semantic_manifest.json"
    raw.write_text(SAMPLE_MANIFEST)
    store = open_store(str(raw))
    bundle = store.resolve_latest()
    assert bundle.semantic_manifest_json == SAMPLE_MANIFEST
    assert bundle.metadata.git_sha == "unknown"  # degenerate metadata-less bundle


def test_open_store_bare_path_becomes_file_uri(tmp_path):
    store = open_store(str(tmp_path / "prod"))
    assert isinstance(store, FsspecStore)
