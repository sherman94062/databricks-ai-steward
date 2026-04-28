# syntax=docker/dockerfile:1
# Multi-stage build for the databricks-ai-steward MCP server.
# Final image runs as non-root, exposes the streamable-http transport,
# and reads workspace credentials from /app/.env (or the platform's
# secret-injection mechanism).

# ---- builder ---------------------------------------------------------
# Compiles native deps (databricks-sdk pulls in cryptography wheels) into
# a venv we then copy into the slim final image.
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Install build essentials only if we end up needing them. databricks-sdk
# and our other deps publish wheels for linux-x86_64/arm64, so the slim
# image is enough on those platforms.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy lockfile + project sources. Order maximises layer cache hits:
# deps change rarely (lockfile only changes on `pip-compile`), source
# changes often.
COPY requirements.lock pyproject.toml README.md ./
COPY mcp_server ./mcp_server

# Install pinned runtime deps from the lockfile (reproducible builds),
# then install the project itself with no-deps so we don't pull
# anything outside the lock. Upgrading pip first closes
# CVE-2026-3219.
RUN pip install --upgrade pip && \
    pip install -r requirements.lock && \
    pip install --no-deps .


# ---- final -----------------------------------------------------------
FROM python:3.12-slim AS final

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8765 \
    MCP_ALLOW_EXTERNAL=1 \
    MCP_AUDIT_LOG_PATH=/var/log/databricks-ai-steward/audit.jsonl

# Non-root user for a smaller blast radius if the container is ever
# compromised. Numeric UID matches what k8s securityContext expects.
RUN groupadd --gid 10001 mcp && \
    useradd --uid 10001 --gid mcp --shell /usr/sbin/nologin --create-home mcp && \
    mkdir -p /var/log/databricks-ai-steward && \
    chown -R mcp:mcp /var/log/databricks-ai-steward

WORKDIR /app
COPY --from=builder --chown=mcp:mcp /opt/venv /opt/venv
COPY --chown=mcp:mcp mcp_server ./mcp_server
COPY --chown=mcp:mcp pyproject.toml README.md ./

USER mcp

EXPOSE 8765

# HEALTHCHECK uses the same /healthz endpoint that k8s pods configure
# as their livenessProbe. /readyz is the readiness counterpart — it
# returns 503 once shutdown is signalled so a rolling-update load
# balancer drains us before SIGKILL.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request as r; r.urlopen('http://127.0.0.1:8765/healthz', timeout=2)" \
      || exit 1

ENTRYPOINT ["python", "-m", "mcp_server.server"]
