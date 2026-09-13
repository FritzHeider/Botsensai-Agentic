"""Multi-Cloud Database Backup and Mirroring (AWS S3 + Cloudflare R2 / Backblaze B2).

Cloudflare R2 provides an S3-compatible API with ZERO egress bandwidth fees.
This module provides atomic online SQLite snapshotting and mirrors snapshots
across AWS S3 (redundancy) and Cloudflare R2 / Backblaze B2 (low/zero-cost egress).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from botsensai.config import Settings, get_settings
from botsensai.util.logging import get_logger

log = get_logger(__name__)


@dataclass
class CloudTarget:
    """Represents a cloud storage destination (S3, R2, or B2)."""

    provider: str  # "s3", "r2", "b2"
    bucket: str
    endpoint_url: str | None = None
    region: str | None = None
    profile: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    prefix: str = "backups"
    enabled: bool = True

    @property
    def is_configured(self) -> bool:
        if not self.enabled or not self.bucket:
            return False
        if self.provider == "s3":
            return True
        return bool(self.endpoint_url)

    @property
    def display_name(self) -> str:
        if self.provider == "r2":
            return f"Cloudflare R2 ({self.bucket})"
        if self.provider == "b2":
            return f"Backblaze B2 ({self.bucket})"
        return f"AWS S3 ({self.bucket})"


@dataclass
class TargetUploadResult:
    """Outcome of uploading a snapshot to a single cloud provider."""

    provider: str
    target_uri: str
    latest_uri: str | None = None
    success: bool = False
    error: str | None = None
    duration_seconds: float = 0.0
    bytes_uploaded: int = 0


@dataclass
class BackupReport:
    """Aggregated summary of a multi-cloud backup operation."""

    snapshot_path: str
    timestamp: str
    total_bytes: int
    results: list[TargetUploadResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        if not self.results:
            return False
        return all(r.success for r in self.results)

    @property
    def successful_providers(self) -> list[str]:
        return [r.provider for r in self.results if r.success]

    @property
    def failed_providers(self) -> list[str]:
        return [r.provider for r in self.results if not r.success]

    def summary(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "snapshot_path": self.snapshot_path,
            "total_bytes": self.total_bytes,
            "success": self.success,
            "successful_targets": [r.target_uri for r in self.results if r.success],
            "failed_targets": [r.target_uri for r in self.results if not r.success],
            "errors": self.errors,
        }


def create_atomic_snapshot(db_path: str | Path, dest_path: str | Path) -> Path:
    """Create a consistent point-in-time snapshot using SQLite's online backup API.

    Works cleanly even while WAL writers and background collectors are active.
    """
    src_p = Path(db_path)
    dst_p = Path(dest_path)

    if not src_p.exists():
        raise FileNotFoundError(f"Database does not exist at: {src_p}")

    dst_p.parent.mkdir(parents=True, exist_ok=True)

    # Online backup API ensures ACID consistency during live operations
    src_conn = sqlite3.connect(str(src_p), timeout=30.0)
    dst_conn = sqlite3.connect(str(dst_p))
    try:
        with dst_conn:
            src_conn.backup(dst_conn, pages=100)
    finally:
        dst_conn.close()
        src_conn.close()

    log.info("created_atomic_snapshot", source=str(src_p), destination=str(dst_p), size=dst_p.stat().st_size)
    return dst_p


class MultiCloudBackupManager:
    """Manages SQLite database backups mirrored across AWS S3, Cloudflare R2, and Backblaze B2."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def get_configured_targets(self, providers: list[str] | None = None) -> list[CloudTarget]:
        """Resolve active cloud targets from settings and environment variables."""
        targets: list[CloudTarget] = []
        filter_providers = {p.lower() for p in providers} if providers else None

        # 1. AWS S3 Target
        s3_cfg = self.settings.backup.s3
        s3_bucket = s3_cfg.resolved_bucket
        if s3_cfg.resolved_enabled and s3_bucket:
            if not filter_providers or "s3" in filter_providers:
                targets.append(
                    CloudTarget(
                        provider="s3",
                        bucket=s3_bucket,
                        region=s3_cfg.resolved_region,
                        profile=s3_cfg.profile,
                        prefix=s3_cfg.prefix,
                        enabled=True,
                    )
                )

        # 2. Cloudflare R2 Target (Zero Egress)
        r2_cfg = self.settings.backup.r2
        r2_bucket = r2_cfg.resolved_bucket
        r2_endpoint = r2_cfg.resolved_endpoint
        if r2_cfg.resolved_enabled and r2_bucket and r2_endpoint:
            if not filter_providers or "r2" in filter_providers:
                targets.append(
                    CloudTarget(
                        provider="r2",
                        bucket=r2_bucket,
                        endpoint_url=r2_endpoint,
                        access_key_id=r2_cfg.resolved_access_key_id,
                        secret_access_key=r2_cfg.resolved_secret_access_key,
                        prefix=r2_cfg.prefix,
                        enabled=True,
                    )
                )

        # 3. Backblaze B2 Target
        b2_cfg = self.settings.backup.b2
        b2_bucket = b2_cfg.resolved_bucket
        b2_endpoint = b2_cfg.resolved_endpoint
        if b2_cfg.resolved_enabled and b2_bucket and b2_endpoint:
            if not filter_providers or "b2" in filter_providers:
                targets.append(
                    CloudTarget(
                        provider="b2",
                        bucket=b2_bucket,
                        endpoint_url=b2_endpoint,
                        region=b2_cfg.region,
                        access_key_id=b2_cfg.resolved_access_key_id,
                        secret_access_key=b2_cfg.resolved_secret_access_key,
                        prefix=b2_cfg.prefix,
                        enabled=True,
                    )
                )

        return targets

    def build_aws_cli_cmd(
        self,
        source: str,
        destination: str,
        target: CloudTarget,
    ) -> tuple[list[str], dict[str, str]]:
        """Build aws-cli command arguments and environment for a specific cloud target."""
        cmd = ["aws", "s3", "cp", source, destination]
        env = os.environ.copy()

        if target.endpoint_url:
            cmd.extend(["--endpoint-url", target.endpoint_url])

        if target.region:
            cmd.extend(["--region", target.region])

        if target.profile:
            cmd.extend(["--profile", target.profile])

        # Provider-specific credentials overriding environment
        if target.access_key_id:
            env["AWS_ACCESS_KEY_ID"] = target.access_key_id
        if target.secret_access_key:
            env["AWS_SECRET_ACCESS_KEY"] = target.secret_access_key

        return cmd, env

    def upload_target(
        self,
        snapshot_path: Path,
        target: CloudTarget,
        timestamp: str,
        update_latest: bool = True,
        dry_run: bool = False,
    ) -> TargetUploadResult:
        """Upload snapshot to a single cloud target (updating timestamped and latest keys)."""
        file_size = snapshot_path.stat().st_size if snapshot_path.exists() else 0
        target_key = f"{target.prefix}/botsensai_{timestamp}.db".strip("/")
        target_uri = f"s3://{target.bucket}/{target_key}"
        latest_uri = f"s3://{target.bucket}/latest/botsensai.db" if update_latest else None

        res = TargetUploadResult(
            provider=target.provider,
            target_uri=target_uri,
            latest_uri=latest_uri,
            bytes_uploaded=file_size,
        )

        if dry_run:
            res.success = True
            log.info("dry_run_upload", provider=target.provider, target_uri=target_uri, latest_uri=latest_uri)
            return res

        if not shutil.which("aws"):
            res.success = False
            res.error = "aws command line tool is not installed or not in PATH"
            log.error("aws_cli_missing", provider=target.provider)
            return res

        start_t = time.monotonic()
        try:
            # 1. Upload timestamped snapshot
            cmd, env = self.build_aws_cli_cmd(str(snapshot_path), target_uri, target)
            log.info("uploading_snapshot", provider=target.provider, destination=target_uri)
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                res.success = False
                res.error = proc.stderr.strip() or f"Process exited with {proc.returncode}"
                log.error("upload_failed", provider=target.provider, error=res.error)
                return res

            # 2. Upload latest pointer if requested
            if update_latest and latest_uri:
                latest_cmd, latest_env = self.build_aws_cli_cmd(str(snapshot_path), latest_uri, target)
                log.info("updating_latest_pointer", provider=target.provider, destination=latest_uri)
                latest_proc = subprocess.run(latest_cmd, env=latest_env, capture_output=True, text=True, check=False)
                if latest_proc.returncode != 0:
                    log.warning("latest_pointer_failed", provider=target.provider, error=latest_proc.stderr.strip())

            res.success = True
            res.duration_seconds = round(time.monotonic() - start_t, 2)
            log.info("upload_complete", provider=target.provider, target_uri=target_uri, duration=res.duration_seconds)
            return res

        except Exception as exc:
            res.success = False
            res.error = str(exc)
            res.duration_seconds = round(time.monotonic() - start_t, 2)
            log.exception("upload_exception", provider=target.provider, error=str(exc))
            return res

    def mirror_backup(
        self,
        db_path: str | Path | None = None,
        providers: list[str] | None = None,
        update_latest: bool = True,
        dry_run: bool = False,
        keep_local_snapshot: bool = False,
    ) -> BackupReport:
        """Execute online atomic backup and mirror to all configured cloud targets."""
        source_db = Path(db_path) if db_path else self.settings.path(self.settings.db_path)
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%SZ")
        tmp_snapshot = Path(f"/tmp/botsensai_{timestamp}.db")

        # Resolve destinations
        targets = self.get_configured_targets(providers)
        if not targets:
            raise ValueError(
                "No cloud backup targets configured. Set BOTSENSAI_BACKUP_S3_BUCKET, "
                "BOTSENSAI_BACKUP_R2_BUCKET + BOTSENSAI_BACKUP_R2_ACCOUNT_ID, or configure in config/botsensai.yaml."
            )

        # 1. Create atomic SQLite snapshot
        create_atomic_snapshot(source_db, tmp_snapshot)
        file_size = tmp_snapshot.stat().st_size

        report = BackupReport(
            snapshot_path=str(tmp_snapshot),
            timestamp=timestamp,
            total_bytes=file_size,
        )

        try:
            # 2. Upload to each target independently
            for target in targets:
                upload_res = self.upload_target(
                    snapshot_path=tmp_snapshot,
                    target=target,
                    timestamp=timestamp,
                    update_latest=update_latest,
                    dry_run=dry_run,
                )
                report.results.append(upload_res)
                if not upload_res.success and upload_res.error:
                    report.errors.append(f"{target.provider}: {upload_res.error}")

        finally:
            if not keep_local_snapshot and tmp_snapshot.exists():
                try:
                    tmp_snapshot.unlink()
                except OSError as exc:
                    log.warning("failed_to_cleanup_temp_snapshot", error=str(exc))

        return report

    def pull_latest(
        self,
        dest_path: str | Path,
        prefer_provider: str = "r2",
        dry_run: bool = False,
    ) -> tuple[bool, str]:
        """Download latest database snapshot.

        Prioritizes Cloudflare R2 (zero egress bandwidth fees) over AWS S3.
        """
        dest_p = Path(dest_path)
        dest_p.parent.mkdir(parents=True, exist_ok=True)

        targets = self.get_configured_targets()
        if not targets:
            return False, "No cloud targets configured from which to pull database."

        # Order targets: preferred provider first
        ordered_targets = sorted(
            targets,
            key=lambda t: 0 if t.provider == prefer_provider.lower() else (1 if t.provider == "r2" else 2),
        )

        errors: list[str] = []
        for target in ordered_targets:
            source_uri = f"s3://{target.bucket}/latest/botsensai.db"
            if dry_run:
                return True, f"Dry-run: would download {source_uri} -> {dest_p}"

            cmd, env = self.build_aws_cli_cmd(source_uri, str(dest_p), target)
            log.info("pulling_database", provider=target.provider, source=source_uri, destination=str(dest_p))
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
            if proc.returncode == 0 and dest_p.exists() and dest_p.stat().st_size > 0:
                log.info("pull_database_success", provider=target.provider, size=dest_p.stat().st_size)
                return True, f"Successfully pulled snapshot from {target.display_name} ({source_uri})"

            err = proc.stderr.strip() or f"Download failed with exit code {proc.returncode}"
            log.warning("pull_database_failed", provider=target.provider, error=err)
            errors.append(f"{target.provider}: {err}")

        return False, f"Failed to pull snapshot from all providers. Errors: {'; '.join(errors)}"


__all__ = [
    "BackupReport",
    "CloudTarget",
    "MultiCloudBackupManager",
    "TargetUploadResult",
    "create_atomic_snapshot",
]
