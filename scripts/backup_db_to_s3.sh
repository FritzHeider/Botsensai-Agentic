#!/usr/bin/env bash
# ============================================================================ #
# Safely snapshot SQLite data/botsensai.db and upload to Amazon S3
#
# Usage:
#   ./scripts/backup_db_to_s3.sh [BUCKET_NAME]
#   ./scripts/backup_db_to_s3.sh --install-cron [BUCKET_NAME]
# ============================================================================ #
set -euo pipefail

APP_DIR="${BOTSENSAI_DIR:-/home/ubuntu/botsensai}"
DB_PATH="${BOTSENSAI_DB_PATH:-$APP_DIR/data/botsensai.db}"

# Check for install-cron mode
if [ "${1:-}" = "--install-cron" ]; then
    shift
    BUCKET="${1:-${BOTSENSAI_BACKUP_S3_BUCKET:-}}"
    if [ -z "$BUCKET" ]; then
        echo "Error: S3 bucket name is required to configure cron."
        echo "Usage: $0 --install-cron <BUCKET_NAME>"
        exit 1
    fi

    CRON_CMD="0 */6 * * * /home/ubuntu/botsensai/scripts/backup_db_to_s3.sh $BUCKET >> /tmp/botsensai_backup.log 2>&1"
    (crontab -l 2>/dev/null | grep -Fv "backup_db_to_s3.sh" ; echo "$CRON_CMD") | crontab -
    echo "==> Cron job installed: every 6 hours backup to s3://$BUCKET/backups/"
    crontab -l | grep "backup_db_to_s3.sh"
    exit 0
fi

BUCKET="${1:-${BOTSENSAI_BACKUP_S3_BUCKET:-}}"
if [ -z "$BUCKET" ]; then
    echo "Error: S3 bucket name not provided and BOTSENSAI_BACKUP_S3_BUCKET is empty."
    echo "Usage: $0 [BUCKET_NAME]"
    exit 1
fi

if [ ! -f "$DB_PATH" ]; then
    echo "Notice: Database $DB_PATH does not exist yet. Skipping backup."
    exit 0
fi

TIMESTAMP=$(date -u +"%Y%m%d_%H%M%SZ")
BACKUP_TMP="/tmp/botsensai_${TIMESTAMP}.db"
S3_TARGET="s3://${BUCKET}/backups/botsensai_${TIMESTAMP}.db"

echo "==> Performing atomic online SQLite backup of $DB_PATH..."
sqlite3 "$DB_PATH" ".backup '$BACKUP_TMP'"

echo "==> Uploading snapshot to $S3_TARGET..."
aws s3 cp "$BACKUP_TMP" "$S3_TARGET"

echo "==> Cleaning up temporary file $BACKUP_TMP..."
rm -f "$BACKUP_TMP"

echo "==> Backup complete: $S3_TARGET"
