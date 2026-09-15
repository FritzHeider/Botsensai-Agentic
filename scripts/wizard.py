#!/usr/bin/env python3
"""
Botsensai Mission Control & Management Wizard
An interactive CLI and operational console for monitoring, controlling,
and diagnosing the live Botsensai deployment on AWS EC2.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

try:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
    from rich.text import Text
except ImportError:
    print("Error: 'rich' library is required. Install with: pip install rich")
    sys.exit(1)

console = Console()

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
DEFAULT_INSTANCE_ID = os.environ.get("BOTSENSAI_INSTANCE_ID", "i-0bb5f0e7d264a2937")
DEFAULT_REGION = os.environ.get("AWS_REGION", "us-east-1")
DEFAULT_PROFILE = os.environ.get("AWS_PROFILE", "agent-profile")
DEFAULT_BUCKET = os.environ.get("BOTSENSAI_BACKUP_S3_BUCKET", "botsensai-backups-538471157365")
REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# AWS SSM Helpers
# --------------------------------------------------------------------------- #
def run_ssm_command(cmd: str, desc: str = "Running command", timeout: int = 40) -> tuple[int, str, str]:
    """Execute a shell command on the remote EC2 instance via AWS SSM."""
    with console.status(f"[bold cyan]SSM:[/bold cyan] {desc} on [yellow]{DEFAULT_INSTANCE_ID}[/yellow]..."):
        params_json = json.dumps({"commands": [cmd]})
        cmd_args = [
            "aws", "ssm", "send-command",
            "--instance-ids", DEFAULT_INSTANCE_ID,
            "--region", DEFAULT_REGION,
            "--document-name", "AWS-RunShellScript",
            "--comment", f"Botsensai Wizard: {desc[:50]}",
            "--parameters", params_json,
            "--query", "Command.CommandId",
            "--output", "text"
        ]
        if DEFAULT_PROFILE:
            cmd_args.extend(["--profile", DEFAULT_PROFILE])

        try:
            p = subprocess.run(cmd_args, capture_output=True, text=True, check=True)
            cmd_id = p.stdout.strip()
        except subprocess.CalledProcessError as e:
            return 1, "", f"Failed to dispatch SSM command: {e.stderr}"

        # Wait for command execution
        wait_args = [
            "aws", "ssm", "wait", "command-executed",
            "--command-id", cmd_id,
            "--instance-id", DEFAULT_INSTANCE_ID,
            "--region", DEFAULT_REGION
        ]
        if DEFAULT_PROFILE:
            wait_args.extend(["--profile", DEFAULT_PROFILE])
        subprocess.run(wait_args, capture_output=True)

        # Get command invocation
        get_args = [
            "aws", "ssm", "get-command-invocation",
            "--command-id", cmd_id,
            "--instance-id", DEFAULT_INSTANCE_ID,
            "--region", DEFAULT_REGION,
            "--query", "{Status:Status,StandardOutputContent:StandardOutputContent,StandardErrorContent:StandardErrorContent}",
            "--output", "json"
        ]
        if DEFAULT_PROFILE:
            get_args.extend(["--profile", DEFAULT_PROFILE])

        p2 = subprocess.run(get_args, capture_output=True, text=True)
        if p2.returncode != 0:
            return 1, "", f"Failed to retrieve command output: {p2.stderr}"

        try:
            res = json.loads(p2.stdout)
            status = res.get("Status", "Unknown")
            stdout = res.get("StandardOutputContent", "")
            stderr = res.get("StandardErrorContent", "")
            exit_code = 0 if status == "Success" else 1
            return exit_code, stdout, stderr
        except json.JSONDecodeError:
            return 1, p2.stdout, p2.stderr


# --------------------------------------------------------------------------- #
# Menu Feature Functions
# --------------------------------------------------------------------------- #

def show_dashboard():
    """Fetches and displays real-time health, server performance, and DB telemetry."""
    console.print(Panel("[bold green]Querying Live Cloud Telemetry...[/bold green]\n"
                        "[dim]Contacting AWS EC2 & SSM Agent to inspect VM health, process status, and store size.[/dim]",
                        border_style="green"))

    # 1. Query Server stats
    stats_cmd = (
        "echo '===STATS===' && uptime && "
        "echo '===MEM===' && free -m && "
        "echo '===DISK===' && df -h / && "
        "echo '===SERVICE===' && systemctl is-active botsensai-serve.service || echo 'inactive' && "
        "echo '===STORE===' && sudo -u ubuntu -i bash -c 'cd /home/ubuntu/Botsensai && .venv/bin/python3 -c \""
        "from botsensai.store.db import Database; from botsensai.config import get_settings; "
        "import json; print(json.dumps(Database(get_settings().path(get_settings().db_path)).counts()))\" 2>/dev/null || echo \"{}\"'"
    )

    code, out, err = run_ssm_command(stats_cmd, "Fetching live telemetry")
    if code != 0:
        console.print(f"[bold red]Failed to query instance:[/bold red] {err}")
        return

    # Parse sections
    sections = {}
    current = None
    for line in out.splitlines():
        if line.startswith("===") and line.endswith("==="):
            current = line.strip("=")
            sections[current] = []
        elif current:
            sections[current].append(line)

    # Server Info Table
    info_tbl = Table(title="AWS EC2 Runtime Telemetry", box=box.ROUNDED)
    info_tbl.add_column("Property", style="cyan")
    info_tbl.add_column("Status / Value", style="bold white")
    info_tbl.add_row("Instance ID", DEFAULT_INSTANCE_ID)
    info_tbl.add_row("Region & Profile", f"{DEFAULT_REGION} ({DEFAULT_PROFILE})")

    uptime_lines = sections.get("STATS", ["Unknown"])
    info_tbl.add_row("Uptime & Load", uptime_lines[0].strip() if uptime_lines else "N/A")

    svc_lines = sections.get("SERVICE", ["unknown"])
    svc_status = svc_lines[0].strip() if svc_lines else "unknown"
    svc_color = "green" if svc_status == "active" else "red"
    info_tbl.add_row("Background Service", f"[{svc_color}]{svc_status} (botsensai-serve.service)[/{svc_color}]")

    disk_lines = sections.get("DISK", [])
    if len(disk_lines) >= 2:
        parts = disk_lines[1].split()
        if len(parts) >= 5:
            info_tbl.add_row("Root Disk Usage", f"{parts[2]} used / {parts[1]} total ({parts[4]} full)")

    mem_lines = sections.get("MEM", [])
    if len(mem_lines) >= 2:
        parts = mem_lines[1].split()
        if len(parts) >= 7:
            info_tbl.add_row("RAM Usage", f"{parts[2]} MB used / {parts[1]} MB total ({parts[6]} MB available)")

    console.print(info_tbl)

    # Store Telemetry Table
    store_lines = sections.get("STORE", ["{}"])
    store_json_str = "".join(store_lines).strip()
    try:
        counts = json.loads(store_json_str)
        if counts:
            store_tbl = Table(title="Live Database Telemetry (data/botsensai.db)", box=box.ROUNDED)
            store_tbl.add_column("Entity / Table", style="yellow")
            store_tbl.add_column("Total Count", justify="right", style="bold green")
            for table_name, count in counts.items():
                store_tbl.add_row(table_name, f"{count:,}")
            console.print(store_tbl)
    except Exception:
        console.print("[yellow]Store telemetry parsing skipped.[/yellow]")


def run_doctor():
    """Runs botsensai doctor and explains surface health."""
    console.print(Panel(
        "[bold cyan]What is this?[/bold cyan]\n"
        "`botsensai doctor` probes all 10 external memecoin and social data surfaces\n"
        "(Pump.fun, Dexscreener, GeckoTerminal, X, Reddit, 4chan, Telegram, TikTok, etc.)\n"
        "directly from the AWS datacenter IP to confirm which endpoints are unblocked.",
        title="Surface Health Diagnostics", border_style="cyan"
    ))

    cmd = "sudo -u ubuntu -i bash -c 'cd /home/ubuntu/Botsensai && .venv/bin/botsensai doctor --config config/botsensai.yaml'"
    code, out, err = run_ssm_command(cmd, "Running surface doctor")
    if out:
        console.print(out)
    if err:
        console.print(f"[yellow]{err}[/yellow]")


def open_port_forward():
    """Starts SSM Port Forwarding for local Swagger / REST UI exploration."""
    console.print(Panel(
        "[bold green]SSM Encrypted Port Forwarding Tunnel[/bold green]\n\n"
        "This tool sets up a secure tunnel from your Mac to port [bold]8001[/bold] on the EC2 instance.\n"
        "No public security group rules or firewall openings are created.\n\n"
        "• Local URL: [bold underline cyan]http://localhost:8001/docs[/bold underline cyan] (Interactive Swagger API)\n"
        "• Telemetry: [bold underline cyan]http://localhost:8001/api/v1/candidates[/bold underline cyan]\n"
        "• Metrics:   [bold underline cyan]http://localhost:8001/api/v1/metrics[/bold underline cyan]\n\n"
        "[dim]Press Ctrl+C in this terminal when you are done to close the tunnel.[/dim]",
        border_style="green"
    ))

    if Confirm.ask("Launch port forwarding tunnel now?", default=True):
        tunnel_cmd = [
            "aws", "ssm", "start-session",
            "--target", DEFAULT_INSTANCE_ID,
            "--region", DEFAULT_REGION,
            "--document-name", "AWS-StartPortForwardingSession",
            "--parameters", '{"portNumber":["8001"],"localPortNumber":["8001"]}'
        ]
        if DEFAULT_PROFILE:
            tunnel_cmd.extend(["--profile", DEFAULT_PROFILE])

        console.print("[cyan]Tunnel active! Open http://localhost:8001/docs in your browser...[/cyan]")
        try:
            subprocess.run(tunnel_cmd)
        except KeyboardInterrupt:
            console.print("\n[yellow]Tunnel closed.[/yellow]")


def view_logs():
    """Views and filters live service logs."""
    n_lines = Prompt.ask("How many lines of logs to retrieve?", default="50")
    cmd = f"sudo journalctl -u botsensai-serve.service -n {n_lines} --no-pager"
    code, out, err = run_ssm_command(cmd, f"Retrieving last {n_lines} log lines")
    console.print(Panel(out or "No log output found.", title=f"Last {n_lines} Lines of botsensai-serve.service", border_style="blue"))


def show_paper_trades():
    """Displays paper-trading track record and performance metrics."""
    console.print(Panel(
        "[bold cyan]Paper-Trading Track Record[/bold cyan]\n"
        "Inspect historical paper trades, win rate, expectancy, and PnL.\n"
        "The system evaluates all decisions through realistic fill models with slippage.",
        border_style="cyan"
    ))

    track_file = REPO_ROOT / "docs" / "TRACK_RECORD.md"
    if track_file.exists():
        console.print(Panel(track_file.read_text(encoding="utf-8").strip(), title="Latest Track Record (docs/TRACK_RECORD.md)", border_style="green"))
    else:
        console.print("[yellow]Local docs/TRACK_RECORD.md not found. Querying instance...[/yellow]")
        cmd = "sudo -u ubuntu -i bash -c 'cd /home/ubuntu/Botsensai && .venv/bin/botsensai track-record --days 7.0'"
        code, out, err = run_ssm_command(cmd, "Generating track record on EC2")
        console.print(out or err)

    if Confirm.ask("\nRe-evaluate rolling track record on EC2 now?", default=False):
        days = Prompt.ask("History window in days (e.g. 7.0 for 1 week, 30.0 for 1 month)", default="7.0")
        cmd = f"sudo -u ubuntu -i bash -c 'cd /home/ubuntu/Botsensai && .venv/bin/botsensai track-record --days {days} --out docs/TRACK_RECORD.md && cat docs/TRACK_RECORD.md'"
        code, out, err = run_ssm_command(cmd, f"Evaluating paper track record over last {days} days")
        if out:
            console.print(Panel(out, title=f"Fresh Track Record ({days} days)", border_style="green"))
            # Save locally
            (REPO_ROOT / "docs" / "TRACK_RECORD.md").write_text(out, encoding="utf-8")


def trigger_snipe():
    """Discovers launches, scores them, and enters a paper trade on the #1 token."""
    console.print(Panel(
        "[bold cyan]Snipe Mode (Paper Execution)[/bold cyan]\n"
        "Discovers the freshest Solana launches, deeply enriches their trade history,\n"
        "evaluates 33 signals, and simulates a paper buy on the highest-conviction token.",
        border_style="cyan"
    ))

    size = Prompt.ask("Simulated position size in SOL", default="0.25")
    min_score = Prompt.ask("Minimum score conviction threshold", default="0.68")
    force = Confirm.ask("Force buy even if below score threshold or vetoed?", default=False)

    force_flag = "--force" if force else ""
    cmd = f"sudo -u ubuntu -i bash -c 'cd /home/ubuntu/Botsensai && .venv/bin/botsensai snipe --size {size} --min-score {min_score} {force_flag}'"
    code, out, err = run_ssm_command(cmd, f"Running snipe (size={size} SOL, min_score={min_score})")
    if out:
        console.print(Panel(out, title="Sniper Fill Result", border_style="green"))
    if err:
        console.print(f"[yellow]{err}[/yellow]")


