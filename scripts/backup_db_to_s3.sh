#!/usr/bin/env bash
# ============================================================================ #
# Multi-Cloud Database Backup (AWS S3 + Cloudflare R2 / Backblaze B2)
#
# Safely snapshot SQLite data/botsensai.db and mirror snapshots across:
#   1. AWS S3 (Operational redundancy)
#   2. Cloudflare R2 (S3-compatible, ZERO egress bandwidth fees)
#   3. Backblaze B2 (Optional secondary S3-compatible store)
#
# Usage:
#   ./scripts/backup_db_to_s3.sh [AWS_S3_BUCKET]
#   ./scripts/backup_db_to_s3.sh --install-cron [AWS_S3_BUCKET]
#   ./scripts/backup_db_to_s3.sh --dry-run
#
# Environment variables:
#   AWS S3:
#     BOTSENSAI_BACKUP_S3_BUCKET / AWS_S3_BUCKET
#     AWS_REGION, AWS_PROFILE
#   Cloudflare R2 (Zero Egress):
#     BOTSENSAI_BACKUP_R2_BUCKET / R2_BUCKET
#     BOTSENSAI_BACKUP_R2_ACCOUNT_ID / R2_ACCOUNT_ID
#     BOTSENSAI_BACKUP_R2_ENDPOINT_URL / R2_ENDPOINT_URL
#     BOTSENSAI_BACKUP_R2_ACCESS_KEY_ID / R2_ACCESS_KEY_ID
#     BOTSENSAI_BACKUP_R2_SECRET_ACCESS_KEY / R2_SECRET_ACCESS_KEY
#   Backblaze B2:
#     BOTSENSAI_BACKUP_B2_BUCKET / B2_BUCKET
#     BOTSENSAI_BACKUP_B2_ENDPOINT_URL / B2_ENDPOINT_URL
#     BOTSENSAI_BACKUP_B2_ACCESS_KEY_ID / B2_ACCESS_KEY_ID
#     BOTSENSAI_BACKUP_B2_SECRET_ACCESS_KEY / B2_SECRET_ACCESS_KEY
# ============================================================================ #
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR="${BOTSENSAI_DIR:-$REPO_DIR}"
DB_PATH="${BOTSENSAI_DB_PATH:-$APP_DIR/data/botsensai.db}"

# Source .env if present
if [ -f "$APP_DIR/.env" ]; then
    # shellcheck disable=SC1091
    set -a
    source "$APP_DIR/.env"
    set +a
fi

DRY_RUN=false
INSTALL_CRON=false
CLI_S3_BUCKET=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --install-cron)
            INSTALL_CRON=true
            shift
            ;;
        --r2-bucket)
            BOTSENSAI_BACKUP_R2_BUCKET="$2"
            shift 2
            ;;
        --r2-account-id)
            BOTSENSAI_BACKUP_R2_ACCOUNT_ID="$2"
            shift 2
            ;;
        --r2-endpoint)
            BOTSENSAI_BACKUP_R2_ENDPOINT_URL="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS] [S3_BUCKET]"
            echo ""
            echo "Options:"
            echo "  --dry-run              Simulate snapshot and mirror uploads without executing"
            echo "  --install-cron [BUCKET] Install 6-hour cron job on current user's crontab"
            echo "  --r2-bucket BUCKET     Cloudflare R2 bucket name"
            echo "  --r2-account-id ID     Cloudflare account ID"
            echo "  --r2-endpoint URL      Cloudflare R2 endpoint URL"
            echo "  --help, -h             Show this help message"
            exit 0
            ;;
        *)
            if [ -z "$CLI_S3_BUCKET" ]; then
                CLI_S3_BUCKET="$1"
            fi
            shift
            ;;
    esac
done

