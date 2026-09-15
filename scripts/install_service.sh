#!/usr/bin/env bash
# ============================================================================ #
# Install and enable the Botsensai systemd service on the Ubuntu EC2 instance
# Must be run with sudo on the Ubuntu instance.
# ============================================================================ #
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: This script must be run as root (or with sudo)."
    exit 1
fi

APP_DIR="/home/ubuntu/botsensai"
SERVICE_SRC="$APP_DIR/infra/systemd/botsensai.service"
SERVICE_DEST="/etc/systemd/system/botsensai.service"

if [ ! -f "$SERVICE_SRC" ]; then
    echo "Error: Source service file not found at $SERVICE_SRC"
    exit 1
fi

echo "==> Copying service unit to $SERVICE_DEST..."
cp "$SERVICE_SRC" "$SERVICE_DEST"
chmod 644 "$SERVICE_DEST"

# Ensure data directory exists with correct permissions
mkdir -p "$APP_DIR/data"
chown -R ubuntu:ubuntu "$APP_DIR/data"

# Ensure .env exists
if [ ! -f "$APP_DIR/.env" ] && [ -f "$APP_DIR/.env.production.example" ]; then
    echo "==> Creating initial .env from .env.production.example..."
    cp "$APP_DIR/.env.production.example" "$APP_DIR/.env"
    chown ubuntu:ubuntu "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
fi

echo "==> Reloading systemd daemon..."
systemctl daemon-reload

echo "==> Enabling botsensai service on boot..."
systemctl enable botsensai.service

echo "==> Starting botsensai service..."
systemctl restart botsensai.service

echo "==> Service status:"
systemctl status botsensai.service --no-pager
