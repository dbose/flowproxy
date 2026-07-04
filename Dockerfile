# syntax=docker/dockerfile:1.7
###############################################################################
# Stage 1 — builder: compile wheels into an isolated virtualenv
###############################################################################
FROM python:3.11-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build toolchain for any adapter wheels that compile native extensions.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev git \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip && pip install -r /tmp/requirements.txt

###############################################################################
# Stage 2 — runtime: slim, non-root, CONSUMES a bundle from the ManifestStore
#
# Production runtime does NOT run `dbt parse` and needs no checked-out dbt
# project — it pulls a validated bundle from FLOWPROXY_MANIFEST_URI and builds a
# synthetic skeleton internally (WS7d/ADR-0012). Warehouse SELECT creds come
# from the environment / secret store (ADR-0015). For local dev, unset
# FLOWPROXY_MANIFEST_URI and mount a project to use DIRECT mode.
###############################################################################
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Air-gap hygiene: never phone home (ADR-0002).
    DO_NOT_TRACK=1 \
    DBT_SEND_ANONYMOUS_USAGE_STATS=false \
    FLOWPROXY_HOST=0.0.0.0 \
    FLOWPROXY_PORT=5432 \
    # Store mode: point at the bundle store, e.g. s3://…/production/ (ADR-0012).
    # FLOWPROXY_MANIFEST_URI must be provided at deploy time.
    FLOWPROXY_MANIFEST_POLL_INTERVAL=300

RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 postgresql-client \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --create-home --home-dir /home/flowproxy flowproxy

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=flowproxy:flowproxy engine/ ./engine/
COPY --chown=flowproxy:flowproxy network/ ./network/
COPY --chown=flowproxy:flowproxy executor/ ./executor/
COPY --chown=flowproxy:flowproxy mcpserver/ ./mcpserver/
COPY --chown=flowproxy:flowproxy flowproxy_cli/ ./flowproxy_cli/
COPY --chown=flowproxy:flowproxy main.py ./
RUN chown -R flowproxy:flowproxy /app

USER flowproxy
EXPOSE 5432

# pg_isready speaks the real startup protocol — a true end-to-end liveness probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD pg_isready -h 127.0.0.1 -p 5432 -U healthcheck || exit 1

# Consume-from-store: no dbt parse, no project mount. main.py chooses STORE mode
# when FLOWPROXY_MANIFEST_URI is set, else DIRECT mode (local dev).
ENTRYPOINT ["python", "/app/main.py"]
