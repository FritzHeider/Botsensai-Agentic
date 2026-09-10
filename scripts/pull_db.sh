#!/usr/bin/env bash
# ============================================================================ #
# Download latest database snapshot from S3 to local machine for offline analysis
# Usage:
#   ./scripts/pull_db.sh [DEST_PATH]
# ============================================================================ #
set -euo pipefail

PROFILE="${AWS_PROFILE:-agent-profile}"
BUCKET="${BOTSENSAI_BACKUP_S3_BUCKET:-botsensai-backups-538471157365}"
DEST="${1:-data/botsensai_remote.db}"

PROFILE_FLAG=""
if [ -n "$PROFILE" ]; then
    PROFILE_FLAG="--profile $PROFILE"
fi

mkdir -p "$(dirname "$DEST")"

echo "======================================================================"
echo "  Downloading latest Botsensai database snapshot from S3"
echo "  Source : s3://$BUCKET/latest/botsensai.db"
echo "  Target : $DEST"
echo "======================================================================"

aws s3 cp "s3://$BUCKET/latest/botsensai.db" "$DEST" $PROFILE_FLAG

echo ""
echo "==> Download complete! Size: $(ls -lh "$DEST" | awk '{print $5}')"
echo "==> Verifying local tables..."
python3 -c "
import sqlite3
conn = sqlite3.connect('$DEST')
cur = conn.cursor()
tables = [row[0] for row in cur.execute(\"SELECT name FROM sqlite_master WHERE type='table';\").fetchall()]
print(f'Tables found ({len(tables)}): {tables[:8]}...')
for t in ['launches', 'trades', 'scores', 'outcomes']:
    if t in tables:
        count = cur.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
        print(f'  {t:15s}: {count:,}')
"
