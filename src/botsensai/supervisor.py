"""Unified async supervisor for Botsensai.

Orchestrates the WebSocket mint streamer, periodic REST sweep loop, background
outcome labeller, and integrity sentinel within a single process.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from rich.console import Console
from rich.table import Table

from botsensai.collectors.pumpfun_ws import PUMPPORTAL_WS, PumpPortalStream
from botsensai.config import Settings
from botsensai.dashboard.integrity import check_integrity
from botsensai.labeller import LabelPolicy, OutcomeLabeller
from botsensai.notifications import NotificationDispatcher
from botsensai.pipeline import Pipeline
from botsensai.store.db import Database
from botsensai.util.logging import get_logger

log = get_logger("supervisor")
console = Console()


@dataclass
class SupervisorStats:
    start_time: datetime = field(default_factory=datetime.now)
    sweeps: int = 0
    mints_streamed: int = 0
    outcomes_labelled: int = 0
    entered_positions: int = 0
    integrity_alarms: int = 0
    errors: list[str] = field(default_factory=list)
    stopped_because: str = "completed"


class Supervisor:
    """Coordinates and manages all asynchronous background tasks in Botsensai."""

    def __init__(
        self,
        settings: Settings,
        enable_stream: bool = True,
        enable_sweep: bool = True,
        enable_labeller: bool = True,
        sweep_interval: float = 60.0,
        label_interval: float = 300.0,
        integrity_interval: float = 180.0,
        discovery_limit: int = 60,
        candidates_limit: int = 20,
    ) -> None:
        self.settings = settings
        self.enable_stream = enable_stream
        self.enable_sweep = enable_sweep
        self.enable_labeller = enable_labeller
        self.sweep_interval = sweep_interval
        self.label_interval = label_interval
        self.integrity_interval = integrity_interval
        self.discovery_limit = discovery_limit
        self.candidates_limit = candidates_limit

        self.db = Database(settings.path(settings.db_path))
        self.pipeline = Pipeline(settings, db=self.db)
        self.dispatcher = NotificationDispatcher(settings)
        self.stats = SupervisorStats()
        self._running = False
        self._tasks: list[asyncio.Task[Any]] = []

    async def run(self, max_seconds: float | None = None) -> SupervisorStats:
        """Run the supervisor loop until max_seconds expires or cancelled."""
        self._running = True
        self.stats.start_time = datetime.now()

        console.print("[bold green]Starting Botsensai Unified Supervisor[/bold green]")
        components = []
        if self.enable_stream:
            components.append("WebSocket Streamer")
        if self.enable_sweep:
            components.append(f"REST Sweeper ({self.sweep_interval:.0f}s)")
        if self.enable_labeller:
            components.append(f"Auto-Labeller ({self.label_interval:.0f}s)")
        components.append("Integrity Sentinel")
        console.print(f"Components active: [cyan]{', '.join(components)}[/cyan]\n")

        try:
            async with asyncio.TaskGroup() as tg:
                if self.enable_stream:
                    self._tasks.append(tg.create_task(self._stream_worker()))
                if self.enable_sweep:
                    self._tasks.append(tg.create_task(self._sweep_worker()))
                if self.enable_labeller:
                    self._tasks.append(tg.create_task(self._labeller_worker()))
                self._tasks.append(tg.create_task(self._integrity_worker()))

                if max_seconds:
                    self._tasks.append(tg.create_task(self._timeout_sentinel(max_seconds)))
        except (KeyboardInterrupt, asyncio.CancelledError):
            self.stats.stopped_because = "interrupted"
        except Exception as exc:
            self.stats.stopped_because = f"error: {exc}"
            self.stats.errors.append(str(exc))
        finally:
            await self.aclose()

        return self.stats

    async def _timeout_sentinel(self, timeout_seconds: float) -> None:
        await asyncio.sleep(timeout_seconds)
        self._running = False
        self.stats.stopped_because = "duration_reached"
        for task in self._tasks:
            if not task.done():
                task.cancel()

    async def _stream_worker(self) -> None:
        ws = PumpPortalStream(self.settings, db=self.db, url=PUMPPORTAL_WS)
        try:
            while self._running:
                try:
                    await ws.run(max_seconds=3600.0)
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    log.warning("ws_stream_reconnecting", error=str(exc))
                    await asyncio.sleep(5.0)
        finally:
            self.stats.mints_streamed += ws.stats.novel_mints

    async def _sweep_worker(self) -> None:
        while self._running:
            try:
                report = await self.pipeline.sweep(
                    discover_limit=self.discovery_limit,
                    max_candidates=self.candidates_limit,
                )
                self.stats.sweeps += 1
                self.stats.entered_positions += report.entered

                summary = report.summary()
                ts = datetime.now().strftime("%H:%M:%S")
                console.print(
                    f"  [dim]{ts}[/dim]  sweep {self.stats.sweeps:3d}  "
                    f"{summary['discovered']:3d} discovered → {summary['screened_in']:2d} screened → "
                    f"{summary['scored']:2d} scored → [bold green]{summary['entered']:1d} entered[/bold green]  "
                    f"[dim]({summary['duration_seconds']}s)[/dim]"
                )

                # Check top candidates for webhook alerts
                if report.top_candidates:
                    for cand in report.top_candidates:
                        cand_score = float(cand.get("score", 0.0))
                        if cand_score >= self.settings.scoring.entry_threshold:
                            token_key = str(cand.get("token_key", ""))
                            mint_val = token_key.split(":")[-1] if ":" in token_key else token_key
                            await self.dispatcher.on_candidate_scored(
                                symbol=str(cand.get("symbol", "?")),
                                mint=mint_val,
                                score=cand_score,
                                coverage=float(cand.get("coverage", 0.0)),
                                explanation=cand.get("why"),
                            )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("sweep_worker_error", error=str(exc))
                self.stats.errors.append(f"sweep: {exc}")

            try:
                await asyncio.sleep(self.sweep_interval)
            except asyncio.CancelledError:
                break

    async def _labeller_worker(self) -> None:
        policy = LabelPolicy.from_settings(self.settings, min_age_hours=24.0)
        labeller = OutcomeLabeller(self.settings, db=self.db, policy=policy)
        try:
            while self._running:
                try:
                    await asyncio.sleep(self.label_interval)
                    stats = await labeller.run(limit=50, use_ohlcv=False)
                    if stats.labelled > 0:
                        self.stats.outcomes_labelled += stats.labelled
                        ts = datetime.now().strftime("%H:%M:%S")
                        console.print(f"  [dim]{ts}[/dim]  [cyan]auto-labeller:[/cyan] labelled +{stats.labelled} outcomes")
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    log.warning("labeller_worker_error", error=str(exc))
        finally:
            await labeller.aclose()

    async def _integrity_worker(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.integrity_interval)
                posts = self.db.social_post_integrity()
                runs = self.db.recent_runs()
                spread = self.db.metric_raw_spread()
                flags = check_integrity(posts, runs, spread)

                alarms = [f for f in flags if f.level == "alarm"]
                if alarms:
                    self.stats.integrity_alarms += len(alarms)
                    for alarm in alarms:
                        console.print(f"  [red]⚠ INTEGRITY ALARM: {alarm.headline}[/red] — {alarm.detail}")
                        await self.dispatcher.on_integrity_alarm(alarm.headline, alarm.detail)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("integrity_worker_error", error=str(exc))

    async def aclose(self) -> None:
        """Cleanly close database and connections."""
        self._running = False
        await self.pipeline.aclose()
        self.db.close()

    def print_summary(self) -> None:
        """Display clean Rich summary table of the supervisor run."""
        duration = (datetime.now() - self.stats.start_time).total_seconds()
        table = Table(title="Botsensai Supervisor Session Summary")
        table.add_column("Metric", style="bold")
        table.add_column("Value", justify="right")

        table.add_row("Status", f"[green]{self.stats.stopped_because}[/green]")
        table.add_row("Duration", f"{duration:.0f}s ({duration/60:.1f}m)")
        table.add_row("Sweeps Completed", str(self.stats.sweeps))
        table.add_row("Streamed Mints", str(self.stats.mints_streamed))
        table.add_row("Outcomes Labelled", str(self.stats.outcomes_labelled))
        table.add_row("Paper Positions Entered", str(self.stats.entered_positions))
        table.add_row("Integrity Alarms", str(self.stats.integrity_alarms))
        console.print(table)
