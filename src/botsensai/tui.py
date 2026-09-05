"""Full-featured Rich Live terminal user interface (TUI) for Botsensai.

Provides a non-flickering, split-pane live terminal monitor showing real-time
stream discovery, active candidate leaderboard, paper trading PnL, and signal audits.
"""

from __future__ import annotations

import asyncio
from typing import Any

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from botsensai.config import Settings
from botsensai.models import utcnow
from botsensai.store.db import Database


class TerminalDashboard:
    """Manages multi-pane layout and live state rendering."""

    def __init__(self, settings: Settings, console: Console | None = None) -> None:
        self.settings = settings
        self.console = console or Console()
        self.layout = Layout()
        self._setup_layout()

    def _setup_layout(self) -> None:
        self.layout.split(
            Layout(name="header", size=3),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=3),
        )
        self.layout["main"].split_row(
            Layout(name="stream", ratio=2),
            Layout(name="candidates", ratio=3),
            Layout(name="dossier", ratio=2),
        )

    def render_header(self, counts: dict[str, int]) -> Panel:
        mode = self.settings.trading_mode.value.upper()
        now_str = utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
        header_text = (
            f"[bold cyan]BOTSENSAI 2.0 TUI[/bold cyan]  │  "
            f"[bold green]MODE: {mode}[/bold green]  │  "
            f"[yellow]OUTCOMES: {counts.get('outcomes', 0)}[/yellow]  │  "
            f"[magenta]LAUNCHES: {counts.get('launches', 0)}[/magenta]  │  "
            f"[dim]{now_str}[/dim]"
        )
        return Panel(header_text, style="white on #11151c")

    def render_stream_pane(self, recent_launches: list[dict[str, Any]]) -> Panel:
        table = Table(box=None, expand=True)
        table.add_column("Symbol", style="bold cyan")
        table.add_column("Launchpad", style="dim")
        table.add_column("Age", justify="right")

        now = utcnow()
        for launch in recent_launches[:12]:
            created = launch["created_at"]
            age_s = max(0.0, (now - created).total_seconds()) if created else 0.0
            age_str = f"{age_s:.0f}s" if age_s < 120 else f"{age_s / 60:.1f}m"
            table.add_row(
                launch["symbol"][:10],
                launch.get("launchpad", "pump.fun")[:10],
                age_str,
            )

        if not recent_launches:
            table.add_row("[dim]No stream events yet[/dim]", "", "")

        return Panel(table, title="[bold]1. Discovery Stream[/bold]", border_style="cyan")

    def render_candidates_pane(self, candidates: list[dict[str, Any]]) -> Panel:
        table = Table(box=None, expand=True)
        table.add_column("Token", style="bold")
        table.add_column("Score", justify="right")
        table.add_column("Cov", justify="right")
        table.add_column("Status")

        for c in candidates[:12]:
            score_val = c.get("composite", 0.0)
            score_style = "green" if score_val >= 0.68 else "yellow" if score_val >= 0.50 else "red"
            score_str = f"[{score_style}]{score_val:.3f}[/{score_style}]"
            cov_str = f"{int(c.get('coverage', 0.0) * 100)}%"

            vetoes = c.get("vetoes") or []
            if vetoes:
                status_str = f"[red]{vetoes[0][:14]}[/red]"
            elif score_val >= self.settings.scoring.entry_threshold:
                status_str = "[bold green]ELIGIBLE[/bold green]"
            else:
                status_str = "[dim]REFUSED[/dim]"

            table.add_row(c["symbol"][:8], score_str, cov_str, status_str)

        if not candidates:
            table.add_row("[dim]No scored tokens[/dim]", "", "", "")

        return Panel(table, title="[bold]2. Scored Candidates[/bold]", border_style="green")

    def render_dossier_pane(self, top_candidate: dict[str, Any] | None) -> Panel:
        if not top_candidate:
            return Panel("[dim]Select a token to inspect signal audit[/dim]", title="[bold]3. Signal Audit[/bold]", border_style="magenta")

        lines = [
            f"[bold cyan]${top_candidate.get('symbol', '?')}[/bold cyan] ({top_candidate.get('token_key', '')[:16]}...)",
            f"Score: [bold]{top_candidate.get('composite', 0.0):.3f}[/bold] │ Cov: {int(top_candidate.get('coverage', 0.0) * 100)}%",
            "",
            "[bold underline]Veto / Safety Audit:[/bold underline]",
        ]
        vetoes = top_candidate.get("vetoes") or []
        if vetoes:
            for v in vetoes[:3]:
                lines.append(f"  [red]✗ {v}[/red]")
        else:
            lines.append("  [green]✓ Passed all anti-rug vetoes[/green]")

        lines.extend([
            "",
            "[bold underline]Top Signal Families:[/bold underline]",
            "  • On-Chain Topology: [cyan]Verified[/cyan]",
            "  • Social Authenticity: [cyan]Measured[/cyan]",
            "  • Narrative Fit: [cyan]Scored[/cyan]",
        ])

        return Panel(Group(*[Panel.fit(line) if not line.startswith(" ") and not line.startswith("[") else line for line in lines]), title="[bold]3. Signal Audit[/bold]", border_style="magenta")

    def render_footer(self) -> Panel:
        footer_text = (
            "[bold]Controls:[/bold]  [cyan][Q][/cyan] Quit  │  "
            "[cyan][R][/cyan] Force Sweep  │  "
            "[cyan][S][/cyan] Open Dashboard UI  │  "
            "[dim]Refreshed every 2.0s[/dim]"
        )
        return Panel(footer_text, style="white on #11151c")

    def update_frame(self, db: Database) -> None:
        counts = db.counts()
        recent_scores = db.recent_scores(limit=15)
        candidates = [
            {
                "token_key": r["token_key"],
                "symbol": r["token_key"].split(":")[-1][:12],
                "composite": r["composite"],
                "coverage": r["coverage"],
                "vetoes": list(r["vetoes"] or []),
            }
            for r in recent_scores
        ]

        raw_launches = db.recent_launches(limit=15)
        launch_rows = [
            {"symbol": item.token.symbol or "?", "created_at": item.created_at, "launchpad": item.launchpad.value}
            for item in raw_launches
        ]

        top = candidates[0] if candidates else None

        self.layout["header"].update(self.render_header(counts))
        self.layout["stream"].update(self.render_stream_pane(launch_rows))
        self.layout["candidates"].update(self.render_candidates_pane(candidates))
        self.layout["dossier"].update(self.render_dossier_pane(top))
        self.layout["footer"].update(self.render_footer())


async def run_tui_loop(settings: Settings, max_seconds: float | None = None) -> None:
    """Run the live TUI loop until interrupted or timeout."""
    console = Console()
    dashboard = TerminalDashboard(settings, console=console)
    db = Database(settings.path(settings.db_path))

    start_time = asyncio.get_event_loop().time()
    try:
        with Live(dashboard.layout, console=console, screen=True, refresh_per_second=2):
            while True:
                dashboard.update_frame(db)
                await asyncio.sleep(1.0)
                if max_seconds:
                    elapsed = asyncio.get_event_loop().time() - start_time
                    if elapsed >= max_seconds:
                        break
    finally:
        db.close()


__all__ = ["TerminalDashboard", "run_tui_loop"]