def open_html_dashboard():
    """Generates a self-contained HTML dashboard on EC2, downloads it, and opens it in browser."""
    console.print(Panel(
        "[bold cyan]Self-Contained HTML Dashboard[/bold cyan]\n"
        "Generates a rich, single-file HTML dashboard on the EC2 instance containing\n"
        "integrity checks, candidate rankings, and surface metrics, then opens it in your Mac browser.",
        border_style="cyan"
    ))

    dash_file = REPO_ROOT / "data" / "dashboard.html"
    dash_file.parent.mkdir(exist_ok=True)

    if Confirm.ask("Generate fresh dashboard from EC2 now?", default=True):
        cmd = "sudo -u ubuntu -i bash -c 'cd /home/ubuntu/Botsensai && .venv/bin/botsensai dashboard --out data/dashboard.html && base64 -w 0 data/dashboard.html'"
        code, out, err = run_ssm_command(cmd, "Generating dashboard on EC2")
        if code == 0 and out:
            try:
                import base64
                b64_data = out.strip().splitlines()[-1]
                content = base64.b64decode(b64_data)
                dash_file.write_bytes(content)
                console.print(f"[green]Saved dashboard to {dash_file} ({len(content):,} bytes)[/green]")
            except Exception as e:
                console.print(f"[yellow]Failed to decode dashboard content: {e}[/yellow]")

    if dash_file.exists():
        console.print(f"[bold green]Opening {dash_file} in your browser...[/bold green]")
        subprocess.run(["open", str(dash_file)])
    else:
        console.print("[red]Dashboard file not found.[/red]")


