"""WS7a — manifest store abstraction and deployment bundle (ADR-0012).

Separates *producing* the semantic manifest (CI, with the full dbt project) from
*consuming* it (the runtime proxy, which needs only a validated artifact). The
artifact is a **bundle**:

    flowproxy-bundle-<gitsha>.tar.zst
    ├── semantic_manifest.json     # the MetricFlow semantic graph (consumed)
    ├── manifest.json              # full dbt manifest (optional; lineage/debug)
    ├── metadata.json              # git sha, dbt version, build time, sha256, report
    └── profiles.template.yml      # adapter SHAPE only — secrets injected at runtime

Bundles carry **no secrets**. A ``ManifestStore`` resolves and fetches bundles;
the default ``FsspecStore`` speaks ``s3://``/``gs://``/``az://``/``file://``/
``https://`` through one code path, so the storage backend is a config URL, not
a rebuild.
"""

from __future__ import annotations

import abc
import hashlib
import io
import json
import logging
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from engine.exceptions import FlowProxyError

logger = logging.getLogger("flowproxy.manifest_store")

BUNDLE_SUFFIX: str = ".tar.zst"
_MANIFEST_NAME = "semantic_manifest.json"
_METADATA_NAME = "metadata.json"
_PROFILES_TEMPLATE_NAME = "profiles.template.yml"
_DBT_MANIFEST_NAME = "manifest.json"


class ManifestStoreError(FlowProxyError):
    """A store could not resolve, fetch, or verify a bundle."""

    sqlstate = "58030"  # io_error


class BundleIntegrityError(ManifestStoreError):
    """A bundle's bytes do not match its recorded sha256 — refuse to load."""

    sqlstate = "XX001"  # data_corrupted


# --------------------------------------------------------------------------- #
# Bundle model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BundleMetadata:
    """``metadata.json`` — makes each deployed artifact self-describing (ADR-0006)."""

    git_sha: str
    dbt_version: str
    metricflow_version: str
    built_at: str                       # ISO-8601, stamped by CI
    manifest_sha256: str                # sha256 of semantic_manifest.json bytes
    validation_report: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "git_sha": self.git_sha,
                "dbt_version": self.dbt_version,
                "metricflow_version": self.metricflow_version,
                "built_at": self.built_at,
                "manifest_sha256": self.manifest_sha256,
                "validation_report": self.validation_report,
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> "BundleMetadata":
        d = json.loads(raw)
        return cls(
            git_sha=d.get("git_sha", "unknown"),
            dbt_version=d.get("dbt_version", "unknown"),
            metricflow_version=d.get("metricflow_version", "unknown"),
            built_at=d.get("built_at", "unknown"),
            manifest_sha256=d.get("manifest_sha256", ""),
            validation_report=d.get("validation_report", {}),
        )


@dataclass(frozen=True)
class Bundle:
    """An in-memory deployment bundle."""

    semantic_manifest_json: str
    metadata: BundleMetadata
    profiles_template: str | None = None
    dbt_manifest_json: str | None = None

    @property
    def version(self) -> str:
        """Short, stable version id — the git sha (falls back to manifest sha)."""
        return self.metadata.git_sha if self.metadata.git_sha != "unknown" else self.metadata.manifest_sha256[:12]

    # -- integrity ---------------------------------------------------------- #
    def verify(self) -> None:
        """Raise if the manifest bytes don't match the recorded sha256."""
        actual = hashlib.sha256(self.semantic_manifest_json.encode("utf-8")).hexdigest()
        expected = self.metadata.manifest_sha256
        if expected and actual != expected:
            raise BundleIntegrityError(
                f"bundle integrity check failed: manifest sha256 {actual[:12]} "
                f"!= recorded {expected[:12]}",
                detail="The artifact may be corrupted or tampered with; re-publish from CI.",
            )
        logger.info("bundle %s integrity OK (sha256=%s)", self.version, actual[:12])

    # -- (de)serialization -------------------------------------------------- #
    def to_tar_bytes(self) -> bytes:
        """Pack the bundle into a tar. (Plain tar; the ``.tar.zst`` name is a
        convention — compression is handled by the store/transport layer.)"""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            _add(tar, _MANIFEST_NAME, self.semantic_manifest_json)
            _add(tar, _METADATA_NAME, self.metadata.to_json())
            if self.profiles_template is not None:
                _add(tar, _PROFILES_TEMPLATE_NAME, self.profiles_template)
            if self.dbt_manifest_json is not None:
                _add(tar, _DBT_MANIFEST_NAME, self.dbt_manifest_json)
        return buf.getvalue()

    @classmethod
    def from_tar_bytes(cls, data: bytes) -> "Bundle":
        members: dict[str, str] = {}
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            for m in tar.getmembers():
                if m.isfile():
                    f = tar.extractfile(m)
                    if f is not None:
                        members[Path(m.name).name] = f.read().decode("utf-8")
        if _MANIFEST_NAME not in members or _METADATA_NAME not in members:
            raise ManifestStoreError(
                f"bundle is missing {_MANIFEST_NAME} or {_METADATA_NAME}",
                detail="The artifact is not a valid FlowProxy bundle.",
            )
        bundle = cls(
            semantic_manifest_json=members[_MANIFEST_NAME],
            metadata=BundleMetadata.from_json(members[_METADATA_NAME]),
            profiles_template=members.get(_PROFILES_TEMPLATE_NAME),
            dbt_manifest_json=members.get(_DBT_MANIFEST_NAME),
        )
        bundle.verify()
        return bundle

    @classmethod
    def build(
        cls,
        semantic_manifest_json: str,
        *,
        git_sha: str,
        dbt_version: str,
        metricflow_version: str,
        built_at: str,
        profiles_template: str | None = None,
        dbt_manifest_json: str | None = None,
        validation_report: dict[str, Any] | None = None,
    ) -> "Bundle":
        """Construct a bundle, computing the manifest sha256 (used by CI)."""
        sha = hashlib.sha256(semantic_manifest_json.encode("utf-8")).hexdigest()
        meta = BundleMetadata(
            git_sha=git_sha,
            dbt_version=dbt_version,
            metricflow_version=metricflow_version,
            built_at=built_at,
            manifest_sha256=sha,
            validation_report=validation_report or {},
        )
        return cls(
            semantic_manifest_json=semantic_manifest_json,
            metadata=meta,
            profiles_template=profiles_template,
            dbt_manifest_json=dbt_manifest_json,
        )


