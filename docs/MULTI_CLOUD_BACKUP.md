# Multi-Cloud Database Backup & Snapshot Mirroring

Botsensai stores continuous collector recon, market snapshots, trades, scores, and trade signals in an atomic SQLite database (`data/botsensai.db`) operating in WAL mode.

To safeguard historical data against EBS volume failures, cloud region outages, and to eliminate expensive egress bandwidth fees when researchers download datasets locally, Botsensai supports **Multi-Cloud Snapshot Mirroring** across **AWS S3**, **Cloudflare R2**, and **Backblaze B2**.

---

## Why Cloudflare R2?

| Feature | Amazon S3 | Cloudflare R2 |
| :--- | :--- | :--- |
| **API Compatibility** | S3 Standard API | 100% S3-Compatible API |
| **Data Storage Cost** | ~$0.023 / GB-month | ~$0.015 / GB-month |
| **Data Egress Bandwidth** | **$0.09 / GB** (Expensive) | **$0.00 / GB (ZERO Egress Fees)** |
| **Ideal Role** | Primary AWS EC2 Redundancy | Remote Download & Offline Research |

> [!TIP]
> When researchers or automated pipelines pull multi-gigabyte database snapshots repeatedly from Cloudflare R2, bandwidth egress costs are **\$0.00**.

---

## Multi-Cloud Architecture

```
                       ┌─────────────────────────┐
                       │   Botsensai Daemon      │
                       │ data/botsensai.db (WAL) │
                       └────────────┬────────────┘
                                    │
                    Atomic Online SQLite Snapshot
                                    │
                                    ▼
                       ┌─────────────────────────┐
                       │  /tmp/botsensai_$TS.db  │
                       └───────┬───────────┬─────┘
                               │           │
                 AWS S3 Upload │           │ S3-Compatible Upload
         (IAM / EC2 Profile)   │           │ (--endpoint-url)
                               ▼           ▼
                     ┌──────────────┐ ┌────────────────────────┐
                     │  Amazon S3   │ │  Cloudflare R2 / B2    │
                     │ (Redundancy) │ │   (Zero-Egress Mirror) │
                     └──────────────┘ └───────────┬────────────┘
                                                  │
                                                  │ Free Egress Download
                                                  ▼
                                      ┌────────────────────────┐
                                      │ Local Research Machine │
                                      │ (data/botsensai_remote)│
                                      └────────────────────────┘
```

Each backup pass writes two objects to every configured cloud store:
1. **Timestamped Archive**: `s3://<bucket>/backups/botsensai_YYYYMMDD_HHMMSSZ.db`
2. **Latest Pointer**: `s3://<bucket>/latest/botsensai.db`

---

## Configuration

Settings can be defined in `config/botsensai.yaml` or injected via environment variables (`.env`).

### 1. Configuration File (`config/botsensai.yaml`)

```yaml
backup:
  enabled: true
  cron_schedule: "0 */6 * * *"
  update_latest: true
  s3:
    enabled: true
    bucket: "botsensai-backups-538471157365"
    region: "us-east-1"
    prefix: "backups"
  r2:
    enabled: true
    bucket: "botsensai-backups"
    account_id: "<CLOUDFLARE_ACCOUNT_ID>"
    prefix: "backups"
  b2:
    enabled: false
    bucket: "botsensai-backups"
    region: "us-east-005"
    prefix: "backups"
```

### 2. Environment Variables (`.env`)

Never commit access keys or secret keys to version control. Set them in your environment:

```bash
# AWS S3 (Automatically uses IAM instance role on EC2 if credentials not set)
BOTSENSAI_BACKUP_S3_BUCKET=botsensai-backups-538471157365
AWS_REGION=us-east-1

# Cloudflare R2 (Zero Egress Bandwidth)
BOTSENSAI_BACKUP_R2_BUCKET=botsensai-backups
BOTSENSAI_BACKUP_R2_ACCOUNT_ID=your_cloudflare_account_id_here
BOTSENSAI_BACKUP_R2_ACCESS_KEY_ID=your_r2_access_key_id
BOTSENSAI_BACKUP_R2_SECRET_ACCESS_KEY=your_r2_secret_access_key

# Optional: Backblaze B2
BOTSENSAI_BACKUP_B2_BUCKET=botsensai-backups
BOTSENSAI_BACKUP_B2_REGION=us-east-005
BOTSENSAI_BACKUP_B2_ACCESS_KEY_ID=your_b2_access_key_id
BOTSENSAI_BACKUP_B2_SECRET_ACCESS_KEY=your_b2_secret_access_key
```

---

## CLI & Script Usage

### Check Configured Cloud Targets
```bash
python -m botsensai.cli backup --status
```

### Execute Immediate Multi-Cloud Mirroring
```bash
# Using Botsensai CLI
python -m botsensai.cli backup

# Mirror only to Cloudflare R2
python -m botsensai.cli backup --provider r2

# Dry-run simulation
python -m botsensai.cli backup --dry-run
```

### Using Shell Script
```bash
# Manual run
./scripts/backup_db_to_s3.sh

# Dry-run
./scripts/backup_db_to_s3.sh --dry-run

# Install automated 6-hour cron job on EC2
./scripts/backup_db_to_s3.sh --install-cron my-s3-bucket
```

### Download / Sync Remote Database (Zero Egress)
```bash
# Automatically uses Cloudflare R2 when configured to avoid egress fees
./scripts/pull_db.sh

# Force pull from Cloudflare R2
./scripts/pull_db.sh --provider r2

# Force pull from AWS S3
./scripts/pull_db.sh --provider s3

# Pull to a custom local destination
./scripts/pull_db.sh data/research_snapshot.db
```

---

## Cloudflare R2 Setup Quickstart

1. Log in to the [Cloudflare Dashboard](https://dash.cloudflare.com/).
2. Navigate to **R2 Storage** -> **Create bucket** (e.g. `botsensai-backups`).
3. Click **Manage R2 API Tokens** -> **Create API Token**.
4. Permissions: **Object Read & Write**.
5. Bucket restriction: Apply to your backup bucket or all buckets.
6. Note down:
   - **Account ID** (found on the right sidebar of the R2 overview page).
   - **Access Key ID**.
   - **Secret Access Key**.
   - **S3 API Endpoint**: `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`.
7. Add these keys to `.env` or EC2 environment.