def launch_live_tui():
    """Launches the interactive split-pane TUI on the instance via SSM."""
    console.print(Panel(
        "[bold cyan]Live Terminal User Interface (TUI)[/bold cyan]\n"
        "Spawns a full-screen, split-pane terminal dashboard on the EC2 instance\n"
        "showing live incoming launches, score progression, and veto gates.\n\n"
        "[dim]Press Ctrl+C inside the TUI to exit back to this menu.[/dim]",
        border_style="cyan"
    ))

    if Confirm.ask("Launch live TUI now?", default=True):
        cmd = [
            "aws", "ssm", "start-session",
            "--target", DEFAULT_INSTANCE_ID,
            "--region", DEFAULT_REGION,
            "--document-name", "AWS-StartInteractiveCommand",
            "--parameters", '{"command": ["sudo -u ubuntu -i bash -c \'cd /home/ubuntu/Botsensai && .venv/bin/botsensai tui\'"]}'
        ]
        if DEFAULT_PROFILE:
            cmd.extend(["--profile", DEFAULT_PROFILE])
        try:
            subprocess.run(cmd)
        except Exception:
            # Fallback to direct SSM start session
            cmd_fallback = [
                "aws", "ssm", "start-session",
                "--target", DEFAULT_INSTANCE_ID,
                "--region", DEFAULT_REGION
            ]
            if DEFAULT_PROFILE:
                cmd_fallback.extend(["--profile", DEFAULT_PROFILE])
            console.print("[yellow]Opening shell. Run: cd ~/Botsensai && .venv/bin/botsensai tui[/yellow]")
            subprocess.run(cmd_fallback)


