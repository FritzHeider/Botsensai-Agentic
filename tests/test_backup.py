"""Unit tests for Multi-Cloud database backup and snapshot mirroring (S3 + R2 / B2)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from botsensai.cli import app
from botsensai.config import (
    AWSS3Settings,
    BackblazeB2Settings,
    BackupSettings,
    CloudflareR2Settings,
    Settings,
)
from botsensai.store.backup import (
    CloudTarget,
    MultiCloudBackupManager,
    create_atomic_snapshot,
)


@pytest.fixture(autouse=True)
def clean_backup_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate backup tests from host environment variables in .env or local environment."""
    backup_vars = [
        "BOTSENSAI_BACKUP_S3_ENABLED", "BOTSENSAI_BACKUP_S3_BUCKET", "AWS_S3_BUCKET",
        "BOTSENSAI_BACKUP_S3_REGION", "BOTSENSAI_BACKUP_S3_PROFILE",
        "BOTSENSAI_BACKUP_R2_ENABLED", "BOTSENSAI_BACKUP_R2_BUCKET", "R2_BUCKET",
        "BOTSENSAI_BACKUP_R2_ACCOUNT_ID", "R2_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID",
        "BOTSENSAI_BACKUP_R2_ENDPOINT_URL", "R2_ENDPOINT_URL",
        "BOTSENSAI_BACKUP_R2_ACCESS_KEY_ID", "R2_ACCESS_KEY_ID",
        "BOTSENSAI_BACKUP_R2_SECRET_ACCESS_KEY", "R2_SECRET_ACCESS_KEY",
        "BOTSENSAI_BACKUP_R2_REGION", "R2_REGION",
        "BOTSENSAI_BACKUP_B2_ENABLED", "BOTSENSAI_BACKUP_B2_BUCKET", "B2_BUCKET",
        "BOTSENSAI_BACKUP_B2_ENDPOINT_URL", "B2_ENDPOINT_URL",
        "BOTSENSAI_BACKUP_B2_REGION", "B2_REGION",
        "BOTSENSAI_BACKUP_B2_ACCESS_KEY_ID", "B2_ACCESS_KEY_ID",
        "BOTSENSAI_BACKUP_B2_SECRET_ACCESS_KEY", "B2_SECRET_ACCESS_KEY",
    ]
    for var in backup_vars:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def sample_db(tmp_path: Path) -> Path:
    """Create a sample SQLite database for testing snapshots."""
    db_file = tmp_path / "botsensai.db"
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("CREATE TABLE launches (token_key TEXT PRIMARY KEY, symbol TEXT);")
    cur.execute("INSERT INTO launches VALUES ('sol:bonk', 'BONK'), ('sol:wif', 'WIF');")
    conn.commit()
    conn.close()
    return db_file


def test_create_atomic_snapshot(sample_db: Path, tmp_path: Path) -> None:
    """Verify online atomic backup correctly clones the database."""
    dest = tmp_path / "snapshot.db"
    result = create_atomic_snapshot(sample_db, dest)
    assert result == dest
    assert dest.exists()

    conn = sqlite3.connect(str(dest))
    cur = conn.cursor()
    rows = cur.execute("SELECT token_key, symbol FROM launches ORDER BY symbol").fetchall()
    conn.close()

    assert len(rows) == 2
    assert rows[0] == ("sol:bonk", "BONK")
    assert rows[1] == ("sol:wif", "WIF")


def test_create_atomic_snapshot_missing_source(tmp_path: Path) -> None:
    """Attempting to snapshot a missing file raises FileNotFoundError."""
    missing = tmp_path / "nonexistent.db"
    dest = tmp_path / "snapshot.db"
    with pytest.raises(FileNotFoundError):
        create_atomic_snapshot(missing, dest)


def test_r2_settings_resolution() -> None:
    """Cloudflare R2 correctly resolves account endpoint and configuration status."""
    r2 = CloudflareR2Settings(
        bucket="my-r2-bucket",
        account_id="acc12345",
    )
    assert r2.resolved_endpoint == "https://acc12345.r2.cloudflarestorage.com"
    assert r2.resolved_region == "auto"
    assert r2.is_configured is True

    # Custom endpoint overrides account_id
    r2_custom = CloudflareR2Settings(
        bucket="my-r2-bucket",
        account_id="acc12345",
        endpoint_url="https://custom.r2.internal/",
    )
    assert r2_custom.resolved_endpoint == "https://custom.r2.internal"

    # Incomplete configuration
    r2_incomplete = CloudflareR2Settings(bucket="my-bucket")
    assert r2_incomplete.is_configured is False


def test_b2_settings_resolution() -> None:
    """Backblaze B2 correctly resolves regional S3 endpoint."""
    b2 = BackblazeB2Settings(
        bucket="my-b2-bucket",
        region="us-west-004",
    )
    assert b2.resolved_endpoint == "https://s3.us-west-004.backblazeb2.com"
    assert b2.is_configured is True


