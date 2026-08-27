# =============================================================================
# Botsensai Container Image
# =============================================================================
FROM python:3.11-slim

# Install system dependencies & Chromium for Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency definition
COPY pyproject.toml .

# Install dependencies with browser & ml extras
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e ".[browser,ml]" && \
    playwright install chromium

# Copy application source
COPY src/ src/
COPY config/ config/
COPY scripts/ scripts/
COPY README.md .

# Create volume mounts for persistent data
RUN mkdir -p /app/data /app/config && \
    useradd -m -u 1000 botsensai && \
    chown -R botsensai:botsensai /app

USER botsensai

ENV PYTHONUNBUFFERED=1 \
    BOTSENSAI_DB_PATH=/app/data/botsensai.db \
    BOTSENSAI_LOG_LEVEL=INFO

EXPOSE 8080

ENTRYPOINT ["botsensai"]
CMD ["run"]
