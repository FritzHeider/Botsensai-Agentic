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
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from botsensai.backtest.engine import Backtester
from botsensai.config import TradingMode, get_settings, load_settings
from botsensai.media import ContentGenerator
from botsensai.memory.store import MemoryStore
from botsensai.metrics import build_registry, metric_catalogue
from botsensai.models import MemoryKind, utcnow
from botsensai.pipeline import Pipeline
from botsensai.scoring import CompositeScorer, Weights
from botsensai.store.db import Database
from botsensai.util.logging import configure_logging
from botsensai.util.synthetic import generate_cohort

app = typer.Typer(
    name="botsensai",
    help="Agentic memecoin recon, scoring, backtesting, paper trading and content generation.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _settings(config: str | None = None, log_level: str = "INFO") -> Any:
    settings = load_settings(config) if config else get_settings()
    configure_logging(log_level, settings.log_json)
    return settings


def _banner(settings: Any) -> None:
    mode = settings.trading_mode.value
    colour = {"backtest": "cyan", "paper": "green", "live": "red bold"}.get(mode, "white")
    console.print(
        Panel(
            f"[{colour}]mode: {mode}[/{colour}]   metrics: {len(build_registry())}   "
            f"db: {settings.db_path}",
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

    if not synthetic:
        db = Database(settings.path(settings.db_path))
        tapes = Backtester.tapes_from_database(db, start, end)
        usable = [t for t in tapes if len(t.snapshots) >= 3]
        if len(usable) < 10:
            console.print(
                f"[yellow]store holds only {len(usable)} replayable tokens in the last "
                f"{days} days — falling back to a synthetic run. Collect for a while with "
                f"'botsensai sweep --repeat 0' to build a real corpus.[/yellow]"
            )
            synthetic = True
        else:
            tapes = usable

    if synthetic:
        cohort = generate_cohort(n=universe, seed=settings.seed)
        tapes = Backtester.tapes_from_synthetic(cohort)

    result = tester.run(tapes, starting_native=capital, synthetic=synthetic)
    summary = result.summary()

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

    if out:
        payload = {
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
        Path(out).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        console.print(f"\nwrote {out}")


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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
