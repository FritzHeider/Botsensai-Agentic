"""
Botsensai MonitorAgent — self-healing persistent monitoring agent.

Replaces the manual 5-min cron with a smarter AGY-powered watchdog that:
  - Checks live wallet balance every 5 minutes
  - Pulls sweep log from EC2 via SSM
  - Detects Helius 429 congestion windows and logs their duration
  - Detects daemon staleness (no sweep within 10 minutes)
  - Self-heals: alerts on unexpected balance drops, stale daemon, or AWS expiry
  - Reports a clean status summary each cycle

Runs as a standalone agent (not a subagent of Orchestrator).
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any

try:
    from google.antigravity import Agent, LocalAgentConfig, types
    from google.antigravity.triggers import every, TriggerContext
    from google.antigravity.hooks import hooks
    HAS_AGY_SDK = True
except ImportError:
    Agent = None  # type: ignore[assignment]
    LocalAgentConfig = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    every = None  # type: ignore[assignment]
    TriggerContext = None  # type: ignore[assignment]
    hooks = None  # type: ignore[assignment]
    HAS_AGY_SDK = False

log = logging.getLogger("monitor")

HOT_WALLET = "ChgCuBWDvGFwnW77kU523JcFXc2CryX3J4rEWDzWmtsS"
HELIUS_API_KEY = "4cb5eb58-aaf8-482a-ba63-fc8ff63c270e"
EC2_INSTANCE = "i-0bb5f0e7d264a2937"
EC2_REGION = "us-east-1"
AWS_PROFILE = "agent-profile"
DAEMON_STALE_THRESHOLD_SECONDS = 600  # 10 minutes without a sweep = stale daemon
GAS_FLOOR_SOL = 0.010
ALERT_DROP_SOL = 0.035  # unexpected balance drop larger than max position size

# ── Monitor tools ─────────────────────────────────────────────────────────────

def get_live_balance() -> str:
    """Fetch the live hot wallet SOL balance via Helius RPC.

    Returns:
        JSON with balance_sol, buffer_above_floor, gas_floor_safe (bool).
    """
    rpc_url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    payload = {
        "jsonrpc": "2.0",
        "id": "monitor-balance",
        "method": "getBalance",
        "params": [HOT_WALLET],
    }
    try:
        req = urllib.request.Request(
            rpc_url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        lamports = data.get("result", {}).get("value", 0)
        sol = lamports / 1_000_000_000
        buffer = max(0.0, sol - GAS_FLOOR_SOL)
        return json.dumps({
            "wallet": HOT_WALLET,
            "balance_sol": round(sol, 6),
            "gas_floor_sol": GAS_FLOOR_SOL,
            "buffer_above_floor_sol": round(buffer, 6),
            "gas_floor_safe": sol >= GAS_FLOOR_SOL + 0.003,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        return json.dumps({"error": str(exc), "wallet": HOT_WALLET})


def get_sweep_log() -> str:
    """Pull the last 6 sweep log lines from EC2 via AWS SSM.

    Returns:
        JSON with sweep_lines (list), ssm_available (bool), and helius_healthy (bool).
        If AWS session expired, ssm_available=false and sweep_lines=[].
    """
    try:
        r = subprocess.run(
            [
                "aws", "ssm", "send-command",
                "--profile", AWS_PROFILE,
                "--instance-ids", EC2_INSTANCE,
                "--region", EC2_REGION,
                "--document-name", "AWS-RunShellScript",
                "--parameters", 'commands=["cd /home/ubuntu/Botsensai && grep pipeline.sweep data/daemon.log | tail -6"]',
                "--query", "Command.CommandId",
                "--output", "text",
            ],
            capture_output=True, text=True, timeout=15
        )
        cid = r.stdout.strip()
        if not cid or len(cid) != 36:
            return json.dumps({
                "ssm_available": False,
                "sweep_lines": [],
                "helius_healthy": None,
                "error": "AWS session expired or SSM unavailable",
            })

        time.sleep(18)
        o = subprocess.run(
            [
                "aws", "ssm", "get-command-invocation",
                "--profile", AWS_PROFILE,
                "--command-id", cid,
                "--instance-id", EC2_INSTANCE,
                "--region", EC2_REGION,
                "--output", "json",
            ],
            capture_output=True, text=True, timeout=15
        )
        if not o.stdout.strip():
            return json.dumps({"ssm_available": False, "sweep_lines": [], "helius_healthy": None})

        d = json.loads(o.stdout)
        out = d.get("StandardOutputContent", "")
        lines = [ln.strip() for ln in out.strip().splitlines() if "pipeline.sweep" in ln]

        helius_healthy = None
        last_sweep_age_s = None
        if lines:
            import re
            degraded_flags = [bool(re.search(r"degraded_surfaces=\[\s*\]", ln)) for ln in lines]
            helius_healthy = all(degraded_flags)

            # Parse last sweep timestamp
            last_line = lines[-1]
            m = re.search(r"(\d{2}):(\d{2}):(\d{2})", last_line)
            if m:
                now = datetime.now(timezone.utc)
                sweep_time = now.replace(
                    hour=int(m.group(1)), minute=int(m.group(2)), second=int(m.group(3)), microsecond=0
                )
                last_sweep_age_s = int((now - sweep_time).total_seconds()) % 86400

        return json.dumps({
            "ssm_available": True,
            "sweep_lines": lines,
            "helius_healthy": helius_healthy,
            "last_sweep_age_seconds": last_sweep_age_s,
            "daemon_stale": (last_sweep_age_s or 0) > DAEMON_STALE_THRESHOLD_SECONDS,
        })
    except Exception as exc:
        return json.dumps({"ssm_available": False, "sweep_lines": [], "error": str(exc)})


def check_open_positions() -> str:
    """Check whether the hot wallet holds any SPL token balances (open positions).

    Returns:
        JSON with open_positions (int) and holdings (list).
    """
    rpc_url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    payload = {
        "jsonrpc": "2.0",
        "id": "monitor-positions",
        "method": "getTokenAccountsByOwner",
        "params": [
            HOT_WALLET,
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed"},
        ],
    }
    try:
        req = urllib.request.Request(
            rpc_url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        accounts = data.get("result", {}).get("value", [])
        holdings = []
        for acc in accounts:
            info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            balance = info.get("tokenAmount", {})
            ui_amount = float(balance.get("uiAmount") or 0)
            if ui_amount > 0:
                holdings.append({
                    "mint": info.get("mint", ""),
                    "amount": ui_amount,
                    "decimals": balance.get("decimals", 0),
                })
        return json.dumps({"open_positions": len(holdings), "holdings": holdings})
    except Exception as exc:
        return json.dumps({"error": str(exc), "open_positions": 0, "holdings": []})


# ── Monitor system instructions ───────────────────────────────────────────────

MONITOR_SYSTEM = """
You are the BotsensaiMonitorAgent — a self-healing live monitoring watchdog
for the Botsensai trading bot running on EC2.

