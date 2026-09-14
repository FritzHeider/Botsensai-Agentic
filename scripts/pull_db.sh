#!/usr/bin/env bash
# ============================================================================ #
# Download latest database snapshot from Cloudflare R2 or AWS S3 to local machine
#
# Prioritizes Cloudflare R2 by default when configured because Cloudflare R2
# has ZERO egress bandwidth fees, avoiding AWS outbound data transfer charges.
#
# Usage:
#   ./scripts/pull_db.sh [OPTIONS] [DEST_PATH]
#
# Examples:
#   ./scripts/pull_db.sh                                # Pull to data/botsensai_remote.db
#   ./scripts/pull_db.sh --provider r2                  # Force pull from Cloudflare R2 (zero egress)
#   ./scripts/pull_db.sh --provider s3                  # Force pull from AWS S3
#   ./scripts/pull_db.sh data/my_snapshot.db            # Custom target path
# ============================================================================ #
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Source .env if present
if [ -f "$REPO_DIR/.env" ]; then
    set -a
    source "$REPO_DIR/.env"
    set +a
fi

PROVIDER="auto"
DRY_RUN=false
DEST=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --provider|-p)
            PROVIDER="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS] [DEST_PATH]"
            echo ""
            echo "Options:"
            echo "  --provider, -p [auto|r2|s3|b2]  Source provider (default: auto, prefers R2 for zero egress fees)"
            echo "  --dry-run                       Simulate download without downloading"
            echo "  --help, -h                      Show this help message"
            exit 0
            ;;
        *)
            if [ -z "$DEST" ]; then
                DEST="$1"
            fi
            shift
            ;;
    esac
done

DEST="${DEST:-data/botsensai_remote.db}"
mkdir -p "$(dirname "$DEST")"

PROFILE="${AWS_PROFILE:-agent-profile}"
S3_BUCKET="${BOTSENSAI_BACKUP_S3_BUCKET:-botsensai-backups-538471157365}"

R2_BUCKET="${BOTSENSAI_BACKUP_R2_BUCKET:-${R2_BUCKET:-botsensai-backups}}"
R2_ACCOUNT_ID="${BOTSENSAI_BACKUP_R2_ACCOUNT_ID:-${R2_ACCOUNT_ID:-${CLOUDFLARE_ACCOUNT_ID:-}}}"
R2_ENDPOINT="${BOTSENSAI_BACKUP_R2_ENDPOINT_URL:-${R2_ENDPOINT_URL:-}}"
R2_REGION="${BOTSENSAI_BACKUP_R2_REGION:-${R2_REGION:-auto}}"
if [ -z "$R2_ENDPOINT" ] && [ -n "$R2_ACCOUNT_ID" ]; then
    R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
fi

B2_BUCKET="${BOTSENSAI_BACKUP_B2_BUCKET:-${B2_BUCKET:-}}"
B2_ENDPOINT="${BOTSENSAI_BACKUP_B2_ENDPOINT_URL:-${B2_ENDPOINT_URL:-}}"
B2_REGION="${BOTSENSAI_BACKUP_B2_REGION:-${B2_REGION:-}}"
if [ -z "$B2_ENDPOINT" ] && [ -n "$B2_REGION" ]; then
    B2_ENDPOINT="https://s3.${B2_REGION}.backblazeb2.com"
fi

echo "======================================================================"
echo "  Downloading Latest Botsensai Database Snapshot"
echo "  Target Path : $DEST"
echo "  Provider    : $PROVIDER"
echo "======================================================================"

PULL_SUCCESS=false

# Helper for pulling from Cloudflare R2
pull_from_r2() {
    if [ -z "$R2_ENDPOINT" ] || [ -z "$R2_BUCKET" ]; then
        return 1
    fi
    SOURCE_URI="s3://$R2_BUCKET/latest/botsensai.db"
    echo "==> Pulling from Cloudflare R2 (Zero Egress): $SOURCE_URI"
    if [ "$DRY_RUN" = true ]; then
        echo "    (Dry-run) aws s3 cp $SOURCE_URI $DEST --endpoint-url $R2_ENDPOINT --region $R2_REGION"
        return 0
    fi

    R2_ENV=()
    if [ -n "${BOTSENSAI_BACKUP_R2_ACCESS_KEY_ID:-${R2_ACCESS_KEY_ID:-}}" ]; then
        R2_ENV+=(AWS_ACCESS_KEY_ID="${BOTSENSAI_BACKUP_R2_ACCESS_KEY_ID:-${R2_ACCESS_KEY_ID}}")
    fi
    if [ -n "${BOTSENSAI_BACKUP_R2_SECRET_ACCESS_KEY:-${R2_SECRET_ACCESS_KEY:-}}" ]; then
        R2_ENV+=(AWS_SECRET_ACCESS_KEY="${BOTSENSAI_BACKUP_R2_SECRET_ACCESS_KEY:-${R2_SECRET_ACCESS_KEY}}")
    fi
    R2_ENV+=(AWS_DEFAULT_REGION="$R2_REGION")
    R2_ENV+=(AWS_REGION="$R2_REGION")

    if env "${R2_ENV[@]}" aws s3 cp "$SOURCE_URI" "$DEST" --endpoint-url "$R2_ENDPOINT" --region "$R2_REGION"; then
        echo "    ✓ Successfully downloaded from Cloudflare R2"
        return 0
    else
        echo "    ✗ Cloudflare R2 download failed" >&2
        return 1
    fi
}

