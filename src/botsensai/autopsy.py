"""Token comparison and post-mortem autopsy studio.

Provides side-by-side metric comparison and reconstructs detailed forensic
event timelines for graduated or rugged tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from botsensai.config import Settings
from botsensai.inspector import inspect_token
from botsensai.models import Side, utcnow
from botsensai.store.db import Database

console = Console()


@dataclass
class AutopsyEvent:
    timestamp: datetime
    elapsed_seconds: float
    category: str  # "launch" | "bundle" | "social" | "peak" | "dump" | "veto"
    headline: str
    detail: str
    impact: str  # "neutral" | "bullish" | "bearish" | "critical"


def build_autopsy_timeline(token_key: str, db: Database) -> list[AutopsyEvent]:
    """Reconstruct chronological event tape for a token."""
    events: list[AutopsyEvent] = []
    launch = db.launch(token_key)
    if not launch:
        return events

    t0 = launch.created_at
    events.append(
        AutopsyEvent(
            timestamp=t0,
            elapsed_seconds=0.0,
            category="launch",
            headline=f"Token Minted: ${launch.token.symbol or '?'}",
            detail=f"Minted on {launch.launchpad.value} by deployer {launch.token.mint[:8]}...",
            impact="neutral",
        )
    )

    now = utcnow()
    trades = db.trades_as_of(token_key, now)
    snapshots = db.snapshots_as_of(token_key, now)
    scores = db.recent_scores(limit=100)
    token_scores = [s for s in scores if s["token_key"] == token_key]

    # Check for early sniper bundles
    early_buys = [t for t in trades if t.side == Side.BUY and (t.as_of - t0).total_seconds() <= 15.0]
    if len(early_buys) >= 3:
        unique_wallets = {t.wallet for t in early_buys}
        events.append(
            AutopsyEvent(
                timestamp=early_buys[0].as_of,
                elapsed_seconds=(early_buys[0].as_of - t0).total_seconds(),
                category="bundle",
                headline=f"Sniper Bundle Detected ({len(early_buys)} buys in first 15s)",
                detail=f"Total {sum(t.amount_native for t in early_buys):.2f} SOL accumulated across {len(unique_wallets)} wallets.",
                impact="bearish",
            )
        )

    # Check for peak liquidity / market snapshot
    if snapshots:
        peak_snap = max(snapshots, key=lambda s: s.market_cap_usd or 0.0)
        events.append(
            AutopsyEvent(
                timestamp=peak_snap.as_of,
                elapsed_seconds=max(0.0, (peak_snap.as_of - t0).total_seconds()),
                category="peak",
                headline=f"Peak Market Cap: ${(peak_snap.market_cap_usd or 0):,.0f}",
                detail=f"Liquidity ${(peak_snap.liquidity_usd or 0):,.0f} | Price ${(peak_snap.price_usd or 0):.6f}",
                impact="bullish",
            )
        )

    # Check for deployer sell or massive dump
    dev_sells = [t for t in trades if t.side == Side.SELL and (t.amount_native >= 5.0 or (t.wallet and t.wallet == launch.token.mint))]
    if dev_sells:
        first_dump = dev_sells[0]
        events.append(
            AutopsyEvent(
                timestamp=first_dump.as_of,
                elapsed_seconds=max(0.0, (first_dump.as_of - t0).total_seconds()),
                category="dump",
                headline=f"Major Dump Executed: {first_dump.amount_native:.2f} SOL",
                detail=f"Wallet {first_dump.wallet[:6]}... dumped {first_dump.amount_token:,.0f} tokens.",
                impact="critical",
            )
        )

    # Veto check
    if token_scores and token_scores[0]["vetoes"]:
        v_list = list(token_scores[0]["vetoes"])
        events.append(
            AutopsyEvent(
                timestamp=token_scores[0]["as_of"],
                elapsed_seconds=max(0.0, (token_scores[0]["as_of"] - t0).total_seconds()),
                category="veto",
                headline=f"Botsensai Hard Veto Triggered: {', '.join(v_list[:2])}",
                detail=f"Scoring engine blocked paper entry (Composite: {token_scores[0]['composite']:.3f}).",
                impact="critical",
            )
        )

    events.sort(key=lambda e: e.timestamp)
    return events


async def compare_tokens(query1: str, query2: str, settings: Settings) -> None:
    """Side-by-side comparison of two tokens across all signals."""
    console.print(f"\n[bold cyan]Comparing Tokens:[/bold cyan] {query1} vs {query2}\n")

    l1, s1 = await inspect_token(query1, settings, deep_enrich=True)
    l2, s2 = await inspect_token(query2, settings, deep_enrich=True)

    if not l1 or not s1 or not l2 or not s2:
        console.print("[red]Unable to resolve and score both tokens for comparison.[/red]")
        return

    table = Table(title=f"Comparative Audit: ${l1.token.symbol} vs ${l2.token.symbol}")
    table.add_column("Dimension", style="bold")
    table.add_column(f"${l1.token.symbol or 'T1'}", justify="right")
    table.add_column(f"${l2.token.symbol or 'T2'}", justify="right")
    table.add_column("Divergence / Edge")

    table.add_row("Composite Score", f"{s1.composite:.3f}", f"{s2.composite:.3f}", f"{s1.composite - s2.composite:+.3f}")
    table.add_row("Signal Coverage", f"{s1.coverage:.0%}", f"{s2.coverage:.0%}", f"{s1.coverage - s2.coverage:+.0%}")
    table.add_row("Hard Vetoes", str(len(s1.vetoes)), str(len(s2.vetoes)), "Pass vs Fail" if bool(s1.vetoes) != bool(s2.vetoes) else "Tied")

    mv1_map = {mv.metric_id: mv for mv in s1.metric_values}
    mv2_map = {mv.metric_id: mv for mv in s2.metric_values}
    all_metrics = sorted(set(mv1_map.keys()) | set(mv2_map.keys()))

    for mid in all_metrics:
        v1 = mv1_map.get(mid)
        v2 = mv2_map.get(mid)
        norm1 = f"{v1.normalized:.2f}" if v1 and v1.normalized is not None else "—"
        norm2 = f"{v2.normalized:.2f}" if v2 and v2.normalized is not None else "—"
        diff_str = "—"
        if v1 and v2 and v1.normalized is not None and v2.normalized is not None:
            diff = v1.normalized - v2.normalized
            diff_str = f"[green]{diff:+.2f}[/green]" if diff > 0.05 else f"[red]{diff:+.2f}[/red]" if diff < -0.05 else f"{diff:+.2f}"
        table.add_row(mid, norm1, norm2, diff_str)

    console.print(table)


async def autopsy_token(query: str, settings: Settings) -> None:
    """Generate chronological forensic autopsy for a token."""
    db = Database(settings.path(settings.db_path))
    try:
        launch, score = await inspect_token(query, settings, deep_enrich=True)
        if not launch:
            console.print(f"[red]Could not locate token:[/red] {query}")
            return

        token_key = f"solana:{launch.token.mint}"
        timeline = build_autopsy_timeline(token_key, db)

        console.print(Panel(
            f"[bold]Token Forensic Autopsy:[/bold] ${launch.token.symbol or '?'}\n"
            f"Mint: [cyan]{launch.token.mint}[/cyan] │ Launchpad: {launch.launchpad.value}",
            title="[red bold]Autopsy Studio[/red bold]",
            expand=False,
        ))

        table = Table(title="Chronological Forensic Event Tape")
        table.add_column("Time (UTC)", style="dim")
        table.add_column("Elapsed", justify="right")
        table.add_column("Category")
        table.add_column("Event Headline", style="bold")
        table.add_column("Technical Details")

        cat_colors = {
            "launch": "cyan",
            "bundle": "yellow",
            "social": "blue",
            "peak": "green",
            "dump": "red bold",
            "veto": "red",
        }

        for ev in timeline:
            cat_style = cat_colors.get(ev.category, "white")
            table.add_row(
                ev.timestamp.strftime("%H:%M:%S"),
                f"+{ev.elapsed_seconds:.0f}s",
                f"[{cat_style}]{ev.category.upper()}[/{cat_style}]",
                ev.headline,
                ev.detail,
            )

        console.print(table)
    finally:
        db.close()


__all__ = ["AutopsyEvent", "autopsy_token", "build_autopsy_timeline", "compare_tokens"]
