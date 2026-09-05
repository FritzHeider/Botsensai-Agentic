"""Universal token inspector and dossier generator.

Resolves URLs, tickers, or raw mints and displays a comprehensive visual dossier
with score breakdowns, veto checks, and gameability audits.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from botsensai.config import Settings
from botsensai.models import Launch, Launchpad, Score, TokenRef, utcnow
from botsensai.pipeline import Pipeline
from botsensai.store.db import Database

console = Console()

# Solana base58 address pattern (32 to 44 characters)
SOLANA_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# URL patterns
URL_PATTERNS = [
    re.compile(r"pump\.fun/(?:coin/)?([1-9A-HJ-NP-Za-km-z]{32,44})"),
    re.compile(r"dexscreener\.com/solana/([1-9A-HJ-NP-Za-km-z]{32,44})"),
    re.compile(r"solscan\.io/token/([1-9A-HJ-NP-Za-km-z]{32,44})"),
    re.compile(r"birdeye\.so/token/([1-9A-HJ-NP-Za-km-z]{32,44})"),
]


def extract_mint_from_query(query: str, db: Database | None = None) -> tuple[str | None, str | None]:
    """Parse a query into (mint_address, resolution_note)."""
    q = query.strip()

    # 1. URL parsing
    for pattern in URL_PATTERNS:
        match = pattern.search(q)
        if match:
            return match.group(1), f"extracted from URL: {q}"

    # 2. Raw base58 mint
    if SOLANA_MINT_RE.match(q):
        return q, "direct Solana mint address"

    # 3. Ticker search ($PEPE or PEPE)
    clean_symbol = q.lstrip("$").upper()
    if db:
        now = utcnow()
        start = now - timedelta(days=30)
        launches = db.launches_between(start, now)
        matches = [
            launch for launch in launches
            if launch.token.symbol and launch.token.symbol.upper() == clean_symbol
        ]
        if matches:
            # Pick the latest matching launch
            latest = sorted(matches, key=lambda launch_item: launch_item.created_at, reverse=True)[0]
            return latest.token.mint, f"resolved from ticker ${clean_symbol} in local store"

    return None, f"unable to resolve query: '{query}'"


async def inspect_token(
    query: str,
    settings: Settings,
    deep_enrich: bool = True,
) -> tuple[Launch | None, Score | None]:
    """Inspect and score any token from a URL, ticker, or mint address."""
    db = Database(settings.path(settings.db_path))
    pipeline = Pipeline(settings, db=db)

    try:
        mint, note = extract_mint_from_query(query, db)
        if not mint:
            console.print(f"[red]Could not resolve token query:[/red] '{query}'")
            console.print(
                "[dim]Accepted inputs: Solana mint address, pump.fun URL, "
                "Dexscreener URL, or recent ticker symbol ($PEPE).[/dim]"
            )
            return None, None

        console.print(f"Resolving: [bold cyan]{mint}[/bold cyan] [dim]({note})[/dim]...")

        # 1. Check local database
        launch = db.launch(f"solana:{mint}")
        if launch is None:
            # 2. Search recent discovery feeds
            discovered = await pipeline.discover(limit=100)
            launch = next((launch_item for launch_item in discovered.launches if launch_item.token.mint == mint), None)

        if launch is None:
            # 3. Construct bare launch and enrich via REST collectors
            launch = Launch(
                token=TokenRef(
                    mint=mint,
                    name="Unknown",
                    symbol=query.lstrip("$").upper() if not SOLANA_MINT_RE.match(query) else "?",
                ),
                launchpad=Launchpad.UNKNOWN,
                created_at=utcnow(),
            )

        if deep_enrich:
            with console.status("[bold green]Enriching token data (holders, trades, security)…"):
                await pipeline.enrich([launch])

        pipeline.compute_regime()
        result = pipeline.score(launch)

        _render_dossier(launch, result, pipeline.metrics)
        return launch, result

    finally:
        await pipeline.aclose()
        db.close()


def _render_dossier(launch: Launch, result: Score, registry: Any) -> None:
    token = launch.token
    score_colour = "green" if result.composite >= 0.68 else "yellow" if result.composite >= 0.50 else "red"

    # Header Panel
    header_text = (
        f"[bold]{token.name or 'Unknown'}[/bold] (${token.symbol or '?'})\n"
        f"Mint: [cyan]{token.mint}[/cyan]\n"
        f"Launchpad: {launch.launchpad.value} | Created: {launch.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    console.print(Panel(header_text, title=f"[{score_colour} bold]Dossier · Score: {result.composite:.3f}[/]", expand=False))

    # Veto Summary
    if result.vetoes:
        console.print(Panel(
            "\n".join(f"  [red]✗ VETO:[/red] {v}" for v in result.vetoes),
            title="[red bold]Safety & Veto Flags[/red bold]",
            expand=False,
        ))
    else:
        console.print("[green]✓ Passed all hard safety & anti-rug veto checks.[/green]\n")

    # Score Explanation
    if result.explanation:
        console.print(Panel(result.explanation, title="Scoring Rationale", expand=False))

    # Metric Breakdown Table
    table = Table(title="Signal Breakdown (34 Signals)")
    table.add_column("Family", style="dim")
    table.add_column("Metric ID", style="cyan")
    table.add_column("Raw", justify="right")
    table.add_column("Norm", justify="right")
    table.add_column("Conf", justify="center")
    table.add_column("Direction")

    by_family: dict[str, list[Any]] = {}
    for mv in result.metric_values:
        metric_def = registry.get(mv.metric_id)
        fam = metric_def.family if metric_def else "other"
        by_family.setdefault(fam, []).append((mv, metric_def))

    for fam, items in sorted(by_family.items()):
        for mv, mdef in sorted(items, key=lambda x: x[0].metric_id):
            norm_val = f"{mv.normalized:.3f}" if mv.normalized is not None else "—"
            raw_val = f"{mv.raw:.4f}" if mv.raw is not None else "—"
            direction = mdef.direction.value if mdef else "—"
            table.add_row(
                fam,
                mv.metric_id,
                raw_val,
                norm_val,
                mv.confidence.value,
                direction,
            )

    console.print(table)
