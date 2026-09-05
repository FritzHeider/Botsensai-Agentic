"""Botsensai command line.

Every subcommand is designed to be runnable without arguments and to do
something useful and safe by default. `doctor` probes every surface and reports
what is reachable; `sweep` runs one pass of the live loop in paper mode;
`backtest` refuses to run against real data it does not have and falls back to a
clearly-labelled synthetic run.

Nothing in this CLI can place a real order.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import webbrowser
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from botsensai.backtest.baselines import BaselineComparison, evaluate_baselines, run_baselines
from botsensai.backtest.engine import Backtester
from botsensai.backtest.walkforward import WalkForward, _has_label
from botsensai.config import (
    Settings,
    TradingMode,
    detect_unknown_yaml_keys,
    get_settings,
    load_settings,
)
from botsensai.export import export_data
from botsensai.inspector import inspect_token
from botsensai.media import ContentGenerator
from botsensai.memory.store import MemoryStore
from botsensai.metrics import build_registry, metric_catalogue
from botsensai.models import MemoryKind, utcnow
from botsensai.onboarding import run_onboarding_wizard, show_config, validate_config
from botsensai.pipeline import Pipeline
from botsensai.scoring import CompositeScorer, Weights
from botsensai.scoring.fit import (
    MIN_SAMPLES_TO_FIT,
    AblationReport,
    FitReport,
    TrainingExample,
    WeightFitter,
    ablation,
    fit_regime_weights,
)
from botsensai.store.db import Database
from botsensai.supervisor import Supervisor
from botsensai.util.logging import configure_logging
from botsensai.util.synthetic import generate_cohort, synthetic_outcome
from botsensai.watchlist import (
    DEFAULT_WATCHLIST_LIMIT,
    collected_channels,
    discover_candidates,
    drop_reason,
    is_useless,
    normalize_channel,
    rank_channels,
    read_channels,
    seed_call_channels,
)

app = typer.Typer(
    name="botsensai",
    help="Agentic memecoin recon, scoring, backtesting, paper trading and content generation.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _settings(config: str | None = None, log_level: str = "INFO") -> Any:
    try:
        settings = load_settings(config) if config else get_settings()
    except Exception as exc:
        console.print(
            Panel(
                f"[red]Configuration Error:[/red] {exc}\n\n"
                "Run [bold]botsensai config check[/bold] or [bold]botsensai init[/bold] to diagnose.",
                title="Config Error",
            )
        )
        raise typer.Exit(1) from None

    unknown_keys = detect_unknown_yaml_keys(config)
    if unknown_keys:
        console.print(
            f"[yellow]⚠ Warning: Unknown config keys ignored: {', '.join(unknown_keys)}[/yellow]"
        )

    configure_logging(log_level, settings.log_json)
    return settings


def _banner(settings: Any) -> None:
    from botsensai.util.environment import get_virtualenv_info

    mode = settings.trading_mode.value
    colour = {"backtest": "cyan", "paper": "green", "live": "red bold"}.get(mode, "white")
    env_info = get_virtualenv_info()
    env_label = f"[dim]env: {env_info['env_type']}[/dim]"
    console.print(
        Panel(
            f"[{colour}]mode: {mode}[/{colour}]   metrics: {len(build_registry())}   "
            f"db: {settings.db_path}   {env_label}",
            title="botsensai",
            expand=False,
        )
    )


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


@app.command()
def doctor(
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Probe every data surface and report what is actually reachable.

    Run this first. The system is designed to degrade rather than fail, which
    means a surface can be silently dead for hours without anything breaking —
    this is how you find out.
    """
    settings = _settings(config, log_level)
    _banner(settings)

    from botsensai.collectors import ALL_COLLECTORS

    async def probe() -> list[tuple[str, bool, str]]:
        rows: list[tuple[str, bool, str]] = []
        for cls in ALL_COLLECTORS:
            collector = cls(settings)
            try:
                ok = await asyncio.wait_for(collector.health_check(), timeout=25.0)
                rows.append((collector.name, ok, "reachable" if ok else "unreachable"))
            except Exception as exc:
                rows.append((collector.name, False, f"{type(exc).__name__}: {exc}"[:60]))
            finally:
                await collector.aclose()
        return rows

    with console.status("[bold green]Probing data surfaces for reachability..."):
        results = asyncio.run(probe())

    table = Table(title="data surfaces")
    table.add_column("surface")
    table.add_column("status")
    table.add_column("detail")
    for name, ok, detail in results:
        table.add_row(name, "[green]ok[/green]" if ok else "[red]down[/red]", detail)
    console.print(table)

    reachable = sum(1 for _, ok, _ in results if ok)
    console.print(
        f"\n{reachable}/{len(results)} surfaces reachable. "
        f"Metrics depending on unreachable surfaces will report reduced confidence "
        f"rather than failing."
    )

    try:
        db = Database(settings.path(settings.db_path))
        counts = db.counts()
        console.print(f"\nstore: {counts}")
    except Exception as exc:
        console.print(f"[red]store unavailable: {exc}[/red]")


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #


@app.command()
def metrics(
    detail: bool = typer.Option(False, "--detail", help="Show thesis and gameability."),
    as_json: bool = typer.Option(False, "--json", help="Emit the catalogue as JSON."),
) -> None:
    """List the metric suite, grouped by family."""
    catalogue = metric_catalogue()
    if as_json:
        console.print_json(json.dumps(catalogue))
        return

    by_family: dict[str, list[dict[str, Any]]] = {}
    for entry in catalogue:
        by_family.setdefault(entry["family"], []).append(entry)

    for family, entries in by_family.items():
        table = Table(title=f"{family} ({len(entries)})")
        table.add_column("id", style="cyan")
        table.add_column("name")
        table.add_column("earliest", justify="right")
        table.add_column("direction")
        if detail:
            table.add_column("thesis")
        for entry in entries:
            row = [
                entry["id"],
                entry["name"],
                f"t+{entry['earliest_seconds']:.0f}s",
                entry["direction"].replace("_", " "),
            ]
            if detail:
                row.append(entry["thesis"])
            table.add_row(*row)
        console.print(table)

    console.print(f"\n{len(catalogue)} metrics across {len(by_family)} families.")


@app.command()
def explain(metric_id: str) -> None:
    """Show everything about one metric, including how it can be gamed."""
    registry = build_registry()
    metric = registry.get(metric_id)
    if metric is None:
        console.print(f"[red]unknown metric '{metric_id}'[/red]")
        import difflib

        suggestions = difflib.get_close_matches(metric_id, registry.ids(), n=3, cutoff=0.5)
        if suggestions:
            console.print(f"did you mean: [bold green]{', '.join(suggestions)}[/bold green]?")
        else:
            console.print(f"available: {', '.join(sorted(registry.ids()))}")
        raise typer.Exit(1)

    console.print(Panel(metric.name, title=metric.id, expand=False))
    console.print(f"[bold]family[/bold]      {metric.family}")
    console.print(f"[bold]direction[/bold]   {metric.direction.value}")
    console.print(f"[bold]sources[/bold]     {', '.join(metric.sources) or 'internal'}")
    console.print(f"[bold]available[/bold]   t+{metric.earliest_seconds:.0f}s after launch")
    console.print(f"[bold]evidence[/bold]    needs {metric.min_evidence} observations\n")
    console.print(Panel(metric.thesis, title="thesis"))
    console.print(Panel(metric.gameability, title="how it can be gamed, and the counter-measure"))


# --------------------------------------------------------------------------- #
# sweep
# --------------------------------------------------------------------------- #


@app.command()
def sweep(
    config: str = typer.Option(None, help="Path to a config YAML."),
    limit: int = typer.Option(60, help="How many launches to pull per discovery pass."),
    candidates: int = typer.Option(20, help="How many survivors to enrich and score."),
    repeat: int = typer.Option(1, help="Number of sweeps to run. 0 means run forever."),
    interval: float = typer.Option(60.0, help="Seconds between sweeps."),
    log_level: str = typer.Option("INFO", help="Log level."),
) -> None:
    """Run the live loop in paper mode: discover, screen, enrich, score, decide."""
    settings = _settings(config, log_level)
    if settings.trading_mode is TradingMode.LIVE:
        console.print("[red]refusing to sweep in live mode; this build cannot trade[/red]")
        raise typer.Exit(2)
    _banner(settings)

    async def run() -> None:
        pipeline = Pipeline(settings)
        try:
            if repeat == 0:
                await pipeline.run_forever(interval)
                return
            for index in range(repeat):
                report = await pipeline.sweep(limit, candidates)
                _print_sweep(report)
                if index < repeat - 1:
                    await asyncio.sleep(interval)
            console.print(f"\nbroker: {pipeline.broker.summary()}")
        finally:
            await pipeline.aclose()

    asyncio.run(run())