def trigger_sweep():
    """Runs a manual recon & paper scoring sweep on the instance."""
    console.print(Panel(
        "[bold cyan]What is a Sweep?[/bold cyan]\n"
        "A sweep executes one full pass of the decision funnel:\n"
        "1. [bold]Discover[/bold] brand new token mints from Solana launchpads.\n"
        "2. [bold]Screen[/bold] out bad liquidity, copycats, and low-effort tokens.\n"
        "3. [bold]Enrich[/bold] survivor tokens with trade histories and holder graphs.\n"
        "4. [bold]Score[/bold] against 33 anti-gaming signals.\n"
        "5. [bold]Decide[/bold] whether to simulate a paper trade fill.",
        border_style="cyan"
    ))

    limit = Prompt.ask("Launches to discover", default="30")
    candidates = Prompt.ask("Candidates to enrich & score", default="10")

    cmd = f"sudo -u ubuntu -i bash -c 'cd /home/ubuntu/Botsensai && .venv/bin/botsensai sweep --limit {limit} --candidates {candidates}'"
    code, out, err = run_ssm_command(cmd, f"Running sweep (limit={limit}, candidates={candidates})")
    if out:
        console.print(out)
    if err:
        console.print(f"[yellow]{err}[/yellow]")


def pull_database():
    """Downloads remote database from Cloudflare R2 or S3."""
    console.print(Panel(
        "[bold green]Database Sync (Multi-Cloud -> Local)[/bold green]\n"
        "Downloads the latest database snapshot to your local workspace at\n"
        "[yellow]data/botsensai_remote.db[/yellow] for offline backtesting and evaluation.\n"
        "Automatically routes through [cyan]Cloudflare R2 (zero egress bandwidth fees)[/cyan]\n"
        "when configured, with fallback to Amazon S3.",
        title="Sync Remote Database", border_style="green"
    ))

    if Confirm.ask("Download latest snapshot now?", default=True):
        pull_script = REPO_ROOT / "scripts" / "pull_db.sh"
        subprocess.run(["bash", str(pull_script)], cwd=str(REPO_ROOT))