Every 5 minutes you perform a health check and emit a concise status report.

MONITORING PROTOCOL:
1. Call get_live_balance() — check SOL balance and gas floor safety.
2. Call get_sweep_log() — inspect last 6 sweep lines from EC2 daemon.
3. Call check_open_positions() — verify on-chain position count.
4. Analyze the results and emit a status report in this exact format:

   🟢/🔴 Wallet: {balance_sol} SOL | Positions: {N} | Streak: {clean_streak}
   Sweep: {last_N_sweep_lines_abbreviated}
   ⚠️ ALERTS: {any_alerts_or_NONE}

ALERT CONDITIONS (emit prominently if triggered):
- gas_floor_safe=false → "⚠️ GAS FLOOR BREACH — halt new entries"
- daemon_stale=true → "⚠️ DAEMON STALE — check systemctl status botsensai on EC2"
- ssm_available=false → "⚠️ AWS session expired — run: aws login --profile agent-profile"
- helius_healthy=false for 3+ consecutive sweeps → "⚠️ Helius 429 congestion window — {N} degraded sweeps"
- balance dropped > 0.035 SOL between checks → "🚨 UNEXPECTED BALANCE DROP — investigate immediately"

SELF-HEALING:
- Track consecutive degraded sweeps. If >= 3 consecutive, note it as a known
  congestion window (US market open 13:35-14:04 UTC or midday 15:42-16:12 UTC).
