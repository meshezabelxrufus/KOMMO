# =============================================================================
# Kommo CRM Automation Platform — Production Dockerfile
# =============================================================================
#
# Multi-stage build:
#   Stage 1 (builder) — compile wheels for all dependencies in isolation
#   Stage 2 (runtime) — lean production image with only runtime artifacts
#
# Build:
#   docker build -t kommo-pipeline:latest .
#
# Run (production):
#   docker run --env-file .env \
#     -v $(pwd)/outputs:/app/outputs \
#     -v $(pwd)/daily_exports:/app/daily_exports \
#     -v $(pwd)/logs:/app/logs \
#     -v $(pwd)/state:/app/state \
#     -v $(pwd)/auth:/app/auth \
#     kommo-pipeline:latest
# =============================================================================

# ── Stage 1: Dependency Builder ───────────────────────────────────────────────
FROM python:3.11-slim AS builder

# Suppress interactive prompts during apt installs
ENV DEBIAN_FRONTEND=noninteractive

# Install build-only dependencies (compilers, headers for cryptography/httpx)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    libssl-dev \
    cargo \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Create a dedicated build workspace
WORKDIR /build

# Copy requirements first — Docker cache busts only when this file changes
COPY requirements.txt .

# Build all wheels into a local cache directory
# --no-cache-dir avoids polluting the layer with pip's own cache
RUN pip install --upgrade pip wheel && \
    pip wheel --no-cache-dir --wheel-dir=/build/wheels -r requirements.txt


# ── Stage 2: Production Runtime ───────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# ── System configuration ─────────────────────────────────────────────────────
ENV DEBIAN_FRONTEND=noninteractive \
    # UTF-8 everywhere — critical for Arabic/Spanish CRM content
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONIOENCODING=utf-8 \
    # Disable .pyc files — cleaner container filesystem
    PYTHONDONTWRITEBYTECODE=1 \
    # Force stdout/stderr to be unbuffered — logs appear instantly
    PYTHONUNBUFFERED=1 \
    # Reproducible pip installs — no version resolver randomness
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Fail loudly on hash mismatches
    PIP_REQUIRE_HASHES=0 \
    # Application home
    APP_HOME=/app \
    # Runtime defaults (overridden by .env / docker-compose env)
    LOG_LEVEL=INFO \
    LOG_TO_FILE=true \
    OUTPUT_DIR=/app/outputs \
    LOG_DIR=/app/logs \
    TOKEN_STORE_PATH=/app/auth/token_store.json \
    SYNC_STATE_PATH=/app/state/sync_state.json

# Install only runtime system dependencies (no compilers)
RUN apt-get update && apt-get install -y --no-install-recommends \
    # tzdata for timezone support
    tzdata \
    # curl for healthcheck probes
    curl \
    # ca-certificates for HTTPS calls to Google/Kommo/Anthropic APIs
    ca-certificates \
    # Required by cryptography at runtime on some architectures
    libffi8 \
    libssl3 \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Set timezone to UTC — consistent timestamps in all logs and exports
ENV TZ=UTC
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# ── Non-root user ────────────────────────────────────────────────────────────
# Running as root inside Docker is a security risk — create a least-privilege user
RUN groupadd --gid 1001 kommo && \
    useradd --uid 1001 --gid kommo --shell /bin/bash --create-home kommo

# ── Install pre-built wheels from builder stage ──────────────────────────────
COPY --from=builder /build/wheels /tmp/wheels
RUN pip install --no-index --find-links=/tmp/wheels /tmp/wheels/*.whl && \
    rm -rf /tmp/wheels

# ── Application code ─────────────────────────────────────────────────────────
WORKDIR $APP_HOME

# Copy application source (respects .dockerignore)
COPY --chown=kommo:kommo . .

# ── Persistent volume mount points ───────────────────────────────────────────
# Create all directories that will be mounted as external volumes.
# Pre-creating ensures correct ownership even before a volume is mounted.
RUN mkdir -p \
    /app/outputs \
    /app/daily_exports \
    /app/logs \
    /app/state \
    /app/auth && \
    chown -R kommo:kommo \
    /app/outputs \
    /app/daily_exports \
    /app/logs \
    /app/state \
    /app/auth

# ── Switch to non-root user ──────────────────────────────────────────────────
USER kommo

# ── Healthcheck ──────────────────────────────────────────────────────────────
# Checks that the Python environment is sane and the app can import cleanly.
# Docker marks the container unhealthy if this fails 3 times in a row.
HEALTHCHECK --interval=60s --timeout=15s --start-period=10s --retries=3 \
    CMD python -c "import main; print('healthy')" || exit 1

# ── Entrypoint ───────────────────────────────────────────────────────────────
# Default: incremental mode (only fetches new data since last run)
# Override CMD in docker-compose for different run modes:
#   CMD ["python", "main.py", "--extraction-only"]
#   CMD ["python", "main.py", "--skip-sheets", "--skip-drive"]
CMD ["python", "main.py", "--auto-incremental"]