def trigger_s3_backup():
    """Forces an immediate atomic backup on the remote instance mirrored across clouds."""
    console.print(Panel(
        "Triggers an atomic `.backup` snapshot of `data/botsensai.db` on EC2\n"
        "and mirrors it to [bold cyan]Amazon S3[/bold cyan] and [bold green]Cloudflare R2 (Zero Egress)[/bold green].",
        title="Trigger Multi-Cloud Database Backup", border_style="magenta"
    ))

    if Confirm.ask("Run atomic backup on EC2 now?", default=True):
        cmd = "sudo -u ubuntu -i bash -c 'cd /home/ubuntu/botsensai && /usr/bin/python3 scripts/backup_db.py'"
        code, out, err = run_ssm_command(cmd, "Executing atomic database backup to S3 + R2")
        console.print(out or "Backup script finished.")
        if err:
            console.print(f"[yellow]{err}[/yellow]")


def service_lifecycle():
    """Provides service restart, stop, and code update options."""
    console.print(Panel(
        "1. [bold green]Restart Service[/bold green] (systemctl restart botsensai-serve)\n"
        "2. [bold cyan]Update Code & Reinstall[/bold cyan] (git pull origin main && pip install -e .)\n"
        "3. [bold yellow]View Cron Schedule[/bold yellow] (inspection of daily automated tasks)\n"
        "4. [bold red]Stop Service[/bold red] (pause background processing)\n"
        "5. [bold green]Start Service[/bold green] (resume background processing)\n"
        "6. Back to main menu",
        title="Service Lifecycle & Maintenance", border_style="yellow"
    ))

    choice = Prompt.ask("Choose action", choices=["1", "2", "3", "4", "5", "6"], default="1")
    if choice == "1":
        code, out, _ = run_ssm_command("sudo systemctl restart botsensai-serve.service && sudo systemctl status botsensai-serve.service --no-pager", "Restarting service")
        console.print(out)
    elif choice == "2":
        code, out, _ = run_ssm_command("sudo -u ubuntu -i bash -c 'cd /home/ubuntu/Botsensai && git pull origin main && .venv/bin/pip install -e . && sudo systemctl restart botsensai-serve.service && sudo systemctl status botsensai-serve.service --no-pager'", "Updating code from GitHub")
        console.print(out)
    elif choice == "3":
        code, out, _ = run_ssm_command("sudo -u ubuntu -i bash -c 'crontab -l'", "Reading user crontab")
        console.print(Panel(out or "No user crontab configured.", title="Automated Cron Tasks on EC2", border_style="cyan"))
    elif choice == "4":
        code, out, _ = run_ssm_command("sudo systemctl stop botsensai-serve.service && echo 'Service stopped.'", "Stopping service")
        console.print(out)
    elif choice == "5":
        code, out, _ = run_ssm_command("sudo systemctl start botsensai-serve.service && sudo systemctl status botsensai-serve.service --no-pager", "Starting service")
        console.print(out)