S3_BUCKET="${CLI_S3_BUCKET:-${BOTSENSAI_BACKUP_S3_BUCKET:-${AWS_S3_BUCKET:-}}}"
R2_BUCKET="${BOTSENSAI_BACKUP_R2_BUCKET:-${R2_BUCKET:-}}"
R2_ACCOUNT_ID="${BOTSENSAI_BACKUP_R2_ACCOUNT_ID:-${R2_ACCOUNT_ID:-${CLOUDFLARE_ACCOUNT_ID:-}}}"
R2_ENDPOINT="${BOTSENSAI_BACKUP_R2_ENDPOINT_URL:-${R2_ENDPOINT_URL:-}}"
R2_REGION="${BOTSENSAI_BACKUP_R2_REGION:-${R2_REGION:-auto}}"

# Fallback to reading config if S3/R2 not explicitly set
if [ -z "$S3_BUCKET" ] && [ -z "$R2_BUCKET" ] && [ -f "$APP_DIR/config/botsensai.yaml" ]; then
    CFG_S3=$(python3 -c "from botsensai.config import load_settings; print(load_settings().backup.s3.resolved_bucket or '')" 2>/dev/null || true)
    CFG_R2=$(python3 -c "from botsensai.config import load_settings; print(load_settings().backup.r2.resolved_bucket or '')" 2>/dev/null || true)
    CFG_R2_EP=$(python3 -c "from botsensai.config import load_settings; print(load_settings().backup.r2.resolved_endpoint or '')" 2>/dev/null || true)
    CFG_R2_REG=$(python3 -c "from botsensai.config import load_settings; print(load_settings().backup.r2.resolved_region or 'auto')" 2>/dev/null || true)
    S3_BUCKET="${S3_BUCKET:-$CFG_S3}"
    R2_BUCKET="${R2_BUCKET:-$CFG_R2}"
    if [ -z "$R2_ENDPOINT" ] && [ -n "$CFG_R2_EP" ]; then
        R2_ENDPOINT="$CFG_R2_EP"
    fi
    if [ -n "$CFG_R2_REG" ]; then
        R2_REGION="$CFG_R2_REG"
    fi
fi

if [ -z "$R2_ENDPOINT" ] && [ -n "$R2_ACCOUNT_ID" ]; then
    R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
fi

B2_BUCKET="${BOTSENSAI_BACKUP_B2_BUCKET:-${B2_BUCKET:-}}"
B2_ENDPOINT="${BOTSENSAI_BACKUP_B2_ENDPOINT_URL:-${B2_ENDPOINT_URL:-}}"
B2_REGION="${BOTSENSAI_BACKUP_B2_REGION:-${B2_REGION:-}}"
if [ -z "$B2_ENDPOINT" ] && [ -n "$B2_REGION" ]; then
    B2_ENDPOINT="https://s3.${B2_REGION}.backblazeb2.com"
fi

# Check for install-cron mode
if [ "$INSTALL_CRON" = true ]; then
    if [ -z "$S3_BUCKET" ] && [ -z "$R2_BUCKET" ]; then
        echo "Error: At least one bucket (AWS S3 or Cloudflare R2) is required to configure cron."
        echo "Usage: $0 --install-cron <S3_BUCKET>"
        exit 1
    fi

    SCRIPT_PATH="$APP_DIR/scripts/backup_db_to_s3.sh"
    CRON_CMD="0 */6 * * * /bin/bash $SCRIPT_PATH ${S3_BUCKET:-} >> /tmp/botsensai_backup.log 2>&1"
    (crontab -l 2>/dev/null | grep -Fv "backup_db_to_s3.sh" ; echo "$CRON_CMD") | crontab -
    echo "==> Cron job installed: every 6 hours mirroring snapshots"
    crontab -l | grep "backup_db_to_s3.sh"
    exit 0
fi

if [ -z "$S3_BUCKET" ] && [ -z "$R2_BUCKET" ] && [ -z "$B2_BUCKET" ]; then
    echo "Error: No cloud destinations configured."
    echo "Please provide an S3 bucket or set BOTSENSAI_BACKUP_S3_BUCKET / BOTSENSAI_BACKUP_R2_BUCKET."
    exit 1
fi