def _add(tar: tarfile.TarFile, name: str, text: str) -> None:
    data = text.encode("utf-8")
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mtime = 0  # deterministic tar → reproducible bundle bytes
    tar.addfile(info, io.BytesIO(data))


# --------------------------------------------------------------------------- #
# Store abstraction
# --------------------------------------------------------------------------- #
class ManifestStore(abc.ABC):
    """Resolves and fetches deployment bundles from a backing store."""

    @abc.abstractmethod
    def resolve_latest(self) -> Bundle:
        """Fetch the current production bundle."""

    @abc.abstractmethod
    def resolve_version(self) -> str:
        """Return the version id of the current production bundle WITHOUT
        downloading it — cheap enough to poll (ADR-0014)."""

    @abc.abstractmethod
    def publish(self, bundle: Bundle) -> str:
        """Store a bundle and point production at it. Returns its version id."""


class FsspecStore(ManifestStore):
    """Object/file/HTTP store via fsspec — one backend for every scheme.

    ``uri`` may be a directory (``s3://bucket/flowproxy/production/``) — the
    store reads/writes ``current.tar`` there and a ``VERSION`` pointer — or a
    direct bundle path. Storage credentials come from the environment /
    instance role (fsspec's own config), never from FlowProxy.
    """

    def __init__(self, uri: str, *, storage_options: dict[str, Any] | None = None) -> None:
        import fsspec  # local import keeps fsspec optional for dry-run installs

        self._uri = uri.rstrip("/")
        self._is_dir = not uri.endswith((".tar", BUNDLE_SUFFIX, ".json"))
        self._fs, _, _ = fsspec.get_fs_token_paths(uri, storage_options=storage_options or {})
        self._storage_options = storage_options or {}

    # Paths within a directory-style URI.
    @property
    def _bundle_path(self) -> str:
        return f"{self._uri}/current.tar" if self._is_dir else self._uri

    @property
    def _version_path(self) -> str:
        return f"{self._uri}/VERSION" if self._is_dir else f"{self._uri}.VERSION"

    def resolve_version(self) -> str:
        try:
            with self._fs.open(self._version_path, "rt") as f:
                return f.read().strip()
        except FileNotFoundError:
            # No pointer yet — fall back to the bundle's mtime as a version.
            try:
                info = self._fs.info(self._bundle_path)
                return str(info.get("mtime", info.get("LastModified", "0")))
            except FileNotFoundError as exc:
                raise ManifestStoreError(
                    f"no bundle found at {self._bundle_path}",
                    detail="Publish a bundle from CI first, or check FLOWPROXY_MANIFEST_URI.",
                ) from exc

    def resolve_latest(self) -> Bundle:
        path = self._bundle_path
        logger.info("fetching bundle from %s", path)
        try:
            with self._fs.open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError as exc:
            raise ManifestStoreError(
                f"no bundle found at {path}",
                detail="Publish a bundle from CI first, or check FLOWPROXY_MANIFEST_URI.",
            ) from exc

        # A directory pointing at a raw semantic_manifest.json (dev/back-compat)
        # is treated as a degenerate, single-file, metadata-less bundle.
        if path.endswith(".json"):
            return _bundle_from_raw_manifest(data.decode("utf-8"))
        return Bundle.from_tar_bytes(data)

    def publish(self, bundle: Bundle) -> str:
        version = bundle.version
        data = bundle.to_tar_bytes()
        parent = self._uri if self._is_dir else str(Path(self._bundle_path).parent)
        try:
            self._fs.makedirs(parent, exist_ok=True)
        except (NotImplementedError, FileExistsError):
            pass
        with self._fs.open(self._bundle_path, "wb") as f:
            f.write(data)
        with self._fs.open(self._version_path, "wt") as f:
            f.write(version)
        logger.info("published bundle %s to %s (%d bytes)", version, self._bundle_path, len(data))
        return version


def _bundle_from_raw_manifest(manifest_json: str) -> Bundle:
    """Wrap a bare ``semantic_manifest.json`` as a metadata-less dev bundle."""
    sha = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
    logger.warning(
        "loading a raw semantic_manifest.json (no bundle metadata); "
        "dev/back-compat mode — CI should publish a full bundle"
    )
    return Bundle(
        semantic_manifest_json=manifest_json,
        metadata=BundleMetadata(
            git_sha="unknown",
            dbt_version="unknown",
            metricflow_version="unknown",
            built_at="unknown",
            manifest_sha256=sha,
        ),
    )


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def open_store(uri: str, *, storage_options: dict[str, Any] | None = None) -> ManifestStore:
    """Open a ManifestStore for a URI (any fsspec scheme, or a local path).

    A bare local path (no scheme) is treated as ``file://``, so
    ``FLOWPROXY_MANIFEST=/path/to/semantic_manifest.json`` keeps working.
    """
    if "://" not in uri:
        uri = "file://" + str(Path(uri).resolve())
    return FsspecStore(uri, storage_options=storage_options)