def open_ssm_shell():
    """Spawns an interactive shell on the remote instance via SSM Session Manager."""
    console.print(Panel(
        "[bold green]Starting Zero-SSH Interactive Shell[/bold green]\n"
        "Connecting via AWS Systems Manager Session Manager.\n"
        "Once connected, run: [bold cyan]sudo su - ubuntu && cd ~/Botsensai[/bold cyan]\n"
        "Type [bold]exit[/bold] when done to return to this menu.",
        border_style="green"
    ))

    cmd = ["aws", "ssm", "start-session", "--target", DEFAULT_INSTANCE_ID, "--region", DEFAULT_REGION]
    if DEFAULT_PROFILE:
        cmd.extend(["--profile", DEFAULT_PROFILE])
    subprocess.run(cmd)


def emergency_kill_switch():
    """Emergency options to freeze processing or stop the VM."""
    console.print(Panel(
        "[bold red]EMERGENCY KILL SWITCH[/bold red]\n\n"
        "[bold yellow]Soft Kill:[/bold yellow] Halts `botsensai-serve.service` immediately via SSM.\n"
        "The EC2 instance remains running so you can inspect logs.\n\n"
        "[bold red]Hard Kill:[/bold red] Shuts down the entire EC2 instance via AWS EC2 API.\n"
        "Completely freezes compute spend and all network activity.",
        border_style="red"
    ))

    action = Prompt.ask("Select kill mode", choices=["soft-kill", "hard-kill", "status", "resume", "cancel"], default="cancel")
    if action == "cancel":
        return

    kill_script = REPO_ROOT / "scripts" / "kill_switch.sh"
    subprocess.run(["bash", str(kill_script), action, DEFAULT_INSTANCE_ID], cwd=str(REPO_ROOT))


def explain_architecture():
    """Displays an educational walkthrough of the system architecture."""
    text = """
[bold underline cyan]The 5-Stage Decision Funnel[/bold underline cyan]
  1. [bold]Discover[/bold] (~165 launches/sweep from Pump.fun, Dexscreener, GeckoTerminal)
  2. [bold]Screen[/bold] (free local filter removing >90% of spam & low liquidity)
  3. [bold]Enrich[/bold] (collect trade histories, holder graphs, and social signals)
  4. [bold]Score[/bold] (evaluates 33 signals across 6 families; vetoes trigger on fraud)
  5. [bold]Decide[/bold] (convex Kelly-sizing with modeled slippage and fee friction)

[bold underline cyan]The 6 Signal Families & Cost-to-Fake[/bold underline cyan]
  • [bold]onchain_topology (7 signals):[/bold] Measures independent wallet clusters, sniper share, and fresh wallet funding.
  • [bold]social_authenticity (9 signals):[/bold] Distinguishes genuine organic interest from sybil bot farms and engagement packages.
  • [bold]community_production (5 signals):[/bold] Measures unpaid third-party creative effort (fan art, derivative memes, remix depth).
  • [bold]narrative (5 signals):[/bold] Novelty, cultural resonance, and search contention.
  • [bold]team_credibility (3 signals):[/bold] Historical dev track record and deployer bag retention.
  • [bold]execution_quality (4 signals):[/bold] Price impact and realizable liquidity for an exit.

[bold underline cyan]Point-in-Time Correctness[/bold underline cyan]
  Every record carries both `as_of` (when it happened) and `observed_at` (when we learned it).
  The backtester strictly filters on both to mathematically prevent look-ahead bias.
"""
    console.print(Panel(text.strip(), title="Botsensai 2.0 Architectural Overview", border_style="cyan"))
    Prompt.ask("\nPress Enter to return to menu")