if [ ! -f "$DB_PATH" ]; then
    echo "Notice: Database $DB_PATH does not exist yet. Skipping backup."
    exit 0
fi

TIMESTAMP=$(date -u +"%Y%m%d_%H%M%SZ")
BACKUP_TMP="/tmp/botsensai_${TIMESTAMP}.db"

echo "======================================================================"
echo "  Botsensai Multi-Cloud Database Backup"
echo "  Source : $DB_PATH"
echo "  Time   : $TIMESTAMP"
echo "======================================================================"

# Step 1: Create atomic online snapshot
echo "==> [1/4] Performing online atomic SQLite snapshot..."
if [ "$DRY_RUN" = true ]; then
    echo "    (Dry-run: skipping actual snapshot creation)"
else
    if command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "$DB_PATH" ".backup '$BACKUP_TMP'"
    else
        python3 -c "
import sqlite3
src = sqlite3.connect('$DB_PATH')
dst = sqlite3.connect('$BACKUP_TMP')
src.backup(dst, pages=100)
dst.close()
src.close()
"
    fi
    SIZE=$(ls -lh "$BACKUP_TMP" | awk '{print $5}')
    echo "    Created local snapshot $BACKUP_TMP ($SIZE)"
fi

ERRORS=0

# Step 2: Upload to Amazon S3 (if configured)
if [ -n "$S3_BUCKET" ]; then
    S3_TARGET="s3://${S3_BUCKET}/backups/botsensai_${TIMESTAMP}.db"
    S3_LATEST="s3://${S3_BUCKET}/latest/botsensai.db"
    echo "==> [2/4] Mirroring to Amazon S3: $S3_TARGET"
    if [ "$DRY_RUN" = true ]; then
        echo "    (Dry-run) aws s3 cp $BACKUP_TMP $S3_TARGET"
        echo "    (Dry-run) aws s3 cp $BACKUP_TMP $S3_LATEST"
    else
        PROFILE_ARG=()
        if [ -n "${AWS_PROFILE:-}" ]; then
            PROFILE_ARG=(--profile "$AWS_PROFILE")
        fi
        if aws s3 cp "$BACKUP_TMP" "$S3_TARGET" "${PROFILE_ARG[@]}"; then
            aws s3 cp "$BACKUP_TMP" "$S3_LATEST" "${PROFILE_ARG[@]}" >/dev/null 2>&1 || true
            echo "    ✓ Amazon S3 upload succeeded"
        else
            echo "    ✗ Amazon S3 upload failed" >&2
            ERRORS=$((ERRORS + 1))
        fi
    fi
else
    echo "==> [2/4] Amazon S3: skipped (not configured)"
fi

# Step 3: Upload to Cloudflare R2 (if configured, Zero Egress)
if [ -n "$R2_BUCKET" ] && [ -n "$R2_ENDPOINT" ]; then
    R2_TARGET="s3://${R2_BUCKET}/backups/botsensai_${TIMESTAMP}.db"
    R2_LATEST="s3://${R2_BUCKET}/latest/botsensai.db"
    echo "==> [3/4] Mirroring to Cloudflare R2 (Zero Egress): $R2_TARGET"
    if [ "$DRY_RUN" = true ]; then
        echo "    (Dry-run) aws s3 cp $BACKUP_TMP $R2_TARGET --endpoint-url $R2_ENDPOINT --region $R2_REGION"
        echo "    (Dry-run) aws s3 cp $BACKUP_TMP $R2_LATEST --endpoint-url $R2_ENDPOINT --region $R2_REGION"
    else
        R2_ENV=()
        if [ -n "${BOTSENSAI_BACKUP_R2_ACCESS_KEY_ID:-${R2_ACCESS_KEY_ID:-}}" ]; then
            R2_ENV+=(AWS_ACCESS_KEY_ID="${BOTSENSAI_BACKUP_R2_ACCESS_KEY_ID:-${R2_ACCESS_KEY_ID}}")
        fi
        if [ -n "${BOTSENSAI_BACKUP_R2_SECRET_ACCESS_KEY:-${R2_SECRET_ACCESS_KEY:-}}" ]; then
            R2_ENV+=(AWS_SECRET_ACCESS_KEY="${BOTSENSAI_BACKUP_R2_SECRET_ACCESS_KEY:-${R2_SECRET_ACCESS_KEY}}")
        fi
        R2_ENV+=(AWS_DEFAULT_REGION="$R2_REGION")
        R2_ENV+=(AWS_REGION="$R2_REGION")

        if env "${R2_ENV[@]}" aws s3 cp "$BACKUP_TMP" "$R2_TARGET" --endpoint-url "$R2_ENDPOINT" --region "$R2_REGION"; then
            env "${R2_ENV[@]}" aws s3 cp "$BACKUP_TMP" "$R2_LATEST" --endpoint-url "$R2_ENDPOINT" --region "$R2_REGION" >/dev/null 2>&1 || true
            echo "    ✓ Cloudflare R2 mirror succeeded"
        else
            echo "    ✗ Cloudflare R2 mirror failed" >&2
            ERRORS=$((ERRORS + 1))
        fi
    fi
