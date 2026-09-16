# Botsensai Mission Control & Management Guide

This guide explains how to use the interactive **Mission Control Wizard (`./scripts/botsensai-ctl`)** and standalone operational utilities to control, monitor, and inspect your live Botsensai deployment on AWS EC2.

---

## Quick Start

Launch the interactive control wizard from your local terminal:

```bash
./scripts/botsensai-ctl
# or
python3 scripts/wizard.py
```

The wizard will display a terminal menu connecting directly to your live EC2 instance (`i-0bb5f0e7d264a2937`) in `us-east-1` via the AWS Systems Manager (SSM) agent.

---

## Features & Capabilities

### 1. 📊 Live Telemetry Dashboard
* **What it does:** Queries the EC2 hypervisor and OS metrics live.
* **Under the hood:** Contacts the SSM agent on the Ubuntu VM to query `uptime`, `free -m`, `df -h /`, `systemctl is-active`, and reads table counts directly from `/home/ubuntu/Botsensai/data/botsensai.db`.
* **Telemetry shown:**
  * System load average, CPU, free RAM, and root disk usage.
  * Status of `botsensai-serve.service`.
  * Total database record counts (Launches, trades, scores, outcomes, metric values).

---

### 2. 🌐 Open Web API Tunnel (SSM Port Forwarding)
* **What it does:** Forwards port `8001` on your remote EC2 instance to `http://localhost:8001` on your Mac.
* **Why it matters:** The EC2 server is running a FastAPI REST & WebSocket server (`botsensai serve`). By port-forwarding via SSM:
  * **No public ports or security group openings** are required on AWS.
  * You can open [http://localhost:8001/docs](http://localhost:8001/docs) in your browser to interact with the OpenAPI Swagger documentation.
  * You can query API endpoints locally:
    * `/api/v1/candidates` — Top-scoring tokens currently discovered.
    * `/api/v1/metrics` — The 33 signal definitions.
    * `/api/v1/regime` — Active graduation rates and market regime.
* **Standalone command:**
  ```bash
  ./scripts/port_forward.sh
  ```

---

### 3. 🧪 Surface Health Diagnostics (`doctor`)
* **What it does:** Probes all 10 external memecoin and social data surfaces from the EC2 instance's IP address.
* **Why it matters:** Data sources (Pump.fun, Dexscreener, X, Reddit, Telegram, 4chan, etc.) have strict rate limits and Cloudflare filters. Testing them from AWS datacenter IPs confirms what data the bot is actively able to pull without throttling.
* **Standalone command:**
  ```bash
  ./scripts/remote_control.sh doctor
  ```

---

### 4. ⚡ Trigger Manual Sweep
* **What it does:** Runs a single pass of the 5-stage decision funnel:
  1. **Discover:** Ingests latest mints from Solana launchpads.
  2. **Screen:** Discards low liquidity, copycats, and low-effort tokens for free.
  3. **Enrich:** Queries trade history and holder distribution graphs for survivors.
  4. **Score:** Scores candidates against 33 anti-gaming signals.
  5. **Decide:** Simulates a paper trade fill if the score meets the convex threshold.
* **Standalone command:**
  ```bash
  ./scripts/remote_control.sh sweep
  ```

---

### 5. 📜 View Service Logs
* **What it does:** Retrieves the last $N$ lines of logs from `systemd journalctl` for `botsensai-serve.service`.
* **Standalone command:**
  ```bash
  ./scripts/remote_control.sh logs 100
  ```

---

### 6. 📥 Download Remote Database (S3 $\to$ Local)
* **What it does:** Copies the latest 470MB production database snapshot (`botsensai.db`) from `s3://botsensai-backups-538471157365/latest/botsensai.db` to your local workspace at `data/botsensai_remote.db`.
* **Why it matters:** Allows you to run local offline backtests, signal evaluations, and data analysis using 28,000+ real Solana token launches without overloading the production instance.
* **Standalone command:**
  ```bash
  ./scripts/pull_db.sh
  ```

---

### 7. 💾 Trigger Remote S3 Backup
* **What it does:** Executes an atomic online SQLite backup (`scripts/backup_db.py`) on the EC2 instance and uploads the result to your S3 backup bucket.
* **Why it matters:** Protects your 28k+ launch dataset against instance termination or EBS corruption.
* **Standalone command:**
  ```bash
  ./scripts/backup_db_to_s3.sh botsensai-backups-538471157365
  ```

---

### 8. 🔄 Service Lifecycle & Updates
* **Restart service:** Safely restarts `botsensai-serve.service` with `systemctl restart`.
* **Update code:** Runs `git pull origin main`, reinstalls the editable package (`pip install -e .`), and restarts the daemon.
* **Inspect cron schedule:** Displays scheduled automated jobs (Outcome Labeller at 02:00 UTC, Weight Fitter at 03:00 UTC, Track Record at 04:00 UTC, S3 Backup at 05:00 UTC).

---

### 9. 💻 Zero-SSH Interactive Shell
* **What it does:** Spawns a secure shell directly into the Ubuntu VM using AWS SSM Session Manager.
* **No SSH key or inbound port 22 needed.**
* **Standalone command:**
  ```bash
  ./scripts/aws_ssm_connect.sh
  ```

---

### 10. 🚨 Emergency Remote Kill Switch
* **Soft Kill:** Pauses the `botsensai-serve.service` daemon cleanly via SSM without stopping the EC2 VM (allowing you to inspect logs).
* **Hard Kill:** Issues an EC2 stop command to power off the VM immediately, halting all CPU billing and network traffic.
* **Resume:** Powers the VM back on and restarts the service.
* **Standalone command:**
  ```bash
  ./scripts/kill_switch.sh soft-kill
  ./scripts/kill_switch.sh hard-kill
  ./scripts/kill_switch.sh resume
  ```

---

## Standalone Tools Cheat-Sheet

| Tool | Purpose |
| :--- | :--- |
| **`./scripts/botsensai-ctl`** | **Interactive Mission Control Wizard (All-in-one)** |
| **`./scripts/port_forward.sh`** | Forward remote port 8001 to `http://localhost:8001/docs` |
| **`./scripts/server_stats.sh`** | Show CPU, RAM, Disk, and Load average on EC2 |
| **`./scripts/pull_db.sh`** | Download latest remote SQLite database to local disk |
| **`./scripts/aws_ssm_connect.sh`** | Open interactive terminal shell into Ubuntu VM |
| **`./scripts/remote_control.sh <cmd>`** | Non-interactive command runner (`status`, `doctor`, `logs`, `restart`, `update`) |
| **`./scripts/kill_switch.sh <mode>`** | Soft/hard kill switch for emergencies |
