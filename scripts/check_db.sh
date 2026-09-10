#!/usr/bin/env bash
# ============================================================================ #
# Check Botsensai SQLite database statistics on the instance
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

DB_FILE="${BOTSENSAI_DB_PATH:-$APP_DIR/data/botsensai.db}"

echo "==> Checking database: $DB_FILE"
if [ ! -f "$DB_FILE" ]; then
    echo "Notice: Database file does not exist yet at $DB_FILE (will be created on first sweep)."
    exit 0
fi

ls -lh "$DB_FILE"

python3 - <<'EOF'
import os
from pathlib import Path
from botsensai.config import get_settings
from botsensai.store.db import Database

settings = get_settings()
db_path = settings.path(settings.db_path)

if db_path.exists():
    db = Database(db_path)
    counts = db.counts()
    print("\n--- Record Counts ---")
    for table, count in counts.items():
        print(f"  {table:20s}: {count:,}")
else:
    print(f"Database at {db_path} not found.")
EOF
