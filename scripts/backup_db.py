#!/usr/bin/env python3
"""Execute atomic SQLite snapshot and mirror across multi-cloud storage (S3 + R2 / B2).

Can be run standalone or invoked by scripts/wizard.py / cron.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add project root to sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from botsensai.config import load_settings  # noqa: E402
from botsensai.store.backup import MultiCloudBackupManager  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-Cloud SQLite Database Backup")
    parser.add_argument("--config", help="Path to config YAML")
    parser.add_argument("--provider", default="all", help="Target provider: all | s3 | r2 | b2")
    parser.add_argument("--dry-run", action="store_true", help="Simulate backup without uploading")
    parser.add_argument("--no-latest", action="store_true", help="Do not overwrite latest/botsensai.db")
    args = parser.parse_args()

    settings = load_settings(args.config)
    manager = MultiCloudBackupManager(settings)

    targets = manager.get_configured_targets(None if args.provider == "all" else [args.provider])
    if not targets:
        print(f"Error: No cloud backup targets configured for provider '{args.provider}'.", file=sys.stderr)
        print("Configure in config/botsensai.yaml or set environment variables like BOTSENSAI_BACKUP_S3_BUCKET.", file=sys.stderr)
        sys.exit(1)

    print(f"==> Mirroring database backup to {len(targets)} target(s):")
    for t in targets:
        print(f"    - {t.display_name} -> s3://{t.bucket}/{t.prefix}/")

    try:
        report = manager.mirror_backup(
            providers=None if args.provider == "all" else [args.provider],
            update_latest=not args.no_latest,
            dry_run=args.dry_run,
        )
        print(f"\n==> Backup finished for snapshot: {report.timestamp}")
        for res in report.results:
            status_icon = "✓" if res.success else "✗"
            dur = f"({res.duration_seconds:.2f}s)" if res.success else f"Error: {res.error}"
            print(f"    {status_icon} {res.provider.upper()}: {res.target_uri} {dur}")

        if not report.success:
            print("\nError: One or more backup destinations failed.", file=sys.stderr)
            sys.exit(1)

        print("\n==> Multi-Cloud backup complete! All active mirrors updated successfully.")
        sys.exit(0)

    except Exception as exc:
        print(f"\nError: Backup execution failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