@app.command()
def snipe(
    size: float = typer.Option(None, "--size", "-s", help="Simulated paper position size in SOL (e.g. 0.25)."),
    min_score: float = typer.Option(None, "--min-score", "-m", help="Minimum conviction score threshold to buy."),
    force: bool = typer.Option(False, "--force", "-f", help="Force paper entry even if below threshold or vetoed."),
    limit: int = typer.Option(60, "--limit", help="How many launches to pull during discovery."),
    candidates: int = typer.Option(20, "--candidates", help="How many survivors to deeply enrich and score."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Discover active launches, score them, and snipe the current #1 best token in paper mode."""
    from botsensai.sniper import execute_live_snipe

    settings = _settings(config, log_level)
    asyncio.run(
        execute_live_snipe(
            settings=settings,
            size_sol=size,
            min_score=min_score,
            force=force,
            discover_limit=limit,
            max_candidates=candidates,
        )
    )


@app.command()
def collect(
    hours: float = typer.Option(1.0, "--hours", help="How long to keep collecting, in hours."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    interval: float = typer.Option(60.0, help="Seconds between sweeps."),
    limit: int = typer.Option(60, help="How many launches to pull per discovery pass."),
    candidates: int = typer.Option(20, help="How many survivors to enrich and score."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Collect continuously for a fixed window, writing a heartbeat every sweep.

    This is the command that builds the corpus. Nothing downstream — coverage,
    fitting, a walk-forward backtest — means anything until this has been left
    running for a long time.

    It is built to be boring: a collector that fails costs one surface, a sweep
    that fails costs one sweep, a sweep that hangs is cut off at its budget, and
    Ctrl-C stops the loop with everything collected so far already committed.
    Every sweep writes a row to `collector_runs` whether it worked or not, so
    the time the daemon was *not* running is recoverable afterwards instead of
    being silently indistinguishable from a quiet market.
    """
    settings = _settings(config, log_level)
    if settings.trading_mode is TradingMode.LIVE:
        console.print("[red]refusing to collect in live mode; this build cannot trade[/red]")
        raise typer.Exit(2)
    _banner(settings)

    db = Database(settings.path(settings.db_path))
    before = db.counts()
    console.print(
        f"collecting for {hours:g}h, one sweep every {interval:g}s. "
        f"store holds {before['launches']} launches. Ctrl-C to stop early.\n"
    )

    start_time = datetime.now()
    sweep_count = 0

    def show(report: Any) -> None:
        nonlocal sweep_count
        sweep_count += 1
        summary = report.summary()
        stamp = report.started_at.strftime("%H:%M:%S")
        elapsed = datetime.now() - start_time
        elapsed_str = f"{elapsed.total_seconds() / 3600.0:.2f}h/{hours:g}h"
        note = ""
        if summary["degraded_surfaces"]:
            note = f"  [yellow]degraded: {', '.join(summary['degraded_surfaces'])}[/yellow]"
        console.print(
            f"[dim]{stamp}[/dim] #{sweep_count:<3} {summary['duration_seconds']:>4.1f}s  "
            f"discovered {summary['discovered']:>3} → screened {summary['screened_in']:>3} → "
            f"scored {summary['scored']:>3} → entered {summary['entered']}  "
            f"[dim]({elapsed_str})[/dim]{note}"
        )
        for error in summary["errors"]:
            console.print(f"  [red]{error}[/red]")

    pipeline = Pipeline(settings)

    async def run() -> Any:
        try:
            return await pipeline.collect(
                hours=hours,
                interval_seconds=interval,
                discover_limit=limit,
                max_candidates=candidates,
                on_report=show,
            )
        finally:
            await pipeline.aclose()

    try:
        session = asyncio.run(run())
    except KeyboardInterrupt:
        # The loop handles Ctrl-C itself; this only catches one that landed
        # between sweeps. Everything collected is already committed, so the run
        # still reports rather than dying with a traceback.
        session = pipeline.last_session
        if session is None:
            console.print("[yellow]interrupted before the first sweep[/yellow]")
            raise typer.Exit(130) from None
        session.stopped_because = "interrupted"
        session.finished_at = utcnow()
    _report_collection(session.summary(), before, db.counts())
    _report_collection_gaps(db, tolerance_seconds=max(interval * 3, 120.0))
    db.close()


def _report_collection(
    summary: dict[str, Any], before: dict[str, int], after: dict[str, int]
) -> None:
    table = Table(title="collection session")
    table.add_column("field")
    table.add_column("value", justify="right")
    table.add_row("stopped because", summary["stopped_because"])
    table.add_row("duration", f"{summary['duration_seconds']:.0f}s")
    table.add_row("sweeps", f"{summary['sweeps']} ({summary['failed_sweeps']} with errors)")
    table.add_row("discovered", str(summary["discovered"]))
    table.add_row("scored", str(summary["scored"]))
    table.add_row("entered", str(summary["entered"]))
    for name in ("launches", "market_snapshots", "trades", "holders", "social_posts", "scores"):
        table.add_row(f"new {name}", f"+{after[name] - before[name]}")
    console.print(table)

    if summary["degraded_surfaces"]:
        console.print(f"[yellow]degraded surfaces: {summary['degraded_surfaces']}[/yellow]")


def _report_collection_gaps(db: Database, tolerance_seconds: float) -> None:
    """Show when nothing was collecting, which is what the heartbeats are for."""
    gaps = db.collection_gaps(tolerance_seconds=tolerance_seconds)
    if not gaps:
        return
    console.print(f"\n[yellow]{len(gaps)} collection gap(s) in the heartbeat history:[/yellow]")
    for gap in gaps[-5:]:
        console.print(
            f"  {gap['seconds']:.0f}s with nothing collecting, "
            f"{gap['after']:%Y-%m-%d %H:%M} → {gap['before']:%H:%M}"
        )


@app.command()
def stream(
    minutes: float = typer.Option(10.0, "--minutes", help="How long to stay subscribed."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    url: str = typer.Option(None, help="Override the websocket endpoint (testing)."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Take pump.fun mints off the websocket instead of waiting for the next poll.

    `collect` discovers by polling `frontend-api-v3/coins` once a sweep, so at a
    one-minute cadence a token is on average thirty seconds old before this
    system has heard of it. This command subscribes to the mint firehose and
    writes each launch on arrival.

    It is meant to be run *alongside* `collect`, as a second process — the store
    is a WAL sqlite and takes concurrent writers, and keeping the two apart means
    a socket outage cannot disturb the sweep loop's deadline handling. Whichever
    path sees a token first supplies its `observed_at`; whichever supplies the
    earlier `created_at` wins that, which is how the latency below is measured.
    """
    from botsensai.collectors.pumpfun_ws import PUMPPORTAL_WS, WS_SURFACE, PumpPortalStream

    settings = _settings(config, log_level)
    _banner(settings)

    db = Database(settings.path(settings.db_path))
    before = db.counts()
    endpoint = url or PUMPPORTAL_WS
    console.print(
        f"streaming {endpoint} for {minutes:g} minute(s). "
        f"store holds {before['launches']} launches. Ctrl-C to stop early.\n"
    )

    ws = PumpPortalStream(settings, db=db, url=endpoint)

    async def run() -> Any:
        return await ws.run(max_seconds=minutes * 60.0)

    try:
        stats = asyncio.run(run())
    except KeyboardInterrupt:
        # Same split as `collect`: the loop catches the cancellation asyncio.run
        # delivers, and this only catches one that landed outside it.
        stats = ws.stats
        stats.stopped_because = "interrupted"

    summary = stats.summary()
    after = db.counts()

    table = Table(title="stream session")
    table.add_column("field")
    table.add_column("value", justify="right")
    table.add_row("stopped because", summary["stopped_because"])
    table.add_row("duration", f"{summary['duration_seconds']:.0f}s")
    table.add_row("connections", f"{summary['connections']} ({summary['failed_connections']} failed)")
    table.add_row("messages", str(summary["messages"]))
    table.add_row("mints", f"{summary['launches']} ({summary['novel_mints']} not already stored)")
    table.add_row("migrations", str(summary["migrations"]))
    table.add_row("unparsed frames", str(summary["unparsed"]))
    for name in ("launches", "market_snapshots"):
        table.add_row(f"new {name}", f"+{after[name] - before[name]}")
    console.print(table)

    for error in summary["errors"]:
        console.print(f"[yellow]{error}[/yellow]")

    latency = db.observation_latency()
    if latency:
        table = Table(title="latency to first observation, by discovering surface")
        table.add_column("source")
        table.add_column("launches", justify="right")
        table.add_column("measured", justify="right")
        table.add_column("median", justify="right")
        table.add_column("p90", justify="right")
        for source, row in latency.items():
            median = f"{row['median_seconds']:.0f}s" if row["median_seconds"] is not None else "—"
            p90 = f"{row['p90_seconds']:.0f}s" if row["p90_seconds"] is not None else "—"
            table.add_row(source, str(row["launches"]), str(row["measured"]), median, p90)
        console.print(table)
        console.print(
            "[dim]measured counts only tokens whose mint time came from somewhere other than "
            f"the observation itself. The {WS_SURFACE} feed carries no timestamp, so its rows "
            "join that column once a REST sweep corroborates them.[/dim]"
        )
    db.close()


@app.command()
def label(
    min_age_hours: float = typer.Option(
        24.0, "--min-age-hours", help="Only label launches at least this old."
    ),
    config: str = typer.Option(None, help="Path to a config YAML."),
    limit: int = typer.Option(None, help="Stop after this many launches."),
    refresh: bool = typer.Option(False, "--refresh", help="Re-label launches that already have an outcome."),
    ohlcv: bool = typer.Option(True, "--ohlcv/--no-ohlcv", help="Extend thin paths with GeckoTerminal candles."),
    max_ohlcv: int = typer.Option(40, help="Cap on tokens fetched from GeckoTerminal this pass."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Write ground-truth `Outcome` rows for launches old enough to have one.

    This is the command that unblocks every fitting and walk-forward task: with
    no outcomes there is no training target, and a scorer with no target is a
    set of opinions.

    The number to look at in the output is the gap between the peak multiple and
    the realizable one. The peak is what the chart did; the realizable figure is
    what a position of the size this system actually takes would have received
    selling into the depth that was really there. On thin tokens the second is a
    small fraction of the first, and training on the first teaches the scorer to
    find spikes nobody could have sold.
    """
    from botsensai.labeller import LabelPolicy, OutcomeLabeller

    settings = _settings(config, log_level)
    _banner(settings)

    db = Database(settings.path(settings.db_path))
    before = db.counts()
    policy = LabelPolicy.from_settings(settings, min_age_hours=min_age_hours)
    labeller = OutcomeLabeller(settings, db=db, policy=policy)
    console.print(
        f"labelling launches older than {min_age_hours:g}h at a "
        f"{policy.position_size_native:g} SOL exit size. "
        f"store holds {before['outcomes']} outcomes.\n"
    )

    async def run() -> Any:
        try:
            return await labeller.run(
                limit=limit, refresh=refresh, use_ohlcv=ohlcv, max_ohlcv=max_ohlcv
            )
        finally:
            await labeller.aclose()

    try:
        stats = asyncio.run(run())
    except KeyboardInterrupt:
        # Outcomes are written one at a time, so whatever finished is committed.
        stats = labeller.stats
        console.print("[yellow]interrupted; outcomes written so far are committed[/yellow]")

    summary = stats.summary()
    after = db.counts()

    table = Table(title="labelling pass")
    table.add_column("field")
    table.add_column("value", justify="right")
    table.add_row("considered", str(summary["considered"]))
    table.add_row("labelled", str(summary["labelled"]))
    table.add_row("no snapshots", str(summary["skipped_no_path"]))
    table.add_row("no usable price", str(summary["skipped_no_price"]))
    table.add_row("no t0 price", str(summary["without_t0"]))
    table.add_row("graduated", str(summary["graduated"]))
    table.add_row("rugged", str(summary["rugged"]))
    table.add_row(
        "ohlcv tokens",
        f"{summary['ohlcv_fetched']}/{summary['ohlcv_attempts']} "
        f"({summary['ohlcv_points']} candles)",
    )
    table.add_row("new outcomes", f"+{after['outcomes'] - before['outcomes']}")
    console.print(table)

    for error in summary["errors"][:5]:
        console.print(f"[yellow]{error}[/yellow]")

    spread = db.outcome_spread()
    if spread["labelled"]:
        table = Table(title="peak vs realizable, over every labelled outcome")
        table.add_column("figure")
        table.add_column("median", justify="right")
        table.add_column("p90", justify="right")
        table.add_column("max", justify="right")
        for name, key in (("peak multiple", "peak"), ("realizable multiple", "realizable")):
            row = spread[key]
            if row["median"] is None:
                table.add_row(name, "—", "—", "—")
                continue
            table.add_row(name, f"{row['median']:.2f}x", f"{row['p90']:.2f}x", f"{row['max']:.2f}x")
        console.print(table)
        console.print(
            f"[dim]{spread['with_both']} outcomes carry both numbers; the realizable figure is "
            "the peak net of the depth actually available to exit at "
            f"{policy.position_size_native:g} SOL.[/dim]"
        )
    db.close()


def _print_sweep(report: Any) -> None:
    summary = report.summary()
    console.print(
        f"\n[bold]sweep[/bold] {summary['duration_seconds']}s  "
        f"discovered {summary['discovered']} → screened {summary['screened_in']} → "
        f"scored {summary['scored']} → entered {summary['entered']}  "
        f"regime={summary['regime']}"
    )
    if summary["degraded_surfaces"]:
        console.print(f"[yellow]degraded: {', '.join(summary['degraded_surfaces'])}[/yellow]")
    for error in summary["errors"]:
        console.print(f"[red]{error}[/red]")

    if summary["top_candidates"]:
        table = Table(title="top candidates")
        table.add_column("symbol")
        table.add_column("score", justify="right")
        table.add_column("cov", justify="right")
        table.add_column("vetoes")
        for candidate in summary["top_candidates"]:
            vetoes = ", ".join(candidate["vetoes"]) or "—"
            colour = "red" if candidate["vetoes"] else "green"
            table.add_row(
                candidate["symbol"] or "?",
                f"[{colour}]{candidate['score']:.3f}[/{colour}]",
                f"{candidate['coverage']:.0%}",
                vetoes,
            )
        console.print(table)


# --------------------------------------------------------------------------- #
# backtest
# --------------------------------------------------------------------------- #


@app.command()
def backtest(
    config: str = typer.Option(None, help="Path to a config YAML."),
    days: float = typer.Option(2.0, help="How far back to replay, in days."),
    synthetic: bool = typer.Option(
        False, "--synthetic", help="Force a synthetic run instead of using the store."
    ),
    universe: int = typer.Option(60, help="Synthetic universe size when running synthetic."),
    capital: float = typer.Option(10.0, help="Starting capital in native units (SOL)."),
    baselines: bool = typer.Option(
        False, "--baselines", help="Compare composite strategy against null-hypothesis baselines."
    ),
    out: str = typer.Option(None, help="Write the full result to this JSON path."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Replay history through the full decision stack with realistic fills.

    If the store holds too little data to be worth replaying, this falls back to
    a synthetic run and labels it as such in every output. A synthetic result
    demonstrates that the pipeline executes; it says nothing about profitability
    and the report says so explicitly.
    """
    settings = _settings(config, log_level)
    _banner(settings)

    tester = Backtester(settings)
    end = utcnow()
    start = end - timedelta(days=days)

    tapes, synthetic = _backtest_tapes(
        settings, start, end, days=days, synthetic=synthetic, universe=universe
    )

    with console.status("[bold cyan]Replaying market history through decision stack…"):
        result = tester.run(tapes, starting_native=capital, synthetic=synthetic)
    summary = result.summary()

    comparison = None
    if baselines:
        b_results = run_baselines(
            tapes, settings=settings, starting_native=capital, synthetic=synthetic
        )
        comparison = evaluate_baselines(result, b_results)
        _print_baselines_table(comparison)
    else:
        _print_backtest_table(summary)

    _print_backtest_caveats(result, summary)

    if out:
        Path(out).write_text(
            json.dumps(
                _backtest_payload(result, summary, comparison=comparison), indent=2, default=str
            ),
            encoding="utf-8",
        )
        console.print(f"\nwrote {out}")


# --------------------------------------------------------------------------- #
# fit
# --------------------------------------------------------------------------- #


@app.command()
def fit(
    config: str = typer.Option(None, help="Path to a config YAML."),
    days: float = typer.Option(90.0, help="How far back to look for labelled outcomes in days."),
    min_samples: int = typer.Option(
        MIN_SAMPLES_TO_FIT, help="Minimum labelled samples required to fit."
    ),
    synthetic: bool = typer.Option(
        False, "--synthetic", help="Force a synthetic run instead of using the store."
    ),
    universe: int = typer.Option(60, help="Synthetic universe size when running synthetic."),
    holdout_fraction: float = typer.Option(
        0.25, help="Fraction of examples reserved for holdout evaluation."
    ),
    seed: int = typer.Option(1337, help="RNG seed for fitting and splitting."),
    out: str = typer.Option(
        None, help="Write fitted weights to this JSON path (defaults to config/weights.json)."
    ),
    target: str = typer.Option(
        "realizable", help="Target metric for fitting: realizable | peak | survival"
    ),
    regimes: bool = typer.Option(
        False, "--regimes", help="Fit separate weights for hot, normal and dead regimes if each has >= 200 samples."
    ),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Fit signal weights on labelled outcomes using coordinate ascent on rank correlation.

    Reads labelled outcomes from the store (filtering on non-null realizable/peak multiples)
    or generates a synthetic cohort, fits metric weights and family budgets, and evaluates
    holdout rank correlation and top-decile lift.
    """
    settings = _settings(config, log_level)
    _banner(settings)

    registry = build_registry()
    examples, is_synth = _fit_examples(
        settings,
        registry,
        days=days,
        min_samples=min_samples,
        synthetic=synthetic,
        universe=universe,
        seed=seed,
        target=target,
    )

    with console.status("[bold green]Fitting weights via coordinate ascent on rank correlation…"):
        if regimes:
            regime_report = fit_regime_weights(
                examples, registry=registry, seed=seed, holdout_fraction=holdout_fraction
            )
            if not regime_report.shipped:
                console.print(f"\n[yellow]⚠️  {regime_report.skip_reason}[/yellow]")
                report = WeightFitter(registry, seed=seed).fit(examples, holdout_fraction=holdout_fraction)
                _print_fit_table(report)
                _print_fit_summary(report, is_synthetic=is_synth)
            else:
                console.print("\n[bold green]Fitted regime-conditional weight sets for hot, normal, and dead regimes.[/bold green]")
                report = FitReport(weights=regime_report.weights, n_samples=len(examples), fitted=True)
                for rname, rrep in regime_report.reports.items():
                    console.print(f"\n--- Regime: [cyan]{rname}[/cyan] (n={rrep.n_samples}) ---")
                    _print_fit_table(rrep)
        else:
            fitter = WeightFitter(registry, seed=seed)
            report = fitter.fit(examples, holdout_fraction=holdout_fraction)
            _print_fit_table(report)
            _print_fit_summary(report, is_synthetic=is_synth)

    out_path = Path(out or settings.scoring.weights_path or "config/weights.json")

    if report.fitted:
        if report.holdout_rank_correlation <= 0.05 and not regimes:
            console.print(
                f"\n[bold red]⚠️  WARNING: holdout rank correlation ({report.holdout_rank_correlation:.4f}) "
                "is at or below 0.05 — these metrics are not predicting outcomes on this dataset. "
                "Fitted weights will NOT be shipped.[/bold red]"
            )
        else:
            report.weights.save(out_path)
            console.print(f"\nwrote fitted weights to {out_path}")
    else:
        console.print(
            f"\n[yellow]Fitting skipped ({len(examples)} < {min_samples} labelled examples). "
            "Default weights remain unchanged.[/yellow]"
        )


def _fit_examples(
    settings: Settings,
    registry: Any,
    *,
    days: float,
    min_samples: int,
    synthetic: bool,
    universe: int,
    seed: int,
    target: str,
) -> tuple[list[TrainingExample], bool]:
    """Load training examples from DB or generate synthetic ones."""
    if not synthetic:
        db = Database(settings.path(settings.db_path))
        end = utcnow()
        start = end - timedelta(days=days)
        tapes = Backtester.tapes_from_database(db, start, end)
        labelled = [t for t in tapes if _has_label(t)]
        wf = WalkForward(settings, registry, fit_target=target)
        examples = wf.training_examples(labelled)
        if len(examples) >= min_samples:
            return examples, False
        console.print(
            f"[yellow]store holds only {len(examples)} labelled training examples with valid multiples "
            f"in the last {days} days — below threshold {min_samples}.[/yellow]"
        )
        if len(examples) > 0:
            return examples, False

    cohort = generate_cohort(n=universe, seed=seed)
    outcomes = {t.launch.token.key: synthetic_outcome(t) for t in cohort}
    tapes = Backtester.tapes_from_synthetic(cohort, outcomes=outcomes)
    wf = WalkForward(settings, registry, fit_target=target)
    return wf.training_examples(tapes), True


def _print_fit_table(report: FitReport) -> None:
    table = Table(title=f"weight fit report (n={report.n_samples}, fitted={report.fitted})")
    table.add_column("family / metric", style="cyan")
    table.add_column("weight", justify="right")
    table.add_column("top-decile lift", justify="right")

    table.add_section()
    table.add_row("[bold]Family Budgets[/bold]", "", "")
    for family, weight in report.weights.families.items():
        table.add_row(f"  {family}", f"{weight:.4f}", "—")

    table.add_section()
    table.add_row("[bold]Metric Weights[/bold]", "", "")
    for metric_id, weight in sorted(report.weights.metrics.items()):
        lift = report.per_metric_lift.get(metric_id)
        lift_str = f"{lift:.4f}" if lift is not None else "—"
        table.add_row(f"  {metric_id}", f"{weight:.4f}", lift_str)

    console.print(table)


def _print_fit_summary(report: FitReport, *, is_synthetic: bool) -> None:
    if is_synthetic:
        console.print(
            "\n[yellow bold]SYNTHETIC RUN:[/yellow bold] "
            "Fitted against synthetic price paths and outcomes. "
            "Proves execution, not predictive edge."
        )

    summary = report.summary()
    console.print(
        f"\n[bold]Fit summary:[/bold]\n"
        f"  • samples: {summary['n_samples']}\n"
        f"  • fitted: {summary['fitted']}\n"
        f"  • train rank correlation: {summary['train_rank_correlation']:.4f}\n"
        f"  • holdout rank correlation: {summary['holdout_rank_correlation']:.4f}\n"
        f"  • top-decile lift: {summary['top_decile_lift']:.4f}\n"
        f"  • version: {summary['weights_version']}"
    )

    if report.warnings:
        console.print("\n[bold]Warnings / Caveats:[/bold]")
        for w in report.warnings:
            console.print(f"  • [yellow]{w}[/yellow]")


# --------------------------------------------------------------------------- #
# ablate
# --------------------------------------------------------------------------- #


@app.command()
def ablate(
    config: str = typer.Option(None, help="Path to a config YAML."),
    days: float = typer.Option(30.0, help="How far back to look for labelled outcomes in days."),
    synthetic: bool = typer.Option(
        False, "--synthetic", help="Force a synthetic run instead of using the store."
    ),
    universe: int = typer.Option(60, help="Synthetic universe size when running synthetic."),
    holdout_fraction: float = typer.Option(
        0.25, help="Fraction of examples reserved for holdout evaluation."
    ),
    seed: int = typer.Option(1337, help="RNG seed for fitting and splitting."),
    out: str = typer.Option(None, help="Write the full ablation report to this JSON path."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Drop each metric in turn and measure how much the holdout objective falls.

    Metrics whose removal improves the objective are liabilities — they are
    actively harmful and flagged loudly in the report.
    """
    settings = _settings(config, log_level)
    _banner(settings)

    registry = build_registry()
    examples, is_synth = _ablate_examples(
        settings, registry, days=days, synthetic=synthetic, universe=universe, seed=seed
    )

    with console.status("[bold yellow]Running ablation study across metric families…"):
        report = ablation(examples, registry=registry, seed=seed, holdout_fraction=holdout_fraction)

    _print_ablation_table(report)
    _print_ablation_summary(report, is_synthetic=is_synth)

    if out:
        payload = {
            "summary": report.summary(),
            "synthetic": is_synth,
            "rows": [r.describe() for r in report.rows],
            "liabilities": [r.metric_id for r in report.liabilities],
        }
        Path(out).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        console.print(f"\nwrote {out}")


def _ablate_examples(
    settings: Settings,
    registry: Any,
    *,
    days: float,
    synthetic: bool,
    universe: int,
    seed: int,
) -> tuple[list[TrainingExample], bool]:
    """Load training examples from DB or generate synthetic ones."""
    if not synthetic:
        db = Database(settings.path(settings.db_path))
        end = utcnow()
        start = end - timedelta(days=days)
        tapes = Backtester.tapes_from_database(db, start, end)
        labelled = [t for t in tapes if t.outcome is not None]
        wf = WalkForward(settings, registry)
        examples = wf.training_examples(labelled)
        if len(examples) >= 10:
            return examples, False
        console.print(
            f"[yellow]store holds only {len(examples)} labelled training examples in the last "
            f"{days} days — falling back to a synthetic run.[/yellow]"
        )

    cohort = generate_cohort(n=universe, seed=seed)
    outcomes = {t.launch.token.key: synthetic_outcome(t) for t in cohort}
    tapes = Backtester.tapes_from_synthetic(cohort, outcomes=outcomes)
    wf = WalkForward(settings, registry)
    return wf.training_examples(tapes), True


def _print_ablation_table(report: AblationReport) -> None:
    table = Table(title=f"metric ablation report ({len(report.rows)} metrics)")
    table.add_column("family", style="cyan")
    table.add_column("metric", style="bold")
    table.add_column("state")
    table.add_column("coverage", justify="right")
    table.add_column("objective delta", justify="right")

    for r in report.rows:
        if r.liability:
            state_fmt = "[red bold]LIABILITY[/red bold]"
            delta_fmt = f"[red]{r.delta:+.5f}[/red]"
        elif r.inert:
            state_fmt = "[yellow]inert[/yellow]"
            delta_fmt = f"[yellow]{r.delta:+.5f}[/yellow]"
        elif not r.measured:
            state_fmt = "[dim]not measured[/dim]"
            delta_fmt = "[dim]N/A[/dim]"
        else:
            state_fmt = "[green]contributes[/green]"
            delta_fmt = f"[green]{r.delta:+.5f}[/green]"

        cov_fmt = f"{r.coverage}/{report.n_holdout}" if r.measured else "0"
        table.add_row(r.family, r.metric_id, state_fmt, cov_fmt, delta_fmt)

    console.print(table)


def _print_ablation_summary(report: AblationReport, *, is_synthetic: bool) -> None:
    if is_synthetic:
        console.print(
            "\n[yellow bold]SYNTHETIC RUN:[/yellow bold] "
            "Evaluated against synthetic price paths and posts. "
            "Proves execution, not predictive edge."
        )

    if report.liabilities:
        console.print(
            f"\n[red bold]⚠️  LIABILITY WARNING: {len(report.liabilities)} metric(s) "
            f"actively degrade the holdout objective![/red bold]"
        )
        for r in report.liabilities:
            console.print(
                f"  • [red]{r.metric_id}[/red] ({r.family}): "
                f"removal improves objective by [bold]{abs(r.delta):.5f}[/bold]"
            )
    else:
        console.print("\n[green]No liability metrics identified.[/green]")

    if report.warnings:
        console.print("\n[bold]Caveats:[/bold]")
        for w in report.warnings:
            console.print(f"  • [yellow]{w}[/yellow]")


def _backtest_tapes(
    settings: Settings,
    start: datetime,
    end: datetime,
    *,
    days: float,
    synthetic: bool,
    universe: int,
) -> tuple[list[Any], bool]:
    """Replayable tapes for the window, falling back to synthetic when too thin.

    Returns the tapes together with whether they ended up synthetic, because the
    caller must label the result honestly and the fallback can flip the flag.
    """
    if not synthetic:
        db = Database(settings.path(settings.db_path))
        tapes = Backtester.tapes_from_database(db, start, end)
        usable = [t for t in tapes if len(t.snapshots) >= 3]
        if len(usable) >= 10:
            return usable, False
        console.print(
            f"[yellow]store holds only {len(usable)} replayable tokens in the last "
            f"{days} days — falling back to a synthetic run. Collect for a while with "
            f"'botsensai sweep --repeat 0' to build a real corpus.[/yellow]"
        )

    cohort = generate_cohort(n=universe, seed=settings.seed)
    return Backtester.tapes_from_synthetic(cohort), True


def _print_backtest_table(summary: dict[str, Any]) -> None:
    table = Table(title="backtest result")
    table.add_column("metric")
    table.add_column("value", justify="right")
    for key in (
        "universe_size",
        "evaluated",
        "entered",
        "trades",
        "total_pnl_native",
        "expectancy_native",
        "win_rate",
        "median_multiple",
        "profit_factor",
        "max_drawdown_native",
    ):
        if key in summary:
            table.add_row(key.replace("_", " "), str(summary[key]))
    console.print(table)


def _print_baselines_table(comparison: BaselineComparison) -> None:
    table = Table(title="baseline comparison (null-hypothesis testing)")
    table.add_column("strategy", style="bold", no_wrap=True)
    table.add_column("entered", justify="right")
    table.add_column("trades", justify="right")
    table.add_column("win rate", justify="right")
    table.add_column("total pnl", justify="right")
    table.add_column("expectancy", justify="right")
    table.add_column("profit factor", justify="right")

    comp_s = comparison.composite.summary()
    table.add_row(
        "composite (signal)",
        str(comp_s.get("entered", 0)),
        str(comp_s.get("trades", 0)),
        f"{comp_s.get('win_rate', 0.0):.1%}",
        f"{comp_s.get('total_pnl_native', 0.0):+.4f}",
        f"{comp_s.get('expectancy_native', 0.0):+.6f}",
        str(comp_s.get("profit_factor", "0.0")),
    )

    for mode, b_res in comparison.baselines.items():
        b_s = b_res.summary()
        table.add_row(
            f"baseline: {mode}",
            str(b_s.get("entered", 0)),
            str(b_s.get("trades", 0)),
            f"{b_s.get('win_rate', 0.0):.1%}",
            f"{b_s.get('total_pnl_native', 0.0):+.4f}",
            f"{b_s.get('expectancy_native', 0.0):+.6f}",
            str(b_s.get("profit_factor", "0.0")),
        )

    console.print(table)
    if not comparison.has_edge:
        console.print(
            "\n[bold yellow]warning: the composite strategy does not beat all three "
            "null-hypothesis baselines — it has no edge[/bold yellow]"
        )
    else:
        console.print(
            "\n[bold green]composite strategy beat all three null-hypothesis baselines[/bold green]"
        )


def _print_backtest_caveats(result: Any, summary: dict[str, Any]) -> None:
    """Everything that qualifies the headline: the CI, the warnings, the vetoes."""
    low, high = result.bootstrap_expectancy_ci()
    if low or high:
        console.print(
            f"\nbootstrap 95% CI on expectancy: [{low:+.6f}, {high:+.6f}] native per trade"
        )
        if low <= 0 <= high:
            console.print(
                "[yellow]the interval spans zero — this result is not distinguishable "
                "from no edge[/yellow]"
            )

    for key in ("warning", "warning_synthetic"):
        if summary.get(key):
            console.print(f"[yellow]{summary[key]}[/yellow]")
    for note in result.notes:
        console.print(f"[dim]note: {note}[/dim]")

    if result.veto_counts:
        console.print(f"\nvetoes triggered: {result.veto_counts}")


def _backtest_payload(
    result: Any, summary: dict[str, Any], comparison: BaselineComparison | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "summary": summary,
        "veto_counts": result.veto_counts,
        "metric_coverage": result.metric_coverage,
        "trades": [
            {
                "token": t.token_key,
                "symbol": t.symbol,
                "score": t.score,
                "pnl_native": t.pnl_native,
                "multiple": t.multiple,
                "exit_reason": t.exit_reason,
                "hold_seconds": t.hold_seconds,
            }
            for t in result.trades
        ],
    }
    if comparison is not None:
        payload["baselines"] = comparison.summary()
    return payload


# --------------------------------------------------------------------------- #
# score / content / memory
# --------------------------------------------------------------------------- #


@app.command()
def score(
    mint: str = typer.Argument(..., help="Token mint address to score."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    write_post: bool = typer.Option(False, "--post", help="Also generate a blog post."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Collect, score and explain one specific token."""
    settings = _settings(config, log_level)

    async def run() -> None:
        pipeline = Pipeline(settings)
        try:
            discovered = await pipeline.discover(80)
            match = next(
                (launch for launch in discovered.launches if launch.token.mint == mint), None
            )
            if match is None:
                match = pipeline.db.launch(f"solana:{mint}")
            if match is None:
                console.print(f"[red]token {mint} not found in the recent feed or the store[/red]")
                raise typer.Exit(1)

            await pipeline.enrich([match])
            pipeline.compute_regime()
            result = pipeline.score(match)

            console.print(
                Panel(
                    f"{match.token.name or ''} (${match.token.symbol or '?'})",
                    title=f"score {result.composite:.3f}",
                    expand=False,
                )
            )
            console.print(result.explanation or "")

            table = Table(title="metric values")
            table.add_column("metric")
            table.add_column("raw", justify="right")
            table.add_column("norm", justify="right")
            table.add_column("confidence")
            for value in sorted(
                result.metric_values, key=lambda v: (v.normalized is None, -(v.normalized or 0))
            ):
                table.add_row(
                    value.metric_id,
                    f"{value.raw:.4f}" if value.raw is not None else "—",
                    f"{value.normalized:.3f}" if value.normalized is not None else "—",
                    value.confidence.value,
                )
            console.print(table)

            if write_post:
                generator = ContentGenerator(settings, pipeline.metrics, pipeline.memory)
                piece = generator.blog_post(match, result)
                path = generator.save(piece)
                console.print(f"\nwrote {path}")
        finally:
            await pipeline.aclose()

    asyncio.run(run())


@app.command(name="x-setup")
def x_setup(
    config: str = typer.Option(None, help="Path to the config YAML to write."),
    profile: str = typer.Option(None, help="Profile directory. Defaults to ~/botsensai-x-profile."),
    chrome: str = typer.Option(None, help="Path to the Chrome binary, if it is not in a standard location."),
    launch: bool = typer.Option(True, help="Open Chrome for you. Pass --no-launch to do it yourself."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Create the X browser profile, log in, verify, and write the config.

    Collapses the four manual setup steps into one command. It never handles a
    credential — Chrome does the login in a window you drive, and this waits,
    then checks whether the session took.

    Run this on the machine that will do the collecting: the profile lives on
    disk, and the collector runs Chrome headed.
    """
    from botsensai.setup_x import (
        BURNER_CONFIRMATION,
        config_path_for,
        default_profile_dir,
        find_chrome,
        launch_chrome_for_login,
        manual_launch_command,
        patch_config,
        prepare_profile,
        profile_is_locked,
    )

    settings = _settings(config, log_level)
    profile_dir = Path(profile).expanduser() if profile else default_profile_dir()

    console.print(
        Panel(
            "This sets up a logged-in Chrome profile so Botsensai can read reply text,\n"
            "bookmarks and engager account ages from X — none of which exist on any\n"
            "free path.\n\n"
            "[yellow]Use a throwaway account.[/yellow] Sustained automated reads from a\n"
            "logged-in account are against X's terms, and the realistic consequence is\n"
            "suspension of that account.\n\n"
            "Botsensai never sees your password. You log in yourself in the window that\n"
            "opens; this only checks afterwards whether it worked.",
            title="X session setup",
            expand=False,
        )
    )

    created, message = prepare_profile(profile_dir)
    if not created and "refusing" in message:
        console.print(f"[red]{message}[/red]")
        raise typer.Exit(2)
    console.print(f"profile: {message}")

    chrome_path = find_chrome(chrome)
    if chrome_path is None:
        console.print(
            "[yellow]could not find a Chrome binary.[/yellow] Install Chrome, or pass "
            "--chrome with the full path."
        )
        console.print(f"\nThen run:\n  {manual_launch_command(None, profile_dir)}")
        raise typer.Exit(2)
    console.print(f"browser: {chrome_path}")

    if launch:
        console.print("\nOpening Chrome. Log in to X with your throwaway account.")
        if launch_chrome_for_login(chrome_path, profile_dir) is None:
            console.print("[yellow]could not launch Chrome automatically.[/yellow]")
            console.print(f"Run this yourself:\n  {manual_launch_command(chrome_path, profile_dir)}")
    else:
        console.print(f"\nRun this, log in, then come back:\n  {manual_launch_command(chrome_path, profile_dir)}")

    console.print(
        "\n[bold]When you have logged in, close Chrome completely[/bold] — it holds a "
        "lock on the profile directory and Playwright cannot open a locked profile."
    )
    typer.prompt("Press Enter once Chrome is closed", default="", show_default=False)

    if profile_is_locked(profile_dir):
        console.print(
            "[yellow]Chrome still appears to be holding the profile.[/yellow] Quit Chrome "
            "entirely (Cmd-Q, not just closing the window) and run `botsensai x-session` "
            "to check."
        )
        raise typer.Exit(1)

    from botsensai.collectors.x_session import AuthenticatedXCollector

    settings.browser.user_data_dir = str(profile_dir)

    async def check() -> Any:
        collector = AuthenticatedXCollector(settings)
        try:
            return await collector.verify_session(force=True)
        finally:
            await collector.aclose()

    console.print("\nverifying session...")
    status = asyncio.run(check())

    if not status.authenticated:
        console.print(f"[yellow]{status.explain()}[/yellow]")
        console.print(
            "\nNothing has been written to your config. Re-run this command, or see "
            "docs/X_SESSION.md for the manual path."
        )
        raise typer.Exit(1)

    console.print(f"[green]{status.explain()}[/green]")

    console.print(
        f"\nTo enable collection, confirm this is an account you are willing to lose.\n"
        f"Type [bold]{BURNER_CONFIRMATION}[/bold] to set acknowledged_burner, or press "
        f"Enter to leave it off."
    )
    answer = typer.prompt("confirm", default="", show_default=False).strip().lower()
    acknowledged = answer == BURNER_CONFIRMATION

    target = config_path_for(settings, config)
    ok, detail = patch_config(target, profile_dir, acknowledged)
    console.print(f"\nconfig: {detail}")
    if not ok:
        raise typer.Exit(1)

    if acknowledged:
        console.print(
            "\n[green]Session collection is active.[/green] Run `botsensai x-session` "
            "any time to re-check, and `botsensai sweep` to collect."
        )
    else:
        console.print(
            "\n[yellow]Profile saved but collection stays off[/yellow] — "
            "acknowledged_burner was not set. Set it in the config when you are ready."
        )


@app.command(name="x-session")
def x_session(
    config: str = typer.Option(None, help="Path to a config YAML."),
    profile: str = typer.Option(None, help="Chrome profile directory to check."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Check whether the configured Chrome profile is logged in to X.

    Run this before enabling session collection, and again whenever social
    coverage drops. A session that has silently expired produces empty results
    that are indistinguishable from tokens with no social activity, which is the
    single worst failure mode in the collection layer — so this command exists to
    make the state explicit rather than inferred.
    """
    settings = _settings(config, log_level)
    if profile:
        settings.browser.user_data_dir = profile

    from botsensai.collectors.x_session import AuthenticatedXCollector

    async def run() -> None:
        collector = AuthenticatedXCollector(settings)
        try:
            status = await collector.verify_session(force=True)
        finally:
            await collector.aclose()

        colour = "green" if status.authenticated else "yellow"
        console.print(Panel(f"[{colour}]{status.explain()}[/{colour}]", title="X session"))

        gate = settings.x_session_enabled
        table = Table(title="session collection gate")
        table.add_column("requirement")
        table.add_column("state")
        for label, ok in (
            ("x_session.enabled", settings.x_session.enabled),
            ("x_session.acknowledged_burner", settings.x_session.acknowledged_burner),
            ("browser.user_data_dir set", bool(settings.browser.user_data_dir)),
            ("profile is logged in", status.authenticated),
        ):
            table.add_row(label, "[green]yes[/green]" if ok else "[red]no[/red]")
        console.print(table)

        if not status.authenticated:
            console.print(
                "\n[dim]To set this up: launch Chrome with a dedicated profile directory,\n"
                "log in to X manually with a throwaway account, close Chrome, then point\n"
                "browser.user_data_dir at that directory. See docs/X_SESSION.md.[/dim]"
            )
        elif not gate:
            console.print(
                "\n[yellow]Session is valid but collection is still gated off.[/yellow] "
                "Set x_session.enabled and x_session.acknowledged_burner to true once you "
                "have confirmed this profile belongs to an account you are willing to lose."
            )
        else:
            console.print("\n[green]Session collection is active.[/green]")

    asyncio.run(run())


@app.command()
def dashboard(
    out: str = typer.Option(..., "--out", help="Where to write the HTML file."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Write a self-contained HTML dashboard.

    One file, no server, no external assets, safe to share. The integrity panel
    is the point of it: five collection bugs found on 2026-07-29 all presented
    as a healthy system, and two of them fabricated evidence rather than hiding
    it. Scores alone would have looked entirely plausible throughout.
    """
    from botsensai.dashboard.data import build_snapshot
    from botsensai.dashboard.render import render_html

    settings = _settings(config, log_level)
    db = Database(settings.path(settings.db_path))
    try:
        snapshot = build_snapshot(settings, db)
    finally:
        db.close()

    target = Path(out).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_html(snapshot), encoding="utf-8")

    alarms = [f for f in snapshot["integrity"] if f["level"] == "alarm"]
    console.print(f"wrote {target} ({target.stat().st_size} bytes)")
    if alarms:
        console.print(f"[red]{len(alarms)} integrity alarm(s):[/red]")
        for flag in alarms:
            console.print(f"  [red]{flag['headline']}[/red] — {flag['detail']}")


@app.command(name="ui")
def live_ui(
    host: str = typer.Option("127.0.0.1", "--host", help="Host interface to bind."),
    port: int = typer.Option(8000, "--port", help="Port to listen on."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open browser automatically."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("INFO", help="Log level."),
) -> None:
    """Launch the real-time live WebSocket dashboard."""
    import uvicorn

    from botsensai.dashboard.server import create_app

    settings = _settings(config, log_level)
    _banner(settings)
    app_instance = create_app(settings)

    url = f"http://{host}:{port}"
    console.print(f"\n[green]🚀 Live Dashboard starting at[/green] [bold cyan]{url}[/bold cyan]")
    console.print("[dim]Press Ctrl+C to stop the dashboard server.[/dim]\n")

    if open_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(url)

    uvicorn.run(app_instance, host=host, port=port, log_level=log_level.lower())


@app.command(name="tui")
def live_tui(
    seconds: float = typer.Option(None, "--seconds", help="Max seconds to run before exit (useful for testing)."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Launch the split-pane live terminal user interface (TUI)."""
    from botsensai.tui import run_tui_loop

    settings = _settings(config, log_level)
    asyncio.run(run_tui_loop(settings, max_seconds=seconds))


@app.command()
def channels(
    rank: bool = typer.Option(
        False, "--rank", help="Measure each channel's lead time against subsequent price action."
    ),
    config: str = typer.Option(None, help="Path to a config YAML."),
    min_tokens: int = typer.Option(
        2, help="Distinct tokens that must point at a channel before it counts as a call channel."
    ),
    horizon_hours: float = typer.Option(6.0, help="How far past a call to look for the peak."),
    days: float = typer.Option(30.0, help="Only consider calls this recent."),
    limit: int = typer.Option(DEFAULT_WATCHLIST_LIMIT, help="Channels the watchlist hands over."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Curate the Telegram call-channel watchlist and rank it by lead time.

    Discovery costs nothing: Dexscreener's `telegram` link is already in the
    store on every launch, and a channel several unrelated tokens point at is a
    call room rather than a token's own. `--rank` then measures what each
    channel's calls were actually worth, which is what lets one be dropped.

    Exits 0 with an empty store on purpose. A channel with nothing measured is
    reported as unmeasured; it is not reported as bad.
    """
    settings = _settings(config, log_level)
    db = Database(settings.path(settings.db_path))
    telegram = settings.collector("telegram")
    curated = [str(c) for c in telegram.extra.get("call_channels", [])]
    since = utcnow() - timedelta(days=days)
    try:
        candidates = discover_candidates(db, min_tokens=min_tokens, since=since)
        ranking: list[Any] = []
        if rank:
            # Rank everything we have heard from, not just what qualifies for
            # the watchlist. A ranking only ever removes, so the wider set costs
            # nothing and answers the question a curator actually has.
            ranking = rank_channels(
                db,
                [
                    *curated,
                    *(c.channel for c in candidates),
                    *collected_channels(db, since),
                    # Channels we tried and got nothing from are not in the
                    # collected set by definition, and they are the ones this
                    # report exists to name. Omitting them would let an evicted
                    # channel disappear silently instead of showing its reason.
                    *read_channels(db, since),
                ],
                horizon_seconds=horizon_hours * 3600.0,
                since=since,
            )
        seeded = seed_call_channels(curated, candidates, ranking, limit=limit)
    finally:
        db.close()

    _print_channel_candidates(candidates, curated)
    if rank:
        _print_channel_ranking(ranking, horizon_hours, set(seeded))
    console.print(
        f"\nwatchlist ({len(seeded)}/{limit}): {', '.join(seeded) or '[yellow]empty[/yellow]'}"
    )
    dropped = [(r.channel, drop_reason(r)) for r in ranking if is_useless(r)]
    for channel, reason in dropped:
        console.print(f"[yellow]dropped on evidence:[/yellow] {channel} — {reason}")


def _print_channel_candidates(candidates: Sequence[Any], curated: Sequence[str]) -> None:
    if not candidates:
        console.print(
            "[yellow]no call channels discovered[/yellow] — no Telegram link in the store is "
            "shared by enough tokens yet. Collect more launches, or curate by hand in "
            "collectors.telegram.extra.call_channels."
        )
        return
    table = Table(title="call-channel candidates")
    table.add_column("channel")
    table.add_column("tokens", justify="right")
    table.add_column("posts", justify="right")
    table.add_column("evidence")
    curated_set = {c for c in (normalize_channel(x) for x in curated) if c is not None}
    for candidate in candidates:
        marker = " [green](curated)[/green]" if candidate.channel in curated_set else ""
        table.add_row(
            f"{candidate.channel}{marker}",
            str(candidate.tokens),
            str(candidate.posts),
            ", ".join(candidate.sources),
        )
    console.print(table)


def _print_channel_ranking(
    ranking: Sequence[Any], horizon_hours: float, seeded: set[str]
) -> None:
    """Lead time per channel, with the sample size beside it, never without."""
    if not ranking:
        console.print("[yellow]nothing to rank[/yellow] — no channel has been read yet.")
        return
    table = Table(title=f"lead time to peak, {horizon_hours:g}h horizon")
    table.add_column("channel")
    table.add_column("calls", justify="right")
    table.add_column("measured", justify="right")
    table.add_column("median lead (s)", justify="right")
    table.add_column("median peak (x)", justify="right")
    table.add_column("led", justify="right")
    table.add_column("empty", justify="right")
    table.add_column("note")
    for entry in ranking:
        marker = _rank_marker(entry, seeded)
        table.add_row(
            f"{entry.channel}{marker}",
            str(entry.calls),
            str(entry.measured),
            "—" if entry.median_lead_seconds is None else f"{entry.median_lead_seconds:.0f}",
            "—" if entry.median_peak_multiple is None else f"{entry.median_peak_multiple:.2f}",
            "—" if entry.led_share is None else f"{entry.led_share:.0%}",
            str(entry.empty_reads) if entry.empty_reads else "—",
            _rank_note(entry),
        )
    console.print(table)
    console.print("[green]*[/green] on the watchlist   [yellow]![/yellow] dropped on evidence")


def _rank_marker(entry: Any, seeded: set[str]) -> str:
    """Which of the three states a channel is in, in one character.

    A channel that is neither seeded nor dropped is simply behind better
    candidates for the six slots, and that is a different fact from having been
    ruled out — the reason for every `!` is printed under the table.
    """
    if entry.channel in seeded:
        return " [green]*[/green]"
    return " [yellow]![/yellow]" if is_useless(entry) else ""


def _rank_note(entry: Any) -> str:
    """The caveats on a channel's record, which are half of what it means."""
    parts = [entry.note] if entry.note else []
    if entry.entry_after_call:
        parts.append(f"{entry.entry_after_call} priced only after the call")
    if entry.already_peaked:
        parts.append(f"{entry.already_peaked} called a top")
    return "; ".join(parts)


@app.command()
def memory(
    config: str = typer.Option(None, help="Path to a config YAML."),
    subject: str = typer.Option(None, help="Filter to one subject (token key, wallet, 'global')."),
    query: str = typer.Option(None, help="Free-text relevance query."),
    limit: int = typer.Option(20, help="How many entries to show."),
) -> None:
    """Show what the system has learned and written to itself."""
    settings = _settings(config, "WARNING")
    store = MemoryStore(settings.path(settings.memory.path))

    console.print(f"stats: {store.stats()}\n")
    entries = store.recall(subject=subject, query=query, limit=limit, apply_decay=False)
    if not entries:
        console.print("[dim]no memories recorded yet[/dim]")
        return

    table = Table(title="agentic memory")
    table.add_column("kind")
    table.add_column("subject", overflow="fold")
    table.add_column("title", overflow="fold")
    table.add_column("conf", justify="right")
    for entry in entries:
        table.add_row(
            entry.kind.value,
            entry.subject[:28],
            entry.title[:64],
            f"{entry.confidence:.2f}",
        )
    console.print(table)


@app.command()
def remember(
    title: str = typer.Argument(..., help="Short title for the note."),
    body: str = typer.Argument(..., help="The note itself."),
    kind: str = typer.Option("heuristic", help="observation|hypothesis|postmortem|heuristic|entity|regime"),
    subject: str = typer.Option("global", help="What this is about."),
    confidence: float = typer.Option(0.6, help="0..1"),
    config: str = typer.Option(None, help="Path to a config YAML."),
) -> None:
    """Write a note into agentic memory by hand.

    Useful for seeding the system with things the operator knows and the bot has
    not yet learned, which then condition future scoring through `meta_alignment`
    and the regime classifier.
    """
    settings = _settings(config, "WARNING")
    store = MemoryStore(settings.path(settings.memory.path))
    entry = store.remember(
        MemoryKind(kind), subject=subject, title=title, body=body, confidence=confidence
    )
    console.print(f"[green]remembered[/green] {entry.id} ({entry.kind.value})")


@app.command()
def weights(
    show: bool = typer.Option(True, help="Print the active weighting."),
    config: str = typer.Option(None, help="Path to a config YAML."),
) -> None:
    """Show the active scoring weights and where they came from."""
    settings = _settings(config, "WARNING")
    scorer = CompositeScorer(build_registry(), None, settings)
    active: Weights = scorer.weights

    console.print(f"version: {active.version}")
    console.print(f"fitted on: {active.fitted_on or '[yellow]never — these are priors, not a model[/yellow]'}")
    console.print(f"sample size: {active.sample_size}\n")

    if not show:
        return

    table = Table(title="family budget")
    table.add_column("family")
    table.add_column("weight", justify="right")
    for family, weight in sorted(active.families.items(), key=lambda kv: kv[1], reverse=True):
        table.add_row(family, f"{weight:.3f}")
    console.print(table)


@app.command()
def recap(
    config: str = typer.Option(None, help="Path to a config YAML."),
    hours: float = typer.Option(1.0, help="Window to summarize."),
    save: bool = typer.Option(True, help="Write the recap to the content directory."),
) -> None:
    """Generate a digest of everything scored in the recent window."""
    settings = _settings(config, "WARNING")
    db = Database(settings.path(settings.db_path))
    memory_store = MemoryStore(settings.path(settings.memory.path))
    registry = build_registry()
    generator = ContentGenerator(settings, registry, memory_store)

    now = utcnow()
    launches = db.launches_between(now - timedelta(hours=hours), now)
    if not launches:
        console.print("[yellow]nothing scored in that window; run a sweep first[/yellow]")
        raise typer.Exit(1)

    scorer = CompositeScorer(registry, None, settings)
    pairs = []
    for launch in launches[:50]:
        values = db.metric_values_as_of(launch.token.key, now)
        if not values:
            continue
        from botsensai.metrics.base import MetricContext

        ctx = MetricContext(token=launch.token, as_of=now, launch=launch)
        pairs.append((launch, scorer.score(ctx, values)))

    if not pairs:
        console.print("[yellow]no scored tokens in that window[/yellow]")
        raise typer.Exit(1)

    piece = generator.recap(pairs, window_label=f"the last {hours:g} hour(s)")
    console.print(piece.body)
    if save:
        path = generator.save(piece)
        console.print(f"\nwrote {path}")


@app.command(name="track-record")
def track_record(
    config: str = typer.Option(None, help="Path to a config YAML."),
    days: float = typer.Option(2.0, help="Window of history to evaluate in days."),
    synthetic: bool = typer.Option(
        False, "--synthetic", help="Force a synthetic run instead of using the store."
    ),
    universe: int = typer.Option(60, help="Synthetic universe size when running synthetic."),
    capital: float = typer.Option(10.0, help="Starting capital in native units (SOL)."),
    out: str = typer.Option(
        "docs/TRACK_RECORD.md", "--out", help="Path to write the track record markdown file."
    ),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Run paper-trading evaluation and publish a rolling performance track record."""
    settings = _settings(config, log_level)
    _banner(settings)

    from botsensai.execution.track_record import build_track_record

    result, markdown = build_track_record(
        settings, days=days, synthetic=synthetic, universe=universe, capital=capital
    )

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")

    summary = result.summary()
    low_ci, high_ci = result.bootstrap_expectancy_ci()

    table = Table(title="paper-trading track record summary")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("evaluated tokens", str(result.evaluated))
    table.add_row("entered positions", str(result.entered))
    table.add_row("completed trades", str(len(result.trades)))
    table.add_row("win rate", f"{summary.get('win_rate', 0.0):.2%}")
    table.add_row("total return", f"{summary.get('total_return', 0.0):+.2%}")
    table.add_row("expectancy", f"{summary.get('expectancy_native', 0.0):+.6f} SOL/trade")
    table.add_row("bootstrap 95% CI", f"[{low_ci:+.6f}, {high_ci:+.6f}] SOL/trade")
    console.print(table)
    console.print(f"\nwrote track record to [green]{out}[/green]")


# --------------------------------------------------------------------------- #
# init & config
# --------------------------------------------------------------------------- #


@app.command()
def init(
    config: str = typer.Option(None, help="Custom path for config YAML."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Run non-interactively with defaults."),
) -> None:
    """Guided onboarding wizard to verify environment, API keys, and configuration."""
    run_onboarding_wizard(Path(config) if config else None, non_interactive=yes)


@app.command(name="wizard")
def wizard(
    config: str = typer.Option(None, help="Custom path for config YAML."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Run non-interactively with defaults."),
) -> None:
    """Guided onboarding wizard to verify environment, API keys, and configuration."""
    run_onboarding_wizard(Path(config) if config else None, non_interactive=yes)


config_app = typer.Typer(help="Inspect and validate configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")


@config_app.command(name="check")
def config_check(
    config: str = typer.Option(None, help="Path to config YAML."),
) -> None:
    """Validate configuration syntax and report unknown keys or typos."""
    ok = validate_config(Path(config) if config else None)
    if not ok:
        raise typer.Exit(1)


@config_app.command(name="show")
def config_show(
    config: str = typer.Option(None, help="Path to config YAML."),
    as_json: bool = typer.Option(False, "--json", help="Emit as JSON instead of YAML."),
) -> None:
    """Display the effective loaded configuration."""
    show_config(Path(config) if config else None, as_json=as_json)


# --------------------------------------------------------------------------- #
# run (unified supervisor)
# --------------------------------------------------------------------------- #


@app.command()
def run(
    config: str = typer.Option(None, help="Path to a config YAML."),
    hours: float = typer.Option(None, "--hours", help="How long to keep running in hours (default: run forever)."),
    stream: bool = typer.Option(True, "--stream/--no-stream", help="Enable pump.fun WebSocket mint stream."),
    sweep: bool = typer.Option(True, "--sweep/--no-sweep", help="Enable periodic REST discovery and scoring sweeps."),
    autolabel: bool = typer.Option(True, "--autolabel/--no-autolabel", help="Enable background outcome labelling."),
    interval: float = typer.Option(60.0, help="Seconds between discovery sweeps."),
    limit: int = typer.Option(60, help="Launches to pull per sweep."),
    candidates: int = typer.Option(20, help="Survivors to enrich and score per sweep."),
    log_level: str = typer.Option("INFO", help="Log level."),
) -> None:
    """Run the unified multi-task supervisor (Streamer, Sweeper, Labeller, Sentinel)."""
    from botsensai.util.environment import auto_reexec_in_virtualenv

    auto_reexec_in_virtualenv()
    settings = _settings(config, log_level)
    if settings.trading_mode is TradingMode.LIVE:
        console.print("[red]refusing to run in live mode; this build cannot trade[/red]")
        raise typer.Exit(2)
    _banner(settings)

    supervisor = Supervisor(
        settings=settings,
        enable_stream=stream,
        enable_sweep=sweep,
        enable_labeller=autolabel,
        sweep_interval=interval,
        discovery_limit=limit,
        candidates_limit=candidates,
    )

    max_seconds = hours * 3600.0 if hours else None
    try:
        asyncio.run(supervisor.run(max_seconds=max_seconds))
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted; shutting down supervisor cleanly...[/yellow]")
    finally:
        supervisor.print_summary()


venv_app = typer.Typer(help="Inspect and manage local virtual environment.", no_args_is_help=True)
app.add_typer(venv_app, name="venv")


@venv_app.command(name="info")
def venv_info() -> None:
    """Display current Python interpreter and virtualenv status."""
    from botsensai.util.environment import get_virtualenv_info

    info = get_virtualenv_info()
    table = Table(title="Python Environment Info", box=None)
    table.add_column("Property", style="bold cyan")
    table.add_column("Value")
    table.add_row("Virtualenv Active", "[green]Yes[/green]" if info["is_virtual"] else "[yellow]No (Global)[/yellow]")
    table.add_row("Environment Type", info["env_type"])
    table.add_row("Prefix Path", info["current_prefix"])
    table.add_row("Executable", info["executable"])
    table.add_row("Local .venv Exists", "[green]Yes[/green]" if info["local_venv_exists"] else "[dim]No[/dim]")
    table.add_row("Local .venv Path", info["local_venv_path"])
    console.print(table)


@venv_app.command(name="create")
def venv_create(
    dest: str = typer.Option(None, "--dest", "-d", help="Destination path for .venv directory."),
) -> None:
    """Create a new standard library virtualenv in the repository root."""
    from pathlib import Path

    from botsensai.util.environment import create_local_virtualenv

    target = create_local_virtualenv(Path(dest) if dest else None)
    console.print(f"[green]✓ Created local virtual environment at:[/green] [bold]{target}[/bold]")
    console.print(f"\nActivate with:\n  [bold cyan]source {target}/bin/activate[/bold cyan]\n")


# --------------------------------------------------------------------------- #
# inspect
# --------------------------------------------------------------------------- #


@app.command()
def inspect(
    query: str = typer.Argument(..., help="Solana mint address, pump.fun/Dexscreener URL, or $TICKER."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    enrich: bool = typer.Option(True, "--enrich/--no-enrich", help="Enrich on-chain and social signals."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Inspect and score any token from a URL, ticker ($SYMBOL), or mint address."""
    settings = _settings(config, log_level)
    asyncio.run(inspect_token(query, settings, deep_enrich=enrich))


@app.command()
def graph(
    mint: str = typer.Argument(..., help="Solana mint address or token key to visualize."),
    out: str = typer.Option(None, "--out", help="Write HTML graph to this path."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the generated graph in browser."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Generate an interactive D3.js on-chain topology and funder graph."""
    from botsensai.dashboard.graph import build_topology_graph, render_topology_html

    settings = _settings(config, log_level)
    db = Database(settings.path(settings.db_path))
    token_key = f"solana:{mint}" if not mint.startswith("solana:") else mint
    try:
        topo_graph = build_topology_graph(token_key, db)
    finally:
        db.close()

    html_content = render_topology_html(topo_graph)
    target_path = Path(out) if out else Path(f"data/topology_{mint[:8]}.html")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(html_content, encoding="utf-8")

    console.print(
        f"[green]✓ Generated topology graph:[/green] [bold]{target_path}[/bold] "
        f"({len(topo_graph.nodes)} wallets, {len(topo_graph.links)} links)"
    )

    if open_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(f"file://{target_path.resolve()}")


@app.command()
def compare(
    token1: str = typer.Argument(..., help="First token query ($TICKER, mint, or URL)."),
    token2: str = typer.Argument(..., help="Second token query ($TICKER, mint, or URL)."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Side-by-side comparative scoring audit of two tokens."""
    from botsensai.autopsy import compare_tokens

    settings = _settings(config, log_level)
    asyncio.run(compare_tokens(token1, token2, settings))


@app.command()
def autopsy(
    token: str = typer.Argument(..., help="Token query ($TICKER, mint, or URL) to autopsy."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Reconstruct chronological post-mortem event timeline for a token."""
    from botsensai.autopsy import autopsy_token

    settings = _settings(config, log_level)
    asyncio.run(autopsy_token(token, settings))


@app.command()
def gallery(
    mint: str = typer.Argument(..., help="Solana mint address or token key to inspect memes."),
    out: str = typer.Option(None, "--out", help="Write HTML gallery to this path."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the generated gallery in browser."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Generate a visual meme lineage, pHash cluster tree, and originality gallery."""
    from botsensai.media.gallery import build_meme_lineage, render_meme_gallery_html

    settings = _settings(config, log_level)
    db = Database(settings.path(settings.db_path))
    token_key = f"solana:{mint}" if not mint.startswith("solana:") else mint
    try:
        report = build_meme_lineage(token_key, db)
    finally:
        db.close()

    html_content = render_meme_gallery_html(report)
    target_path = Path(out) if out else Path(f"data/gallery_{mint[:8]}.html")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(html_content, encoding="utf-8")

    console.print(
        f"[green]✓ Generated meme lineage gallery:[/green] [bold]{target_path}[/bold] "
        f"({report.total_images} images, {report.distinct_clusters} clusters, "
        f"originality: {report.originality_index:.1%})"
    )

    if open_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(f"file://{target_path.resolve()}")


@app.command()
def ask(
    token: str = typer.Argument(..., help="Token query ($TICKER, mint, or URL)."),
    question: str = typer.Argument(..., help="Question to ask the AI copilot."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Ask natural language questions to the AI Copilot with evidence grounding."""
    from botsensai.copilot import run_copilot_cli

    settings = _settings(config, log_level)
    asyncio.run(run_copilot_cli(token, question, settings))


@app.command()
def share(
    token: str = typer.Argument(..., help="Token query ($TICKER, mint, or URL) to generate share card for."),
    out: str = typer.Option(None, "--out", help="Path to write SVG card."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open generated card in browser."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Generate a high-resolution 1200x675 SVG social share card."""
    from botsensai.media.fal_cards import generate_social_card

    settings = _settings(config, log_level)
    target = asyncio.run(generate_social_card(token, settings, out_path=out))
    console.print(f"[green]✓ Generated social share card:[/green] [bold]{target}[/bold]")

    if open_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(f"file://{target.resolve()}")


@app.command()
def sandbox(
    param: str = typer.Option("entry_threshold", "--param", help="Strategy parameter to stress-test."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Run interactive 'what-if' strategy parameter sensitivity analysis."""
    from botsensai.sandbox import run_sandbox_cli

    settings = _settings(config, log_level)
    asyncio.run(run_sandbox_cli(param, settings))


@app.command()
def playground(
    interactive: bool = typer.Option(True, "--interactive/--auto", help="Run in interactive prompt mode."),
) -> None:
    """Run interactive scenario training playground against adversarial launch cases."""
    from botsensai.playground import run_playground

    asyncio.run(run_playground(interactive=interactive))


@app.command(name="smart-money")
def smart_money_cmd(
    min_trades: int = typer.Option(2, "--min-trades", help="Minimum trades to qualify."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Discover and rank smart money wallets with high historical win rates."""
    from botsensai.smart_money import run_smart_money_cli

    settings = _settings(config, log_level)
    asyncio.run(run_smart_money_cli(settings, min_trades=min_trades))


@app.command()
def replay(
    mint: str = typer.Argument(..., help="Token mint or query to replay."),
    step: float = typer.Option(10.0, "--step", help="Seconds per evaluation step."),
    animate: bool = typer.Option(False, "--animate", help="Animate the replay in terminal."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Replay historical telemetry and signal evaluations second-by-second."""
    from botsensai.replay import run_replay_cli

    settings = _settings(config, log_level)
    asyncio.run(run_replay_cli(mint, settings, step_seconds=step, animate=animate))


@app.command()
def digest(
    out: str = typer.Option(None, "--out", help="Path to write markdown digest."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Generate and export executive market intelligence and alpha digest."""
    from botsensai.digest import export_market_digest

    settings = _settings(config, log_level)
    target = export_market_digest(settings, out_path=out)
    console.print(f"[green]✓ Generated market intelligence digest:[/green] [bold]{target}[/bold]")


@app.command()
def demo(
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Run without terminal prompts."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Run an instant zero-config end-to-end simulation of Botsensai 2.0."""
    from botsensai.demo import run_demo_simulation

    settings = _settings(config, log_level)
    asyncio.run(run_demo_simulation(settings, non_interactive=non_interactive))


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Host interface to bind."),
    port: int = typer.Option(8001, "--port", help="Port to listen on."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("INFO", help="Log level."),
) -> None:
    """Launch the headless REST and WebSocket API server with OpenAPI docs."""
    import uvicorn

    from botsensai.api import create_headless_api_app

    settings = _settings(config, log_level)
    api_app = create_headless_api_app(settings)
    console.print(f"\n[green]🚀 Botsensai API Server starting at[/green] [bold cyan]http://{host}:{port}/docs[/bold cyan]\n")
    uvicorn.run(api_app, host=host, port=port, log_level=log_level.lower())


@app.command()
def corpus(
    action: str = typer.Argument("export", help="Action: export | import"),
    file_path: str = typer.Option(None, "--file", help="Bundle archive path."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Manage training corpus snapshot archives (bundle export and import)."""
    from botsensai.corpus_sync import export_corpus_bundle, import_corpus_bundle

    settings = _settings(config, log_level)
    if action == "export":
        dest = export_corpus_bundle(settings, out_path=file_path)
        console.print(f"[green]✓ Exported corpus archive to[/green] [bold]{dest}[/bold]")
    elif action == "import" and file_path:
        manifest = import_corpus_bundle(settings, bundle_path=file_path)
        console.print(f"[green]✓ Imported corpus archive from[/green] [bold]{file_path}[/bold] (weights: {manifest.get('weights_version')})")
    else:
        console.print("[yellow]Specify a valid action: 'export' or 'import --file <path>'[/yellow]")


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #


@app.command()
def export(
    table: str = typer.Argument(..., help="Table to export: launches | outcomes | market_snapshots | paper_orders"),
    format: str = typer.Option("csv", "--format", help="Export format: csv | json | jsonl | parquet"),
    out: str = typer.Option(None, "--out", help="Output file path."),
    limit: int = typer.Option(None, help="Limit number of rows exported."),
    config: str = typer.Option(None, help="Path to a config YAML."),
    log_level: str = typer.Option("WARNING", help="Log level."),
) -> None:
    """Export SQLite store records to CSV, JSON, or Parquet."""
    settings = _settings(config, log_level)
    try:
        dest = export_data(settings, table=table, output_format=format, out_file=out, limit=limit)
        console.print(f"[green]✓ Exported table '{table}' to[/green] [bold]{dest}[/bold]")
    except Exception as exc:
        console.print(f"[red]Export failed:[/red] {exc}")
        raise typer.Exit(1) from None


def main() -> None:

    app()


if __name__ == "__main__":
    main()