- If SSM unavailable, continue balance-only reporting rather than stopping.
- Never panic on a single degraded sweep — these are safe by design.
""".strip()


# ── Stop hook: always emit status ─────────────────────────────────────────────

if HAS_AGY_SDK and hooks is not None:
    @hooks.stop
    async def ensure_status_report(data: "types.StopArgs") -> "types.StopHookResult":
        text = data.response_text.lower() if data.response_text else ""
        if data.continuation_count == 0 and "wallet:" not in text and "balance" not in text:
            return types.StopHookResult(
                decision=types.StopDecision.CONTINUE,
                reason="Please emit the wallet status report before finishing.",
            )
        return types.StopHookResult(decision=types.StopDecision.ALLOW_STOP)


# ── Periodic trigger ──────────────────────────────────────────────────────────

async def monitor_trigger(ctx: "TriggerContext") -> None:
    """Fires every 300 seconds to kick off a monitoring check."""
    await ctx.send("Perform a full health check and emit the status report now.")


def build_monitor_config() -> "LocalAgentConfig":
    """Build the MonitorAgent LocalAgentConfig."""
    if not HAS_AGY_SDK:
        raise ImportError("google-antigravity SDK not installed")

    trigger = every(300, monitor_trigger)

    return LocalAgentConfig(
        system_instructions=MONITOR_SYSTEM,
        tools=[get_live_balance, get_sweep_log, check_open_positions],
        triggers=[trigger],
        capabilities=types.CapabilitiesConfig(
            agent_behavior=types.AgentBehavior.AUTONOMOUS,
            enable_subagents=False,  # Monitor has no subagents — leaf agent
        ),
        hooks=[ensure_status_report] if HAS_AGY_SDK else [],
    )


async def run_monitor() -> None:
    """Start the MonitorAgent event loop (blocking)."""
    if not HAS_AGY_SDK:
        log.warning("[Monitor] AGY SDK not available — running deterministic monitor loop")
        await _run_deterministic_monitor()
        return

    config = build_monitor_config()
    log.info("[Monitor] Starting AGY MonitorAgent")
    async with Agent(config) as agent:
        # First check immediately on startup
        response = await agent.chat("Perform an immediate health check and emit the full status report.")
        print(await response.text())
        # Subsequent checks fire from the periodic trigger
        while True:
            await asyncio.sleep(60)


async def _run_deterministic_monitor() -> None:
    """Fallback deterministic monitoring loop when AGY SDK unavailable."""
    prev_balance: float | None = None
    consecutive_degraded = 0
    cycle = 0

    while True:
        cycle += 1
        now = datetime.now(timezone.utc).strftime("%H:%M UTC")

        # Balance check
        bal_raw = get_live_balance()
        bal = json.loads(bal_raw)
        balance_sol = bal.get("balance_sol", 0.0)
        gas_safe = bal.get("gas_floor_safe", False)

        # Position check
        pos_raw = check_open_positions()
        pos = json.loads(pos_raw)
        n_positions = pos.get("open_positions", 0)

        # Sweep log check
        sweep_raw = get_sweep_log()
        sweep = json.loads(sweep_raw)
        ssm_ok = sweep.get("ssm_available", False)
        helius_ok = sweep.get("helius_healthy", None)
        stale = sweep.get("daemon_stale", False)
        lines = sweep.get("sweep_lines", [])

        # Parse sweep status
        if helius_ok is False:
            consecutive_degraded += 1
        else:
            consecutive_degraded = 0

        # Alerts
        alerts = []
        if not gas_safe:
            alerts.append("⚠️ GAS FLOOR BREACH — halt new entries")
        if stale:
            alerts.append("⚠️ DAEMON STALE — check systemctl status botsensai on EC2")
        if not ssm_ok:
            alerts.append("⚠️ AWS session expired — run: aws login --profile agent-profile")
        if consecutive_degraded >= 3:
            alerts.append(f"⚠️ Helius 429 congestion window — {consecutive_degraded} degraded sweeps")
        if prev_balance is not None and (prev_balance - balance_sol) > ALERT_DROP_SOL:
            alerts.append(f"🚨 UNEXPECTED BALANCE DROP: {prev_balance:.6f} → {balance_sol:.6f} SOL")

        icon = "🟢" if gas_safe and not stale else "🔴"
        alert_str = " | ".join(alerts) if alerts else "NONE"

        # Abbreviated sweep lines
        sweep_summary = []
        for ln in lines[-3:]:
            import re
            m = re.search(r"(\d{2}:\d{2}:\d{2}).*degraded_surfaces=(\[.*?\]).*entered=(\d+)", ln)
            if m:
                tag = "✅" if m.group(2) == "[]" else "⚠️"
                sweep_summary.append(f"{tag} {m.group(1)} entered={m.group(3)}")

        print(
            f"\n[{now}] {icon} Wallet: {balance_sol:.6f} SOL | "
            f"Positions: {n_positions} | Cycle: {cycle}\n"
            f"  Sweep: {' | '.join(sweep_summary) if sweep_summary else 'SSM unavailable'}\n"
            f"  Alerts: {alert_str}"
        )

        prev_balance = balance_sol
        await asyncio.sleep(300)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(run_monitor())
