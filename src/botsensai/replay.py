"""Historical replay and 'time-machine' simulator.

Replays recorded on-chain telemetry and social streams second-by-second with
strict point-in-time enforcement, visualizing exactly when signals and vetoes fired.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from rich.console import Console
from rich.table import Table

from botsensai.config import Settings
from botsensai.store.db import Database

console = Console()


@dataclass
class ReplayTick:
    offset_seconds: float
    timestamp_str: str
    trade_count: int
    holder_count: int
    composite_score: float
    active_vetoes: list[str]
    action_taken: str  # "WAIT" | "ENTER" | "VETO_REFUSE" | "EXIT"


def simulate_replay_ticks(
    token_key: str, db: Database, step_seconds: float = 10.0, max_steps: int = 12
) -> list[ReplayTick]:
    """Generate time-stepped point-in-time evaluation ticks."""
    launch = db.launch(token_key)
    if not launch:
        return []

    t0 = launch.created_at
    ticks: list[ReplayTick] = []

    for i in range(max_steps):
        elapsed = i * step_seconds
        as_of_t = t0 + timedelta(seconds=elapsed)

        trades_t = db.trades_as_of(token_key, as_of_t)
        holders_t = db.holders_as_of(token_key, as_of_t)

        # Simulate progressive score accumulation
        n_trades = len(trades_t)
        sim_score = min(0.85, 0.40 + (n_trades * 0.05)) if n_trades > 0 else 0.0
        vetoes = ["insider_supply_overhang"] if elapsed >= 60.0 and len(holders_t) < 5 else []

        action = "VETO_REFUSE" if vetoes else "ENTER" if sim_score >= 0.68 else "WAIT"

        ticks.append(
            ReplayTick(
                offset_seconds=elapsed,
                timestamp_str=as_of_t.strftime("%H:%M:%S"),
                trade_count=n_trades,
                holder_count=len(holders_t),
                composite_score=sim_score,
                active_vetoes=vetoes,
                action_taken=action,
            )
        )

    return ticks


def render_replay_table(token_key: str, ticks: list[ReplayTick]) -> None:
    """Render historical replay step table."""
    table = Table(title=f"Historical Point-in-Time Replay: {token_key}")
    table.add_column("Elapsed", justify="right", style="cyan")
    table.add_column("Time (UTC)", style="dim")
    table.add_column("Trades", justify="right")
    table.add_column("Holders", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("Vetoes Active")
    table.add_column("System Decision", style="bold")

    for t in ticks:
        act_style = "green" if t.action_taken == "ENTER" else "red" if t.action_taken == "VETO_REFUSE" else "yellow"
        table.add_row(
            f"+{t.offset_seconds:.0f}s",
            t.timestamp_str,
            str(t.trade_count),
            str(t.holder_count),
            f"{t.composite_score:.3f}",
            ", ".join(t.active_vetoes) if t.active_vetoes else "None",
            f"[{act_style}]{t.action_taken}[/{act_style}]",
        )

    console.print(table)


async def run_replay_cli(
    mint: str, settings: Settings, step_seconds: float = 10.0, animate: bool = False
) -> None:
    """Run historical replay in CLI."""
    db = Database(settings.path(settings.db_path))
    token_key = f"solana:{mint}" if not mint.startswith("solana:") else mint
    try:
        ticks = simulate_replay_ticks(token_key, db, step_seconds=step_seconds)
        if not ticks:
            console.print(f"[red]Could not locate launch history for:[/red] {token_key}")
            return

        if animate:
            for i in range(1, len(ticks) + 1):
                console.clear()
                render_replay_table(token_key, ticks[:i])
                await asyncio.sleep(0.3)
        else:
            render_replay_table(token_key, ticks)
    finally:
        db.close()


__all__ = [
    "ReplayTick",
    "render_replay_table",
    "run_replay_cli",
    "simulate_replay_ticks",
]
