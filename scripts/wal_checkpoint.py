#!/usr/bin/env python3
"""Botsensai SQLite WAL Truncation and Optimization Daemon.

Executes PRAGMA wal_checkpoint(TRUNCATE) and PRAGMA optimize on Botsensai
databases to reclaim disk space, prevent unbounded WAL file growth, and
maintain optimal B-tree query performance.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import sqlite3
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("botsensai.wal_checkpoint")

TARGET_DATABASES = [
    Path("data/botsensai.db"),
    Path("data/memory.db"),
]


def checkpoint_database(db_path: Path) -> bool:
    if not db_path.exists():
        log.warning("Database %s does not exist, skipping.", db_path)
        return True

    wal_path = Path(f"{db_path}-wal")
    shm_path = Path(f"{db_path}-shm")

    db_size_before = db_path.stat().st_size if db_path.exists() else 0
    wal_size_before = wal_path.stat().st_size if wal_path.exists() else 0

    log.info(
        "Beginning WAL truncation for %s (DB: %.2f MB, WAL: %.2f MB)",
        db_path,
        db_size_before / (1024 * 1024),
        wal_size_before / (1024 * 1024),
    )

    t0 = time.perf_counter()
    try:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        conn.execute("PRAGMA busy_timeout = 30000;")


        # Execute truncate checkpoint
        cursor = conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        busy, log_frames, ckpt_frames = cursor.fetchone()
        log.info(
            "WAL checkpoint completed on %s: busy=%s, log_frames=%s, ckpt_frames=%s",
            db_path,
            busy,
            log_frames,
            ckpt_frames,
        )

        # Run query planner optimization
        conn.execute("PRAGMA optimize;")
        conn.commit()
        conn.close()

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        wal_size_after = wal_path.stat().st_size if wal_path.exists() else 0
        reclaimed_mb = (wal_size_before - wal_size_after) / (1024 * 1024)

        log.info(
            "Successfully truncated %s in %.1f ms (reclaimed %.2f MB, WAL now %.2f KB)",
            db_path,
            elapsed_ms,
            reclaimed_mb,
            wal_size_after / 1024,
        )
        return True
    except sqlite3.OperationalError as exc:
        log.error("Operational error during checkpoint on %s: %s", db_path, exc)
        return False
    except Exception as exc:
        log.exception("Unexpected error during checkpoint on %s: %s", db_path, exc)
        return False


def main() -> int:
    log.info("Starting automated SQLite WAL checkpoint sweep.")
    success = True
    for db in TARGET_DATABASES:
        if not checkpoint_database(db):
            success = False

    log.info("WAL checkpoint sweep finished. Overall status: %s", "OK" if success else "FAILED")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
