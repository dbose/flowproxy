"""WS7b — the CI deploy gate: offline, plan-only validation (ADR-0013).

Given a dbt project (already `dbt parse`-d), this:
  1. loads the semantic manifest via a real (adapter-backed) compiler;
  2. **plans** — does not execute — every saved query and every auto-cube,
     catching broken join paths / renamed dimensions / missing metrics;
  3. (optional) diffs against a currently-deployed bundle for breaking changes;
  4. builds a deployment bundle with a validation report and returns it.

Planning uses MetricFlow `explain`, which renders SQL without touching the
warehouse — so the gate runs anywhere CI runs, with no warehouse creds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from engine.catalog import CatalogBuilder
from engine.compiler import SemanticCompiler
from engine.exceptions import FlowProxyError
from engine.manifest_store import Bundle, ManifestStore

logger = logging.getLogger("flowproxy.cli.validate")


@dataclass
class ValidationReport:
    """Per-object plan results — embedded in the bundle's metadata.json."""

    saved_queries: dict[str, str] = field(default_factory=dict)   # name -> "ok" | error
    cubes: dict[str, str] = field(default_factory=dict)           # name -> "ok" | error
    breaking_changes: list[str] = field(default_factory=list)
    ok: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "saved_queries": self.saved_queries,
            "cubes": self.cubes,
            "breaking_changes": self.breaking_changes,
        }


def validate_project(
    project_dir: Path,
    manifest_path: Path,
    *,
    deployed_store: ManifestStore | None = None,
) -> tuple[ValidationReport, SemanticCompiler]:
    """Run the plan-only gate. Returns (report, compiler-for-catalog-metadata)."""
    compiler = SemanticCompiler(manifest_path, project_dir, dry_run=False)
    report = ValidationReport()

    # 1. Plan every saved query.
    for name in compiler.saved_query_names():
        try:
            compiler.plan_saved_query(name)
            report.saved_queries[name] = "ok"
            logger.info("saved query %r: OK", name)
        except FlowProxyError as exc:
            report.saved_queries[name] = exc.message
            report.ok = False
            logger.error("saved query %r: FAILED — %s", name, exc.message)

    # 2. Plan every auto-cube (a representative query per cube's metrics).
    catalog = CatalogBuilder(compiler, manifest_sha="validate").build()
    for table in catalog.tables:
        metric_cols = [c.semantic_name for c in table.columns if c.is_metric]
        dim_cols = [c.semantic_name for c in table.columns if not c.is_metric]
        if not metric_cols:
            continue
        # A representative slice: all the cube's metrics by one dimension
        # (or metric-only if the cube has none).
        group_by = dim_cols[:1]
        try:
            compiler.plan_only(metric_cols, group_by)
            report.cubes[table.name] = "ok"
            logger.info("cube %r: OK", table.name)
        except FlowProxyError as exc:
            report.cubes[table.name] = exc.message
            report.ok = False
            logger.error("cube %r: FAILED — %s", table.name, exc.message)

    # 3. Breaking-change diff vs the currently-deployed bundle (advisory).
    if deployed_store is not None:
        try:
            deployed = deployed_store.resolve_latest()
            report.breaking_changes = _diff_breaking(deployed, compiler)
            for change in report.breaking_changes:
                logger.warning("breaking change vs deployed: %s", change)
        except FlowProxyError as exc:
            logger.info("no deployed bundle to diff against (%s)", exc.message)

    return report, compiler


def _diff_breaking(deployed: Bundle, new_compiler: SemanticCompiler) -> list[str]:
    """Metrics present in the deployed manifest but missing from the new one."""
    import json

    old = json.loads(deployed.semantic_manifest_json)
    old_metrics = {m["name"] for m in old.get("metrics", [])}
    new_metrics = new_compiler.registry.metrics
    removed = sorted(old_metrics - new_metrics)
    return [f"metric removed: {m}" for m in removed]