def test_multi_cloud_backup_manager_targets(sample_db: Path) -> None:
    """MultiCloudBackupManager resolves active targets from settings."""
    settings = Settings(
        db_path=str(sample_db),
        backup=BackupSettings(
            s3=AWSS3Settings(bucket="aws-backup-bucket"),
            r2=CloudflareR2Settings(bucket="r2-backup-bucket", account_id="cf_acc_99"),
            b2=BackblazeB2Settings(bucket="b2-backup-bucket", region="us-east-005"),
        ),
    )
    manager = MultiCloudBackupManager(settings)
    targets = manager.get_configured_targets()

    providers = [t.provider for t in targets]
    assert "s3" in providers
    assert "r2" in providers
    assert "b2" in providers

    r2_target = next(t for t in targets if t.provider == "r2")
    assert r2_target.endpoint_url == "https://cf_acc_99.r2.cloudflarestorage.com"
    assert r2_target.bucket == "r2-backup-bucket"
    assert r2_target.region == "auto"

    # Test provider filtering
    r2_only = manager.get_configured_targets(providers=["r2"])
    assert len(r2_only) == 1
    assert r2_only[0].provider == "r2"


def test_build_aws_cli_cmd() -> None:
    """Command builder attaches proper endpoint, region, and credentials."""
    manager = MultiCloudBackupManager()
    target = CloudTarget(
        provider="r2",
        bucket="r2-test",
        endpoint_url="https://cf123.r2.cloudflarestorage.com",
        region="auto",
        access_key_id="r2_access_key",
        secret_access_key="r2_secret_key",
    )
    cmd, env = manager.build_aws_cli_cmd("/tmp/snapshot.db", "s3://r2-test/backups/snapshot.db", target)

    assert cmd == [
        "aws",
        "s3",
        "cp",
        "/tmp/snapshot.db",
        "s3://r2-test/backups/snapshot.db",
        "--endpoint-url",
        "https://cf123.r2.cloudflarestorage.com",
        "--region",
        "auto",
    ]
    assert env["AWS_ACCESS_KEY_ID"] == "r2_access_key"
    assert env["AWS_SECRET_ACCESS_KEY"] == "r2_secret_key"
    assert env["AWS_DEFAULT_REGION"] == "auto"
    assert env["AWS_REGION"] == "auto"


def test_mirror_backup_dry_run(sample_db: Path) -> None:
    """Dry run simulates snapshot and upload reporting without external network calls."""
    settings = Settings(
        db_path=str(sample_db),
        backup=BackupSettings(
            s3=AWSS3Settings(bucket="test-aws-bucket"),
            r2=CloudflareR2Settings(bucket="test-r2-bucket", account_id="12345678"),
        ),
    )
    manager = MultiCloudBackupManager(settings)
    report = manager.mirror_backup(dry_run=True)

    assert report.success is True
    assert report.total_bytes > 0
    assert len(report.results) == 2

    s3_res = next(r for r in report.results if r.provider == "s3")
    r2_res = next(r for r in report.results if r.provider == "r2")

    assert s3_res.success is True
    assert "test-aws-bucket/backups/" in s3_res.target_uri
    assert s3_res.latest_uri == "s3://test-aws-bucket/latest/botsensai.db"

    assert r2_res.success is True
    assert "test-r2-bucket/backups/" in r2_res.target_uri
    assert r2_res.latest_uri == "s3://test-r2-bucket/latest/botsensai.db"


def test_pull_latest_prefers_r2(tmp_path: Path) -> None:
    """pull_latest prioritizes Cloudflare R2 for zero egress fees."""
    settings = Settings(
        backup=BackupSettings(
            s3=AWSS3Settings(bucket="test-aws-bucket"),
            r2=CloudflareR2Settings(bucket="test-r2-bucket", account_id="12345678"),
        ),
    )
    manager = MultiCloudBackupManager(settings)
    dest = tmp_path / "remote.db"

    ok, msg = manager.pull_latest(dest, dry_run=True, prefer_provider="r2")
    assert ok is True
    assert "s3://test-r2-bucket/latest/botsensai.db" in msg


def test_mirror_backup_handles_partial_failure(sample_db: Path) -> None:
    """If one target fails, other targets still attempt upload and are recorded in report."""
    settings = Settings(
        db_path=str(sample_db),
        backup=BackupSettings(
            s3=AWSS3Settings(bucket="test-aws-bucket"),
            r2=CloudflareR2Settings(bucket="test-r2-bucket", account_id="12345678"),
        ),
    )
    manager = MultiCloudBackupManager(settings)

    # Mock subprocess.run: S3 fails, R2 succeeds
    def mock_run(cmd, *args, **kwargs):
        mock_proc = MagicMock()
        if "test-aws-bucket" in " ".join(cmd):
            mock_proc.returncode = 1
            mock_proc.stderr = "AccessDenied: Bucket does not exist"
        else:
            mock_proc.returncode = 0
            mock_proc.stderr = ""
        return mock_proc

    with patch("subprocess.run", side_effect=mock_run), patch("shutil.which", return_value="/usr/local/bin/aws"):
        report = manager.mirror_backup(dry_run=False)

    assert report.success is False
    assert "s3" in report.failed_providers
    assert "r2" in report.successful_providers
    assert len(report.errors) == 1
    assert "AccessDenied" in report.errors[0]


def test_cli_backup_status() -> None:
    """CLI backup --status displays active multi-cloud targets."""
    runner = CliRunner()
    result = runner.invoke(app, ["backup", "--status"])
    assert result.exit_code == 0
    assert "multi-cloud backup targets" in result.output
    assert "S3" in result.output


def test_cli_backup_dry_run() -> None:
    """CLI backup --dry-run prints execution plan."""
    runner = CliRunner()
    result = runner.invoke(app, ["backup", "--dry-run"])
    assert result.exit_code == 0
    assert "backup results" in result.output
    assert "uploaded" in result.output