# --------------------------------------------------------------------------- #
# Main Menu Loop
# --------------------------------------------------------------------------- #
def main():
    while True:
        console.clear()
        banner = Text()
        banner.append("🤖 BOTSENSAI MISSION CONTROL WIZARD\n", style="bold green")
        banner.append(f"Target Instance: {DEFAULT_INSTANCE_ID} | Region: {DEFAULT_REGION} | Profile: {DEFAULT_PROFILE}\n", style="dim cyan")
        banner.append("AWS Systems Manager Zero-SSH Orchestrator", style="italic white")
        console.print(Panel(banner, box=box.DOUBLE, border_style="green"))

        menu_tbl = Table(show_header=False, box=box.SIMPLE)
        menu_tbl.add_column("Key", style="bold cyan", width=4)
        menu_tbl.add_column("Action", style="bold white")
        menu_tbl.add_column("Description", style="dim")

        menu_tbl.add_row("1", "📊 Live Telemetry Dashboard", "Inspect EC2 CPU/RAM/Disk, service status & DB counts")
        menu_tbl.add_row("2", "🌐 Open Web API Tunnel", "Forward port 8001 locally to browse Swagger UI & API docs")
        menu_tbl.add_row("3", "🧪 Run Surface Doctor", "Probe Pump.fun, X, Telegram, Reddit & DEX endpoints")
        menu_tbl.add_row("4", "⚡ Trigger Manual Sweep", "Execute a live discovery, scoring, and screening pass")
        menu_tbl.add_row("5", "🎯 Snipe Mode (Paper Execution)", "Discover, score, and paper-buy the #1 best token")
        menu_tbl.add_row("6", "📈 Paper Trades & Track Record", "View realized PnL (+78.8 SOL), win rate & trade log")
        menu_tbl.add_row("7", "🖥 Open HTML Dashboard in Browser", "Self-contained visual report with candidate rankings")
        menu_tbl.add_row("8", "📟 Launch Live TUI Terminal Dashboard", "Split-pane full-screen streaming interface on EC2")
        menu_tbl.add_row("9", "📜 View Service Logs", "Tail journalctl logs for botsensai-serve.service")
        menu_tbl.add_row("10", "📥 Download Database (R2/S3 -> Local)", "Pull database snapshot (zero egress fees via Cloudflare R2)")
        menu_tbl.add_row("11", "💾 Trigger Multi-Cloud Backup", "Force immediate atomic SQLite backup to S3 & Cloudflare R2")
        menu_tbl.add_row("12", "🔄 Service Lifecycle & Updates", "Restart daemon, pull git updates, or view cron jobs")
        menu_tbl.add_row("13", "💻 Interactive SSM Terminal", "Open zero-SSH terminal session directly on Ubuntu")
        menu_tbl.add_row("14", "🚨 Emergency Kill Switch", "Soft-stop background daemon or power off EC2 instance")
        menu_tbl.add_row("15", "📖 Architecture & Signals Guide", "Educational overview of the 33 signals and the funnel")
        menu_tbl.add_row("0", "👋 Exit", "Close the control center")

        console.print(menu_tbl)

        choice = Prompt.ask("\n[bold yellow]Select option[/bold yellow]", choices=["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15"], default="1")

        if choice == "0":
            console.print("[green]Goodbye![/green]")
            break
        elif choice == "1":
            show_dashboard()
            Prompt.ask("\nPress Enter to continue")
        elif choice == "2":
            open_port_forward()
        elif choice == "3":
            run_doctor()
            Prompt.ask("\nPress Enter to continue")
        elif choice == "4":
            trigger_sweep()
            Prompt.ask("\nPress Enter to continue")
        elif choice == "5":
            trigger_snipe()
            Prompt.ask("\nPress Enter to continue")
        elif choice == "6":
            show_paper_trades()
            Prompt.ask("\nPress Enter to continue")
        elif choice == "7":
            open_html_dashboard()
            Prompt.ask("\nPress Enter to continue")
        elif choice == "8":
            launch_live_tui()
        elif choice == "9":
            view_logs()
            Prompt.ask("\nPress Enter to continue")
        elif choice == "10":
            pull_database()
            Prompt.ask("\nPress Enter to continue")
        elif choice == "11":
            trigger_s3_backup()
            Prompt.ask("\nPress Enter to continue")
        elif choice == "12":
            service_lifecycle()
            Prompt.ask("\nPress Enter to continue")
        elif choice == "13":
            open_ssm_shell()
            Prompt.ask("\nPress Enter to continue")
        elif choice == "14":
            emergency_kill_switch()
            Prompt.ask("\nPress Enter to continue")
        elif choice == "15":
            explain_architecture()


if __name__ == "__main__":
    main()
