# ===============================================================
# Botsensai FastAPI & Sentinel Production Cloud Run Container
# ===============================================================

FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Security hardening: Create non-root user and persistent dirs
RUN useradd -m -u 1000 appuser && \
    mkdir -p /app/data && \
    chown -R appuser:appuser /app

# Copy dependency manifests and install dependencies
COPY --chown=appuser:appuser pyproject.toml README.md ./
RUN pip install --no-cache-dir hatchling && pip install --no-cache-dir .

# Copy application source code, static assets, and data
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser public/ ./public/
COPY --chown=appuser:appuser data/ ./data/

# Ensure package is installed with latest source
RUN pip install --no-cache-dir --no-deps .

# Cloud Run defaults
ENV PORT=8080 \
    HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1 \
    BOTSENSAI_REPO_ROOT=/app \
    BOTSENSAI_DB_PATH=/app/data/botsensai.db

EXPOSE 8080

USER appuser

# Healthcheck for container readiness & Cloud Run liveness
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["uvicorn", "botsensai.dashboard.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
