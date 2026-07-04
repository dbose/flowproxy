#!/usr/bin/env bash
# FlowProxy boot orchestration:
#   1. `dbt parse` regenerates target/semantic_manifest.json from the
#      MetricFlow YAML configs mounted at $DBT_PROJECT_DIR
#   2. launch the async PostgreSQL wire-protocol proxy
set -euo pipefail

echo "[entrypoint] flowproxy starting (project=${DBT_PROJECT_DIR})"

if [[ -f "${DBT_PROJECT_DIR}/dbt_project.yml" ]]; then
    echo "[entrypoint] running dbt parse to build the semantic manifest..."
    (cd "${DBT_PROJECT_DIR}" && dbt parse --profiles-dir "${DBT_PROFILES_DIR}")
    echo "[entrypoint] manifest ready: ${FLOWPROXY_MANIFEST}"
else
    echo "[entrypoint] WARNING: no dbt_project.yml at ${DBT_PROJECT_DIR};" \
         "expecting a prebuilt manifest at ${FLOWPROXY_MANIFEST}" >&2
fi

if [[ ! -f "${FLOWPROXY_MANIFEST}" ]]; then
    echo "[entrypoint] FATAL: semantic manifest not found at ${FLOWPROXY_MANIFEST}" >&2
    exit 1
fi

echo "[entrypoint] launching PostgreSQL wire-protocol proxy on :${FLOWPROXY_PORT}"
exec python /app/main.py
