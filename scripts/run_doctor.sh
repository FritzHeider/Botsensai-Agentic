#!/usr/bin/env bash
# ============================================================================ #
# Run Botsensai health check / diagnostics on the instance
# ============================================================================ #
set -euo pipefail

APP_DIR="${BOTSENSAI_DIR:-/home/ubuntu/botsensai}"
cd "$APP_DIR"

if [ -f "$APP_DIR/.venv/bin/activate" ]; then
    source "$APP_DIR/.venv/bin/activate"
fi

if [ -f "$APP_DIR/.env" ]; then
    set -a
    source "$APP_DIR/.env"
    set +a
fi

echo "==> Running Botsensai doctor diagnostics..."
python3 -m botsensai.cli doctor --config config/botsensai.yaml
