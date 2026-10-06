#!/usr/bin/env python3
"""
Botsensai Multi-Agent System Launcher.

Starts both the OrchestratorAgent (trading) and MonitorAgent (watchdog)
as concurrent asyncio tasks.

Usage:
    # Trading orchestrator only
    python scripts/run_agents.py --mode orchestrator

    # Monitoring watchdog only (replaces 5-min cron)
    python scripts/run_agents.py --mode monitor

    # Both together (default)
    python scripts/run_agents.py --mode both

Requirements:
    pip install "botsensai[agents]"
    # or: pip install google-antigravity

Environment:
    HELIUS_API_KEY or BOTSENSAI_HELIUS_API_KEY  (falls back to embedded key)
    GEMINI_API_KEY                               (required for AGY SDK Gemini models)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-20s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("launcher")


async def run_both() -> None:
    """Run Orchestrator and Monitor concurrently."""
    from botsensai.agents.orchestrator import run_orchestrator
    from botsensai.agents.monitor import run_monitor

    log.info("[Launcher] Starting OrchestratorAgent + MonitorAgent")
    await asyncio.gather(
        run_orchestrator(),
        run_monitor(),
    )


async def run_orchestrator_only() -> None:
    from botsensai.agents.orchestrator import run_orchestrator
    log.info("[Launcher] Starting OrchestratorAgent only")
    await run_orchestrator()


async def run_monitor_only() -> None:
    from botsensai.agents.monitor import run_monitor
    log.info("[Launcher] Starting MonitorAgent only")
    await run_monitor()


def main() -> None:
    parser = argparse.ArgumentParser(description="Botsensai Multi-Agent Launcher")
    parser.add_argument(
        "--mode",
        choices=["orchestrator", "monitor", "both"],
        default="both",
        help="Which agents to run (default: both)",
    )
    args = parser.parse_args()

    try:
        if args.mode == "orchestrator":
            asyncio.run(run_orchestrator_only())
        elif args.mode == "monitor":
            asyncio.run(run_monitor_only())
        else:
            asyncio.run(run_both())
    except KeyboardInterrupt:
        log.info("[Launcher] Shutdown requested — stopping agents")
        sys.exit(0)


if __name__ == "__main__":
    main()
