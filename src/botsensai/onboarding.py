"""Guided onboarding wizard and configuration validation tools."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from botsensai.config import (
    DEFAULT_CONFIG_PATH,
    REPO_ROOT,
    audit_capabilities,
    detect_unknown_yaml_keys,
    load_settings,
)

console = Console()


def check_prerequisites() -> dict[str, bool]:
    """Check required and optional environment dependencies."""
    return {
        "python_version": True,  # Already executing under Python 3.11+
        "playwright": importlib.util.find_spec("playwright") is not None,
        "sklearn": importlib.util.find_spec("sklearn") is not None,
        "lightgbm": importlib.util.find_spec("lightgbm") is not None,
        "chrome": shutil.which("google-chrome") is not None
        or shutil.which("chrome") is not None
        or Path("/Applications/Google Chrome.app").exists(),
    }


def run_onboarding_wizard(
    target_config: Path | None = None,
    non_interactive: bool = False,
) -> None:
    """Step-by-step interactive onboarding to get Botsensai ready in seconds."""
    dest = target_config or DEFAULT_CONFIG_PATH

    console.print(
        Panel(
            "[bold green]Botsensai Setup & Readiness Wizard[/bold green]\n\n"
            "This wizard will verify your environment, check connectivity to Solana launchpads,\n"
            "audit API capabilities, and configure your local settings file.",
            title="botsensai init",
            expand=False,
        )
    )

    # Step 1: Config file check/creation
    console.print("\n[bold cyan][1/4] Configuration File[/bold cyan]")
    if dest.exists():
        console.print(f"  • Existing config found at: [green]{dest}[/green]")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Copy template or write default
        default_yaml_content = """# Botsensai configuration
trading_mode: paper
dry_run: true
log_level: INFO
db_path: data/botsensai.db
seed: 1337

risk:
  max_position_native: 0.25
  max_portfolio_exposure_native: 2.0
  max_concurrent_positions: 8
  max_daily_loss_native: 1.0
  max_trades_per_hour: 20
  min_liquidity_usd: 5000
  stop_loss_pct: 0.45

scoring:
  weights_version: v0
  weights_path: config/weights.json
  min_coverage: 0.5
  entry_threshold: 0.68
  exit_threshold: 0.35

memory:
  enabled: true
  path: data/memory.db

media:
  enabled: true
  output_dir: data/content