# Helper for pulling from AWS S3
pull_from_s3() {
    if [ -z "$S3_BUCKET" ]; then
        return 1
    fi
    SOURCE_URI="s3://$S3_BUCKET/latest/botsensai.db"
    echo "==> Pulling from Amazon S3: $SOURCE_URI"
    if [ "$DRY_RUN" = true ]; then
        echo "    (Dry-run) aws s3 cp $SOURCE_URI $DEST ${PROFILE:+--profile $PROFILE}"
        return 0
    fi

    PROFILE_ARG=()
    if [ -n "$PROFILE" ]; then
        PROFILE_ARG=(--profile "$PROFILE")
    fi

    if aws s3 cp "$SOURCE_URI" "$DEST" "${PROFILE_ARG[@]}"; then
        echo "    ✓ Successfully downloaded from Amazon S3"
        return 0
    else
        echo "    ✗ Amazon S3 download failed" >&2
        return 1
    fi
}

# Helper for pulling from Backblaze B2
pull_from_b2() {
    if [ -z "$B2_ENDPOINT" ] || [ -z "$B2_BUCKET" ]; then
        return 1
    fi
    SOURCE_URI="s3://$B2_BUCKET/latest/botsensai.db"
    echo "==> Pulling from Backblaze B2: $SOURCE_URI"
    if [ "$DRY_RUN" = true ]; then
        echo "    (Dry-run) aws s3 cp $SOURCE_URI $DEST --endpoint-url $B2_ENDPOINT"
        return 0
    fi

    B2_ENV=()
    if [ -n "${BOTSENSAI_BACKUP_B2_ACCESS_KEY_ID:-${B2_ACCESS_KEY_ID:-}}" ]; then
        B2_ENV+=(AWS_ACCESS_KEY_ID="${BOTSENSAI_BACKUP_B2_ACCESS_KEY_ID:-${B2_ACCESS_KEY_ID}}")
    fi
    if [ -n "${BOTSENSAI_BACKUP_B2_SECRET_ACCESS_KEY:-${B2_SECRET_ACCESS_KEY:-}}" ]; then
        B2_ENV+=(AWS_SECRET_ACCESS_KEY="${BOTSENSAI_BACKUP_B2_SECRET_ACCESS_KEY:-${B2_SECRET_ACCESS_KEY}}")
    fi

    if env "${B2_ENV[@]}" aws s3 cp "$SOURCE_URI" "$DEST" --endpoint-url "$B2_ENDPOINT"; then
        echo "    ✓ Successfully downloaded from Backblaze B2"
        return 0
    else
        echo "    ✗ Backblaze B2 download failed" >&2
        return 1
    fi
}

case "$PROVIDER" in
    r2)
        pull_from_r2 && PULL_SUCCESS=true
        ;;
    s3)
        pull_from_s3 && PULL_SUCCESS=true
        ;;
    b2)
        pull_from_b2 && PULL_SUCCESS=true
        ;;
    auto)
        # Try R2 first (zero egress fees), fall back to S3, then B2
        if pull_from_r2; then
            PULL_SUCCESS=true
        elif pull_from_s3; then
            PULL_SUCCESS=true
        elif pull_from_b2; then
            PULL_SUCCESS=true
        fi
        ;;
    *)
        echo "Error: Unknown provider '$PROVIDER'. Use: auto, r2, s3, or b2." >&2
        exit 1
        ;;
esac

if [ "$PULL_SUCCESS" = false ]; then
    echo "Error: Failed to download database snapshot from available cloud providers." >&2
    exit 1
fi

if [ "$DRY_RUN" = true ]; then
    echo "==> Dry-run complete."
    exit 0
fi

echo ""
echo "==> Download complete! Size: $(ls -lh "$DEST" | awk '{print $5}')"
echo "==> Verifying local database tables..."
python3 -c "
import sqlite3, sys
try:
    conn = sqlite3.connect('$DEST')
    cur = conn.cursor()
    tables = [row[0] for row in cur.execute(\"SELECT name FROM sqlite_master WHERE type='table';\").fetchall()]
    print(f'Tables found ({len(tables)}): {tables[:8]}...')
    for t in ['launches', 'trades', 'scores', 'outcomes']:
        if t in tables:
            count = cur.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
            print(f'  {t:15s}: {count:,}')
    conn.close()
except Exception as exc:
    print(f'Error verifying database: {exc}', file=sys.stderr)
    sys.exit(1)
"
