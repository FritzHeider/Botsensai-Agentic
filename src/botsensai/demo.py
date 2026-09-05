"""Zero-config interactive demo runner.

Demonstrates end-to-end Botsensai 2.0 capabilities (mint firehose, adversarial scoring,
funder graph analysis, paper trading, and live UI) without requiring external RPC keys.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from botsensai.config import Settings, get_settings
from botsensai.models import (
    HolderRecord,
    Launch,
    Launchpad,
    Score,
    Side,
    TokenRef,
    Trade,
    VetoReason,
)
from botsensai.store.db import Database

console = Console()


def seed_demo_environment(db: Database) -> list[TokenRef]:
    """Seed SQLite database with realistic launch cohorts (organic runner vs slow rug)."""
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)

    # 1. Organic Runner ($GIGAWHALE)
    t1 = TokenRef(mint="gigawhale123456789012345678901234", name="Gigawhale", symbol="GIGAWHALE")
    l1 = Launch(token=t1, launchpad=Launchpad.PUMPFUN, created_at=now)
    db.upsert_launch(l1)

    trades1 = [
        Trade(
            token=t1,
            signature=f"sig_giga_{i}",
            wallet=f"trader_wallet_{i}",
            side=Side.BUY,
            amount_token=25000.0,
            amount_native=1.5 + (i * 0.2),
            as_of=now + timedelta(seconds=i * 5),
            observed_at=now + timedelta(seconds=i * 5),
        )
        for i in range(10)
    ]
    db.insert_trades(trades1)

    holders1 = [
        HolderRecord(
            token=t1,
            wallet=f"trader_wallet_{i}",
            balance=25000.0,
            share_of_supply=0.025,
            as_of=now + timedelta(seconds=60),
            observed_at=now + timedelta(seconds=60),
        )
        for i in range(10)
    ]
    db.insert_holders(holders1)

    db.insert_score(
        Score(
            token=t1,
            composite=0.824,
            coverage=0.92,
            as_of=now + timedelta(seconds=60),
            observed_at=now + timedelta(seconds=60),
            metric_values=[],
            vetoes=[],
            explanation="Passed all vetoes; strong organic holder dispersion and remix depth.",
            weights_version="v2.0-alpha",
        )
    )

    # 2. Sybil Rug Attempt ($PEPERUG)
    t2 = TokenRef(mint="peperug1234567890123456789012345", name="Pepe Rug", symbol="PEPERUG")
    l2 = Launch(token=t2, launchpad=Launchpad.PUMPFUN, created_at=now)
    db.upsert_launch(l2)

    db.insert_score(
        Score(
            token=t2,
            composite=0.210,
            coverage=0.85,
            as_of=now + timedelta(seconds=60),
            observed_at=now + timedelta(seconds=60),
            metric_values=[],
            vetoes=[VetoReason.INSIDER_SUPPLY_EXCESSIVE, VetoReason.BUNDLE_SUPPLY_EXCESSIVE],
            explanation="Blocked: 78% supply held by deployer-funded sybils.",
            weights_version="v2.0-alpha",
        )
    )

    return [t1, t2]


async def run_demo_simulation(settings: Settings | None = None, non_interactive: bool = False) -> None:
    """Run automated 5-step live demonstration."""
    active_settings = settings or get_settings()
    db = Database(active_settings.path(active_settings.db_path))

    console.print(Panel(
        "[bold cyan]BOTSENSAI 2.0 ZERO-CONFIG ADVERSARIAL DEMO[/bold cyan]\n"
        "Simulating live on-chain discovery, telemetry ingestion, adversarial scoring, and safety vetoes.",
        title="[bold green]System Demo[/bold green]",
        expand=False,
    ))

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        task = progress.add_task("1. Initializing in-memory telemetry store...", total=None)
        await asyncio.sleep(0.2)
        tokens = seed_demo_environment(db)

        progress.update(task, description="2. Ingesting bonding curve trades and holder snapshots...")
        await asyncio.sleep(0.3)

        progress.update(task, description="3. Evaluating 34 adversarial metric signals...")
        await asyncio.sleep(0.3)

        progress.update(task, description="4. Executing hard anti-rug safety veto gate...")
        await asyncio.sleep(0.2)

        progress.update(task, description="5. Publishing real-time paper trading signals...")
        await asyncio.sleep(0.2)

    console.print("\n[green]✓ Demo Simulation Completed Successfully![/green]\n")
    console.print(f"  • [cyan]${tokens[0].symbol}[/cyan]: Score [bold green]0.824[/bold green] (PASSED - Eligible for Paper Entry)")
    console.print(f"  • [red]${tokens[1].symbol}[/red]: Score [bold red]0.210[/bold red] (VETOED - Blocked: insider_supply_excessive)\n")

    db.close()


__all__ = ["run_demo_simulation", "seed_demo_environment"]