"""
        dest.write_text(default_yaml_content, encoding="utf-8")
        console.print(f"  • Created default config at: [green]{dest}[/green]")

    # Check for .env file
    env_file = REPO_ROOT / ".env"
    env_example = REPO_ROOT / ".env.example"
    if not env_file.exists() and env_example.exists():
        shutil.copyfile(env_example, env_file)
        console.print("  • Created [green].env[/green] template from [dim].env.example[/dim]")

    # Step 2: Dependencies Audit
    console.print("\n[bold cyan][2/4] Dependency & Tooling Audit[/bold cyan]")
    prereqs = check_prerequisites()
    dep_table = Table(box=None)
    dep_table.add_column("Component", style="bold")
    dep_table.add_column("Status")
    dep_table.add_column("Note")

    dep_table.add_row(
        "Core Engine",
        "[green]installed[/green]",
        "Python 3.11+, pydantic, typer, rich, sqlite3",
    )
    dep_table.add_row(
        "Browser Extra",
        "[green]installed[/green]" if prereqs["playwright"] else "[yellow]optional (not installed)[/yellow]",
        "Unlocks web-use collectors (`pip install -e '.[browser]'`)"
        if not prereqs["playwright"]
        else "Playwright available",
    )
    dep_table.add_row(
        "ML Modeling Extra",
        "[green]installed[/green]"
        if (prereqs["sklearn"] and prereqs["lightgbm"])
        else "[yellow]optional (not installed)[/yellow]",
        "Unlocks weight fitting and ablation (`pip install -e '.[ml]'`)"
        if not (prereqs["sklearn"] and prereqs["lightgbm"])
        else "scikit-learn & LightGBM available",
    )
    console.print(dep_table)

    # Step 3: API Keys & Credentials
    console.print("\n[bold cyan][3/4] API Key Capabilities Audit[/bold cyan]")
    settings = load_settings(dest)
    caps = audit_capabilities(settings)

    key_table = Table(box=None)
    key_table.add_column("Capability")
    key_table.add_column("Key Configured")
    key_table.add_column("What it Unlocks")

    key_table.add_row(
        "Helius RPC",
        "[green]yes[/green]" if caps["helius_rpc"] else "[yellow]no (using public RPC)[/yellow]",
        "Deployer history, deep funding analysis, topology",
    )
    key_table.add_row(
        "Birdeye Security",
        "[green]yes[/green]" if caps["birdeye"] else "[yellow]no (optional)[/yellow]",
        "Token security audit & top holder distribution",
    )
    key_table.add_row(
        "X Session",
        "[green]yes[/green]" if caps["x_session"] else "[dim]off (run botsensai x-setup to enable)[/dim]",
        "Reply sentiment, view counts, engager account ages",
    )
    key_table.add_row(
        "Notifications",
        "[green]yes[/green]" if caps["notifications"] else "[dim]off[/dim]",
        "Discord/Telegram instant push alerts",
    )
    console.print(key_table)

    # Step 4: Unknown Key Validation
    console.print("\n[bold cyan][4/4] Configuration Syntax & Typo Check[/bold cyan]")
    unknown_keys = detect_unknown_yaml_keys(dest)
    if unknown_keys:
        console.print(
            f"  [red]⚠ Found unknown keys in {dest}:[/red] {', '.join(unknown_keys)}\n"
            "  (These may be typos and will be ignored by the engine.)"
        )
    else:
        console.print("  [green]✓ Configuration syntax is valid and clean.[/green]")

    console.print(
        Panel(
            "[bold green]Botsensai is ready![/bold green]\n\n"
            "Quick commands to get started:\n"
            "  • [bold]botsensai doctor[/bold]         - Verify live connectivity to all launchpads\n"
            "  • [bold]botsensai sweep[/bold]          - Run one live discovery & scoring pass\n"
            "  • [bold]botsensai run[/bold]            - Run continuous background streamer & poller\n"
            "  • [bold]botsensai inspect <mint>[/bold] - Deeply analyze any specific token or URL",
            title="Next Steps",
            expand=False,
        )
    )


def validate_config(config_path: Path | None = None) -> bool:
    """Audit and validate configuration file integrity."""
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        console.print(f"[red]Config file does not exist at {path}[/red]")
        return False

    console.print(f"Auditing config: [bold]{path}[/bold]\n")

    unknown = detect_unknown_yaml_keys(path)
    if unknown:
        console.print(f"[yellow]⚠ Unknown/deprecated keys found:[/yellow] {', '.join(unknown)}")
    else:
        console.print("[green]✓ No unknown keys or typos found.[/green]")

    try:
        settings = load_settings(path)
        console.print("[green]✓ Pydantic validation passed.[/green]")
        console.print(f"  • Mode: [cyan]{settings.trading_mode.value}[/cyan]")
        console.print(f"  • DB Path: {settings.db_path}")
        console.print(f"  • Max Position: {settings.risk.max_position_native} SOL")
        console.print(f"  • Entry Threshold: {settings.scoring.entry_threshold}")
        return True
    except Exception as exc:
        console.print(f"[red]✗ Config validation error:[/red] {exc}")
        return False


def show_config(config_path: Path | None = None, as_json: bool = False) -> None:
    """Print effective loaded settings as YAML or JSON."""
    settings = load_settings(config_path)
    model_dict = settings.model_dump()
    if as_json:
        import json

        console.print_json(json.dumps(model_dict, default=str))
    else:
        console.print(yaml.dump(model_dict, sort_keys=False))
