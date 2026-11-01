# syntax=docker/dockerfile:1.9
# ==============================================================================
# Stage 1: Builder — Install dependencies with Astral uv on Alpine Linux
# ==============================================================================
FROM ghcr.io/astral-sh/uv:0.6-python3.12-alpine AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install build dependencies only if native wheels are unavailable on musl
RUN apk add --no-cache gcc musl-dev libffi-dev

# 1. Install production dependencies into /app/.venv using cached layer
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-install-project --no-dev --no-editable

# 2. Copy only runtime application packages
COPY config/ /app/config/
COPY shared/ /app/shared/
COPY ibkr_worker/ /app/ibkr_worker/
COPY tr_worker/ /app/tr_worker/

# Strip package caches/tests inside virtualenv to minimize image footprint
RUN find /app/.venv -type d \( -name "tests" -o -name "test" \) -exec rm -rf {} + 2>/dev/null || true

# ==============================================================================
# Stage 2: Runtime — Ultra-minimal Python 3.12 Alpine image (~55-65 MB)
# ==============================================================================
FROM python:3.12-alpine AS runtime

LABEL org.opencontainers.image.title="finance-sync" \
      org.opencontainers.image.description="IBKR & Trade Republic to BudgetBakers Wallet Sync Service" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="/app" \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Remove pip/setuptools/wheel/ensurepip from base Python to shrink image & attack surface,
# and create unprivileged user (UID 10001) + persistent data directory
RUN rm -rf /usr/local/lib/python3.12/ensurepip \
           /usr/local/lib/python3.12/site-packages/pip* \
           /usr/local/lib/python3.12/site-packages/setuptools* \
           /usr/local/lib/python3.12/site-packages/wheel* && \
    addgroup -g 10001 -S appgroup && \
    adduser -u 10001 -S -D -H -G appgroup -s /sbin/nologin appuser && \
    mkdir -p /app/data && \
    chown -R appuser:appgroup /app

COPY --from=builder --chown=appuser:appgroup /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appgroup /app/config /app/config
COPY --from=builder --chown=appuser:appgroup /app/shared /app/shared
COPY --from=builder --chown=appuser:appgroup /app/ibkr_worker /app/ibkr_worker
COPY --from=builder --chown=appuser:appgroup /app/tr_worker /app/tr_worker

USER appuser

VOLUME ["/app/data"]

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sqlite3, os; sqlite3.connect(os.environ.get('SQLITE_DB_PATH', '/app/data/finance_sync.db')).execute('SELECT 1')"]

CMD ["python", "-m", "ibkr_worker.worker"]
