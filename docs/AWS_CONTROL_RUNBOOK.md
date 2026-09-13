# AWS Control & Operations Runbook

This runbook details how to operate, monitor, control, and back up **Botsensai** on an Ubuntu EC2 instance in AWS.

---

## Architecture & Layout

```
Local Machine (Operator)
   │
   ├── AWS SSM Session Manager  ──►  Interactive Shell on Ubuntu (Zero SSH)
   ├── scripts/remote_control.sh ──►  AWS SSM Run Command (status / doctor / logs / update)
   ├── scripts/pull_db.sh       ──►  Pull Latest Database (Prioritizes R2 zero-egress sync)
   └── scripts/kill_switch.sh   ──►  Soft Kill (stop service) / Hard Kill (stop instance)

Ubuntu EC2 Instance
   ├── /etc/systemd/system/botsensai.service  (Continuous background daemon)
   ├── /home/ubuntu/botsensai/data/botsensai.db (SQLite database)
   ├── scripts/backup_db_to_s3.sh              (Multi-Cloud mirror: S3 + Cloudflare R2)
   └── scripts/run_doctor.sh                   (Surface diagnostics)
```

---

## 1. Connect to the Instance (Zero-SSH via SSM)

With `AmazonSSMManagedInstanceCore` attached to the instance role, connect directly from your local terminal without opening port 22 or managing key pairs:

```bash
# Using the helper script
./scripts/aws_ssm_connect.sh <INSTANCE_ID>

# Or directly via AWS CLI
aws ssm start-session --target <INSTANCE_ID>
```

Once connected to the instance:
```bash
sudo su - ubuntu
cd /home/ubuntu/botsensai
```

---

## 2. Day-to-Day Service Control (systemd)

The daemon is managed by systemd as `botsensai.service`.

### Initial Service Installation (on the instance)
```bash
sudo /home/ubuntu/botsensai/scripts/install_service.sh
```

### Management Commands (run on the instance)
* **Check Status:** `sudo systemctl status botsensai.service`
* **Restart Daemon:** `sudo systemctl restart botsensai.service`
* **Stop Daemon:** `sudo systemctl stop botsensai.service`
* **Start Daemon:** `sudo systemctl start botsensai.service`
* **Live Logs (follow):** `sudo journalctl -u botsensai.service -f -n 100`

---

## 3. Run Diagnostics & Pipeline Commands on Instance

Run these on the instance inside `/home/ubuntu/botsensai`:

```bash
# Surface connectivity check
./scripts/run_doctor.sh

# Database table counts and file size
./scripts/check_db.sh

# Registered metrics catalogue
source .venv/bin/activate
botsensai metrics

# Manual single sweep
botsensai sweep --limit 40 --candidates 10
```

---

## 4. Remote Control (Without Logging In)

From your **local machine**, use `scripts/remote_control.sh` to issue commands to the Ubuntu instance via AWS Systems Manager:

```bash
# Check service status
./scripts/remote_control.sh status <INSTANCE_ID>

# Run surface diagnostics
./scripts/remote_control.sh doctor <INSTANCE_ID>

# View recent service logs
./scripts/remote_control.sh logs <INSTANCE_ID> 100

# Check database counts
./scripts/remote_control.sh db-stats <INSTANCE_ID>

# Restart the daemon
./scripts/remote_control.sh restart <INSTANCE_ID>

# Update code (git pull, reinstall, restart daemon)
./scripts/remote_control.sh update <INSTANCE_ID>
```

> [!TIP]
> Export `export BOTSENSAI_INSTANCE_ID=<INSTANCE_ID>` in your local shell to omit the instance ID on every call.

---

## 5. Multi-Cloud Backups for SQLite (AWS S3 + Cloudflare R2)

SQLite writes to `/home/ubuntu/botsensai/data/botsensai.db`. To safeguard against EBS loss or instance replacement and eliminate expensive egress fees for research downloads, snapshots are mirrored across **AWS S3** and **Cloudflare R2 (zero egress fees)**.

See [docs/MULTI_CLOUD_BACKUP.md](file:///Users/drop/.gemini/antigravity/worktrees/Botsensai/setup_s3_r2_mirroring/docs/MULTI_CLOUD_BACKUP.md) for full configuration details.

### Manual Multi-Cloud Backup (EC2 or Local):
```bash
# Using the shell script
./scripts/backup_db_to_s3.sh

# Using Botsensai CLI
python -m botsensai.cli backup

# Dry-run verification
./scripts/backup_db_to_s3.sh --dry-run
```

### Automated Cron Backup:
To install an automated 6-hour multi-cloud backup cron job on the Ubuntu instance:
```bash
./scripts/backup_db_to_s3.sh --install-cron my-botsensai-backups-bucket
```

### Pulling Database to Local Machine (Zero Egress via R2):
```bash
# Automatically pulls from Cloudflare R2 if configured ($0 egress bandwidth)
./scripts/pull_db.sh

# Force pull from AWS S3
./scripts/pull_db.sh --provider s3
```

---

## 6. Observability & Remote Kill-Switch

### Emergency Kill Switch (from your local machine)
* **Soft Kill** (stops daemon via SSM without terminating the VM):
  ```bash
  ./scripts/kill_switch.sh soft-kill <INSTANCE_ID>
  ```
* **Hard Kill** (instantly stops the EC2 instance):
  ```bash
  ./scripts/kill_switch.sh hard-kill <INSTANCE_ID>
  ```
* **Resume Operations**:
  ```bash
  ./scripts/kill_switch.sh resume <INSTANCE_ID>
  ```
* **Inspect State**:
  ```bash
  ./scripts/kill_switch.sh status <INSTANCE_ID>
  ```

---

## Required AWS IAM Permissions

Ensure the EC2 Instance Profile has the following policies attached:

1. **`arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore`** (Required for SSM Session Manager and Run Command).
2. **S3 Backup Policy** (Allow instance to write DB snapshots to your S3 bucket):
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": [
           "s3:PutObject",
           "s3:GetObject"
         ],
         "Resource": "arn:aws:s3:::my-botsensai-backups-bucket/*"
       }
     ]
   }
   ```
