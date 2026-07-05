#!/usr/bin/env bash
# FlowProxy STORE-mode entrypoint (ADR-0016).
#
# For the demo, the "warehouse" is a DuckDB file that CI built and uploaded to
# S3 as a sidecar object, separate from the manifest bundle. This script:
#   1. downloads the DuckDB sidecar to local disk (if FLOWPROXY_DUCKDB_URI set)
#   2. exports FLOWPROXY_DUCKDB_PATH to that local file
#   3. execs the proxy, which pulls the bundle from FLOWPROXY_MANIFEST_URI and
#      serves the PostgreSQL wire protocol in STORE mode
#
# With a real networked warehouse (Snowflake/Redshift) there is no sidecar:
# unset FLOWPROXY_DUCKDB_URI and pass the warehouse creds as env vars instead;
# this script then just execs the proxy.
set -euo pipefail

log() { echo "[entrypoint] $*"; }

# The local path the DuckDB file lands at (also what the profile template reads).
#
# IMPORTANT: DuckDB derives its CATALOG (database) name from the file's
# basename. MetricFlow's compiled SQL references that catalog by name (e.g.
# finance_demo.main.fct_...), so the downloaded sidecar MUST keep the same
# basename it had when the manifest was built - finance_demo.duckdb - or every
# query fails with 'Catalog "finance_demo" does not exist'. Default the local
# path's basename to the sidecar's basename.
if [[ -z "${FLOWPROXY_DUCKDB_PATH:-}" && -n "${FLOWPROXY_DUCKDB_URI:-}" ]]; then
    FLOWPROXY_DUCKDB_PATH="/app/data/$(basename "${FLOWPROXY_DUCKDB_URI}")"
fi
: "${FLOWPROXY_DUCKDB_PATH:=/app/data/finance_demo.duckdb}"
export FLOWPROXY_DUCKDB_PATH

download_sidecar() {
    local uri="$1" dest="$2"
    mkdir -p "$(dirname "$dest")"
    case "$uri" in
        s3://*)
            if command -v aws >/dev/null 2>&1; then
                log "downloading DuckDB sidecar via aws cli: $uri"
                aws s3 cp "$uri" "$dest" --only-show-errors
            else
                log "aws cli absent; downloading sidecar via python/fsspec: $uri"
                python - "$uri" "$dest" <<'PY'
import sys, fsspec
src, dst = sys.argv[1], sys.argv[2]
with fsspec.open(src, "rb") as r, open(dst, "wb") as w:
    w.write(r.read())
PY
            fi
            ;;
        file://*|/*)
            log "copying DuckDB sidecar from local/file uri: $uri"
            python - "$uri" "$dest" <<'PY'
import sys, fsspec
src, dst = sys.argv[1], sys.argv[2]
with fsspec.open(src, "rb") as r, open(dst, "wb") as w:
    w.write(r.read())
PY
            ;;
        *)
            log "FATAL: unsupported FLOWPROXY_DUCKDB_URI scheme: $uri" >&2
            exit 1
            ;;
    esac
}

if [[ -n "${FLOWPROXY_DUCKDB_URI:-}" ]]; then
    download_sidecar "${FLOWPROXY_DUCKDB_URI}" "${FLOWPROXY_DUCKDB_PATH}"
    if [[ ! -f "${FLOWPROXY_DUCKDB_PATH}" ]]; then
        log "FATAL: DuckDB sidecar not present at ${FLOWPROXY_DUCKDB_PATH} after download" >&2
        exit 1
    fi
    log "DuckDB warehouse ready at ${FLOWPROXY_DUCKDB_PATH} ($(du -h "${FLOWPROXY_DUCKDB_PATH}" | cut -f1))"
else
    log "no FLOWPROXY_DUCKDB_URI set; assuming a networked warehouse via env creds"
fi

if [[ -z "${FLOWPROXY_MANIFEST_URI:-}" ]]; then
    log "FATAL: FLOWPROXY_MANIFEST_URI is required for STORE mode" >&2
    exit 1
fi

log "launching FlowProxy (STORE mode) on :${FLOWPROXY_PORT:-5432}, manifest=${FLOWPROXY_MANIFEST_URI}"
exec python /app/main.py
