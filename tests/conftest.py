"""Shared pytest fixtures for the FlowProxy test suite.

The `finance_demo` dbt project is built once per session into a DuckDB file,
then a real `SemanticCompiler` (non-dry-run) is bootstrapped against it. L2
(manifest) and L3 (golden-numbers) tests share that engine.

Building dbt requires the `duckdb` extra; if dbt-duckdb is unavailable the
warehouse-backed tests skip rather than error, so the wire-only suite still
runs in a minimal environment.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_DIR = REPO_ROOT / "test_projects" / "finance_demo"
DUCKDB_PATH = PROJECT_DIR / "target" / "finance_demo.duckdb"
MANIFEST_PATH = PROJECT_DIR / "target" / "semantic_manifest.json"

# Air-gap hygiene for every subprocess dbt invocation (ADR-0002).
_DBT_ENV = {
    **os.environ,
    "DO_NOT_TRACK": "1",
    "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    # Absolute DuckDB path so the adapter resolves it regardless of cwd.
    "FLOWPROXY_DUCKDB_PATH": str(DUCKDB_PATH),
}


def _dbt(*args: str) -> None:
    """Run a dbt command inside the fixture project; surface output on failure."""
    proc = subprocess.run(
        [sys.executable, "-m", "dbt.cli.main", *args, "--profiles-dir", "."],
        cwd=PROJECT_DIR,
        env=_DBT_ENV,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # dbt writes diagnostics to stdout, not stderr — combine both.
        raise RuntimeError(
            f"`dbt {' '.join(args)}` failed (exit {proc.returncode}):\n"
            f"{proc.stdout}\n{proc.stderr}"
        )


def _duckdb_available() -> bool:
    try:
        import dbt.adapters.duckdb  # noqa: F401

        return True
    except ImportError:
        return False


@pytest.fixture(scope="session")
def finance_demo_built() -> Path:
    """Build the fixture project (seed + models + parse) once. Returns project dir."""
    if not _duckdb_available():
        pytest.skip("dbt-duckdb not installed; install the `duckdb` extra to run warehouse tests")
    try:
        _dbt("build")
        _dbt("parse")
    except RuntimeError as exc:  # surface dbt's own diagnostics
        pytest.fail(str(exc))
    assert MANIFEST_PATH.is_file(), "semantic_manifest.json was not produced"
    return PROJECT_DIR


@pytest.fixture(scope="session")
def compiler(finance_demo_built: Path):
    """A real (non-dry-run) SemanticCompiler bound to the fixture warehouse."""
    os.environ.setdefault("FLOWPROXY_DUCKDB_PATH", str(DUCKDB_PATH))
    from engine.compiler import SemanticCompiler

    return SemanticCompiler(MANIFEST_PATH, finance_demo_built, dry_run=False)


# Make `engine`, `network`, `executor` importable when pytest runs from repo root.
sys.path.insert(0, str(REPO_ROOT))