else
    echo "==> [3/4] Cloudflare R2: skipped (bucket or endpoint not configured)"
fi

# Step 4: Upload to Backblaze B2 (if configured)
if [ -n "$B2_BUCKET" ] && [ -n "$B2_ENDPOINT" ]; then
    B2_TARGET="s3://${B2_BUCKET}/backups/botsensai_${TIMESTAMP}.db"
    B2_LATEST="s3://${B2_BUCKET}/latest/botsensai.db"
    echo "==> [4/4] Mirroring to Backblaze B2: $B2_TARGET"
    if [ "$DRY_RUN" = true ]; then
        echo "    (Dry-run) aws s3 cp $BACKUP_TMP $B2_TARGET --endpoint-url $B2_ENDPOINT"
        echo "    (Dry-run) aws s3 cp $BACKUP_TMP $B2_LATEST --endpoint-url $B2_ENDPOINT"
    else
        B2_ENV=()
        if [ -n "${BOTSENSAI_BACKUP_B2_ACCESS_KEY_ID:-${B2_ACCESS_KEY_ID:-}}" ]; then
            B2_ENV+=(AWS_ACCESS_KEY_ID="${BOTSENSAI_BACKUP_B2_ACCESS_KEY_ID:-${B2_ACCESS_KEY_ID}}")
        fi
        if [ -n "${BOTSENSAI_BACKUP_B2_SECRET_ACCESS_KEY:-${B2_SECRET_ACCESS_KEY:-}}" ]; then
            B2_ENV+=(AWS_SECRET_ACCESS_KEY="${BOTSENSAI_BACKUP_B2_SECRET_ACCESS_KEY:-${B2_SECRET_ACCESS_KEY}}")
        fi

        if env "${B2_ENV[@]}" aws s3 cp "$BACKUP_TMP" "$B2_TARGET" --endpoint-url "$B2_ENDPOINT"; then
            env "${B2_ENV[@]}" aws s3 cp "$BACKUP_TMP" "$B2_LATEST" --endpoint-url "$B2_ENDPOINT" >/dev/null 2>&1 || true
            echo "    ✓ Backblaze B2 mirror succeeded"
        else
            echo "    ✗ Backblaze B2 mirror failed" >&2
            ERRORS=$((ERRORS + 1))
        fi
    fi
else
    echo "==> [4/4] Backblaze B2: skipped (not configured)"
fi

# Cleanup
if [ "$DRY_RUN" = false ] && [ -f "$BACKUP_TMP" ]; then
    rm -f "$BACKUP_TMP"
fi

if [ $ERRORS -gt 0 ]; then
    echo ""
    echo "==> Backup completed with $ERRORS error(s)."
    exit 1
fi

echo ""
echo "==> Multi-Cloud backup complete! All active mirrors updated successfully."
exit 0
