"""FlowProxy CLI entrypoint (WS7b).

    # PR gate — validate only, never publish (offline, no warehouse):
    uv run python -m flowproxy_cli validate <project_dir>

    # Production release — validate AND publish the bundle to the store:
    uv run python -m flowproxy_cli validate <project_dir> \
        --publish s3://bank-artifacts/flowproxy/production/ \
        --git-sha "$GITHUB_SHA"

Exit code is non-zero if the gate fails, so CI blocks the deploy.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import logging
import sys
from pathlib import Path


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )


def cmd_validate(args: argparse.Namespace) -> int:
    from engine.manifest_store import Bundle, open_store
    from flowproxy_cli.validate import validate_project

    project_dir = Path(args.project_dir).resolve()
    manifest_path = Path(args.manifest) if args.manifest else project_dir / "target" / "semantic_manifest.json"
    if not manifest_path.is_file():
        print(f"ERROR: manifest not found at {manifest_path}; run `dbt parse` first", file=sys.stderr)
        return 2

    deployed_store = open_store(args.publish) if (args.publish and args.diff) else None
    report, compiler = validate_project(project_dir, manifest_path, deployed_store=deployed_store)

    # Human-readable summary.
    print("\n=== FlowProxy validation gate ===", file=sys.stderr)
    for name, status in report.saved_queries.items():
        print(f"  saved_query {name:35} {'OK' if status == 'ok' else 'FAIL: ' + status}", file=sys.stderr)
    for name, status in report.cubes.items():
        print(f"  cube        {name:35} {'OK' if status == 'ok' else 'FAIL: ' + status}", file=sys.stderr)
    for change in report.breaking_changes:
        print(f"  ⚠ breaking: {change}", file=sys.stderr)

    if not report.ok:
        print("\nGATE FAILED — deploy blocked.", file=sys.stderr)
        return 1
    print("\nGATE PASSED.", file=sys.stderr)

    if not args.publish:
        print("(validate-only; not publishing)", file=sys.stderr)
        return 0

    # Production release: build + publish the bundle.
    profiles_template = None
    if args.profiles_template:
        profiles_template = Path(args.profiles_template).read_text(encoding="utf-8")

    bundle = Bundle.build(
        manifest_path.read_text(encoding="utf-8"),
        git_sha=args.git_sha or "unknown",
        dbt_version=_ver("dbt-core"),
        metricflow_version=_ver("metricflow"),
        built_at=args.built_at or "unknown",
        profiles_template=profiles_template,
        validation_report=report.as_dict(),
    )
    store = open_store(args.publish)
    version = store.publish(bundle)
    print(f"\nPUBLISHED bundle {version} → {args.publish}", file=sys.stderr)
    return 0


def _ver(pkg: str) -> str:
    try:
        return md.version(pkg)
    except md.PackageNotFoundError:
        return "unknown"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="flowproxy", description="FlowProxy CI deploy gate")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="offline plan-only validation (+ optional publish)")
    v.add_argument("project_dir", help="dbt project dir (already `dbt parse`-d)")
    v.add_argument("--manifest", help="path to semantic_manifest.json (default: <project>/target/…)")
    v.add_argument("--publish", metavar="URI", help="publish bundle to this store URI (production release only)")
    v.add_argument("--profiles-template", help="profiles.template.yml to bundle (adapter shape, no secrets)")
    v.add_argument("--git-sha", help="git sha to stamp into the bundle")
    v.add_argument("--built-at", help="ISO-8601 build timestamp to stamp")
    v.add_argument("--diff", action="store_true", help="diff against deployed bundle for breaking changes")
    v.set_defaults(func=cmd_validate)
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    _configure_logging(getattr(args, "verbose", False))
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
