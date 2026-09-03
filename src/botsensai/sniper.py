"""Real-time token discovery, selection, and simulated paper execution sniper."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from botsensai.config import Settings, TradingMode, get_settings
from botsensai.models import Fill, Launch, MarketSnapshot, Score, TokenRef, utcnow
from botsensai.notifications import notify_candidate_detected, notify_veto_alarm
from botsensai.pipeline import Pipeline
from botsensai.store.db import Database

console = Console()


def _render_snipe_dossier(
    launch: Launch,
    score: Score,
    fill: Fill | None,
    refusal_reason: str | None = None,
) -> None:
    """Render Rich visual dossier for sniped token and execution receipt."""
    token = launch.token
    status_style = "bold green" if fill and not fill.rejected else "bold yellow"
    status_text = "SIMULATED ENTRY FILLED" if fill and not fill.rejected else "ENTRY REFUSED (RISK/VETO)"

    table = Table(title=f"Sniper Target: ${token.symbol or 'TOKEN'}", box=None)
    table.add_column("Property", style="bold cyan")
    table.add_column("Value")

    table.add_row("Token Mint", f"`{token.mint}`")
    table.add_row("Name / Symbol", f"{token.name or 'Unknown'} (${token.symbol or '?'})")
    table.add_row("Launchpad", f"{launch.launchpad.value}")
    table.add_row("Conviction Score", f"[{'green' if score.composite >= 0.68 else 'yellow'}]{score.composite:.3f}[/]")
    table.add_row("Metric Coverage", f"{score.coverage:.1%}")
    table.add_row("Market Regime", f"{score.regime}")
    vetoes_str = ", ".join(v.value for v in score.vetoes) if score.vetoes else "[green]None (All Passed)[/green]"
    table.add_row("Safety Vetoes", f"[red]{vetoes_str}[/red]" if score.vetoes else vetoes_str)

    console.print(Panel(table, title=f"[{status_style}]{status_text}[/{status_style}]", expand=False))

    if fill and not fill.rejected:
        exec_table = Table(title="Modelled Execution Receipt", box=None)
        exec_table.add_column("Field", style="bold")
        exec_table.add_column("Execution Detail")

        exec_table.add_row("Capital Spent", f"{fill.amount_native:.4f} SOL")
        exec_table.add_row("Tokens Received", f"{fill.amount_token:,.0f} {token.symbol}")
        exec_table.add_row("Effective Price", f"{fill.price_native:.10f} SOL")
        exec_table.add_row("Simulated Slippage", f"{fill.slippage_bps:.1f} bps")
        exec_table.add_row("Priority + Jito Tip", f"{(fill.fee_native + fill.tip_native):.6f} SOL")
        exec_table.add_row("Execution Latency", f"{fill.latency_ms:.1f} ms")

        console.print(Panel(exec_table, title="[bold green]Paper Broker Fill[/bold green]", expand=False))
        console.print(
            f"\n[dim]Track position with:[/dim] [cyan]botsensai positions[/cyan] | "
            f"[cyan]botsensai graph {token.mint}[/cyan] | [cyan]botsensai ask {token.mint}[/cyan]\n"
        )
    elif refusal_reason:
        console.print(f"[yellow]⚠ Position not entered:[/yellow] {refusal_reason}\n")


def _get_or_create_snapshot(db: Database, token: TokenRef) -> MarketSnapshot:
    """Retrieve the most recent market snapshot or construct a default fallback."""
    snapshots = db.snapshots_as_of(token.key, utcnow())
    if snapshots:
        return snapshots[-1]
    return MarketSnapshot(
        token=token,
        as_of=utcnow(),
        observed_at=utcnow(),
        price_native=0.00003,
        liquidity_usd=12000.0,
        market_cap_usd=30000.0,
    )


async def execute_live_snipe(
    settings: Settings | None = None,
    size_sol: float | None = None,
    min_score: float | None = None,
    force: bool = False,
    discover_limit: int = 60,
    max_candidates: int = 20,
) -> tuple[Launch | None, Score | None, Fill | None]:
    """Execute live discovery, select the single best candidate, and model paper entry."""
    active_settings = settings or get_settings()
    if active_settings.trading_mode is TradingMode.LIVE:
        console.print("[red]refusing to snipe in live mode; this build cannot trade[/red]")
        return None, None, None

    console.print("\n[bold cyan]🎯 BOTSENSAI REAL-TIME ADVERSARIAL SNIPER[/bold cyan]")
    console.print("Scanning live bonding curves, enriching on-chain telemetry, and screening candidates...\n")

    pipeline = Pipeline(active_settings)
    try:
        report = await pipeline.sweep(discover_limit=discover_limit, max_candidates=max_candidates)
        db = Database(active_settings.path(active_settings.db_path))
        scores = db.recent_scores(limit=max_candidates)

        if not report.top_candidates or not scores:
            console.print("[yellow]No active launch candidates detected during this sweep.[/yellow]")
            db.close()
            return None, None, None

        best_candidate_dict = report.top_candidates[0]
        mint = best_candidate_dict["mint"]
        launch = db.launch(f"solana:{mint}")

        if not launch:
            token_ref = TokenRef(mint=mint, symbol=best_candidate_dict.get("symbol") or "TOKEN")
            launch = Launch(token=token_ref, created_at=utcnow())

        top_score = Score(
            token=launch.token,
            composite=best_candidate_dict["score"],
            coverage=best_candidate_dict["coverage"],
            as_of=utcnow(),
            observed_at=utcnow(),
            vetoes=[],
            explanation=best_candidate_dict.get("why"),
        )

        threshold = min_score or active_settings.scoring.entry_threshold
        position_size = size_sol or active_settings.risk.max_position_native
        fill: Fill | None = None
        refusal_reason: str | None = None

        if (top_score.composite >= threshold and not top_score.vetoed) or force:
            snapshot = _get_or_create_snapshot(db, launch.token)
            age = (top_score.as_of - launch.created_at).total_seconds()
            fill = pipeline.broker.open_position(
                launch.token,
                position_size,
                snapshot,
                top_score.as_of,
                age_seconds=age,
                reason=f"sniper selection (score {top_score.composite:.3f})",
                score=top_score.composite,
            )
            notify_candidate_detected(launch.token.symbol or "TOKEN", top_score.composite, launch.token.mint)
        else:
            if top_score.vetoed:
                refusal_reason = f"Hard anti-rug veto triggered: {', '.join(v.value for v in top_score.vetoes)}"
                notify_veto_alarm(launch.token.symbol or "TOKEN", refusal_reason)
            else:
                refusal_reason = f"Conviction {top_score.composite:.3f} below threshold {threshold:.3f}"

        db.close()
        _render_snipe_dossier(launch, top_score, fill, refusal_reason)
        return launch, top_score, fill

    finally:
        await pipeline.aclose()


__all__ = ["execute_live_snipe"]
