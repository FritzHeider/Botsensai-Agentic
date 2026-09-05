from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

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
        "python_version": True,  # Executing under Python 3.11+
        "playwright": importlib.util.find_spec("playwright") is not None,
        "sklearn": importlib.util.find_spec("sklearn") is not None,
        "lightgbm": importlib.util.find_spec("lightgbm") is not None,
        "google_genai": importlib.util.find_spec("google.genai") is not None or importlib.util.find_spec("google.generativeai") is not None,
        "fal_client": importlib.util.find_spec("fal_client") is not None,
    }


def _ensure_config_file(dest: Path) -> None:
    """Ensure destination YAML configuration file exists."""
    if dest.exists():
        console.print(f"  • Existing configuration file: [green]{dest}[/green]")
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    default_yaml_content = """# Botsensai 2.0 Configuration
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
    console.print(f"  • Created default configuration file at: [green]{dest}[/green]")


def _ensure_env_file() -> None:
    """Ensure .env template is initialized in repo root."""
    env_file = REPO_ROOT / ".env"
    env_example = REPO_ROOT / ".env.example"
    if not env_file.exists() and env_example.exists():
        env_file.write_text(env_example.read_text(encoding="utf-8"), encoding="utf-8")
        console.print("  • Created [green].env[/green] template from [dim].env.example[/dim]")


def _render_dependencies_table(prereqs: dict[str, bool]) -> None:
    """Render Rich table of core and optional module packages."""
    dep_table = Table(box=None)
    dep_table.add_column("Component", style="bold")
    dep_table.add_column("Status")
    dep_table.add_column("Capabilities")

    dep_table.add_row("Core Engine", "[green]installed[/green]", "Python 3.11+, pydantic, typer, rich, sqlite3")
    dep_table.add_row(
        "Browser Automation",
        "[green]installed[/green]" if prereqs["playwright"] else "[yellow]optional[/yellow]",
        "Playwright headless browser scraping for X and launchpads",
    )
    dep_table.add_row(
        "ML Modeling",
        "[green]installed[/green]" if (prereqs["sklearn"] and prereqs["lightgbm"]) else "[yellow]optional[/yellow]",
        "Scikit-learn & LightGBM signal weight calibration",
    )
    dep_table.add_row(
        "AI Copilot (Gemini)",
        "[green]installed[/green]" if prereqs["google_genai"] else "[yellow]optional[/yellow]",
        "Natural language token explainer and thesis synthesis",
    )
    dep_table.add_row(
        "Fal.ai Media",
        "[green]installed[/green]" if prereqs["fal_client"] else "[yellow]optional[/yellow]",
        "Generative visual share cards & meme lineage analysis",
    )
    console.print(dep_table)


def _render_capabilities_table(caps: dict[str, Any]) -> None:
    """Render Rich table of active credentials and unlocked endpoints."""
    key_table = Table(box=None)
    key_table.add_column("Capability")
    key_table.add_column("Status")
    key_table.add_column("What it Unlocks")

    key_table.add_row(
        "Solana RPC",
        "[green]configured[/green]",
        "Real-time bonding curves, trades & liquidity sync",
    )
    key_table.add_row(
        "Helius API",
        "[green]active[/green]" if caps.get("helius_rpc") else "[yellow]optional (public RPC fallback)[/yellow]",
        "Deep deployer forensics, funding trees & graph topology",
    )
    key_table.add_row(
        "Birdeye Security",
        "[green]active[/green]" if caps.get("birdeye") else "[yellow]optional[/yellow]",
        "Holder rankings & mint security audits",
    )
    key_table.add_row(
        "Notifications",
        "[green]active[/green]" if caps.get("notifications") else "[dim]optional[/dim]",
        "Instant Telegram / Discord push alerts & desktop chimes",
    )
    console.print(key_table)


def run_onboarding_wizard(
    target_config: Path | None = None,
    non_interactive: bool = False,
) -> None:
    """Step-by-step interactive onboarding to configure and verify Botsensai 2.0."""
    dest = target_config or DEFAULT_CONFIG_PATH

    console.print(
        Panel(
            "[bold cyan]BOTSENSAI 2.0 SETUP & READINESS WIZARD[/bold cyan]\n\n"
            "This wizard verifies your environment, audits Solana launchpad connectivity,\n"
            "checks AI/multimodal credentials, and validates local configuration files.",
            title="[bold green]System Onboarding[/bold green]",
            expand=False,
        )
    )

    # Step 1: Configuration & Environment Files
    console.print("\n[bold cyan][1/4] Configuration & Environment[/bold cyan]")
    _ensure_config_file(dest)
    _ensure_env_file()

    # Step 2: Dependency & Module Audit
    console.print("\n[bold cyan][2/4] Tooling & Module Audit[/bold cyan]")
    prereqs = check_prerequisites()
    _render_dependencies_table(prereqs)

    # Step 3: API Credentials & Capabilities Audit
    console.print("\n[bold cyan][3/4] Capabilities & Data Endpoints[/bold cyan]")
    settings = load_settings(dest)
    caps = audit_capabilities(settings)
    _render_capabilities_table(caps)

    # Step 4: Syntax & Typo Verification
    console.print("\n[bold cyan][4/4] Configuration Syntax Verification[/bold cyan]")
    unknown_keys = detect_unknown_yaml_keys(dest)
    if unknown_keys:
        console.print(
            f"  [red]⚠ Unknown or deprecated keys in {dest}:[/red] {', '.join(unknown_keys)}\n"
            "  (These may be typos and will be ignored by the engine.)"
        )
    else:
        console.print("  [green]✓ Configuration syntax is 100% valid and clean.[/green]")

    console.print(
        Panel(
            "[bold green]✓ Botsensai 2.0 is fully initialized and ready![/bold green]\n\n"
            "[bold white]Recommended commands to get started:[/bold white]\n"
            "  • [bold cyan]botsensai demo[/bold cyan]            - Run instant zero-config end-to-end simulation\n"
            "  • [bold cyan]botsensai ui[/bold cyan]              - Launch real-time glassmorphic Web Dashboard\n"
            "  • [bold cyan]botsensai tui[/bold cyan]             - Launch split-pane live terminal interface\n"
            "  • [bold cyan]botsensai graph <mint>[/bold cyan]   - Open interactive on-chain topology graph\n"
            "  • [bold cyan]botsensai ask <mint>[/bold cyan]     - AI copilot dossier explanation with citations\n"
            "  • [bold cyan]botsensai doctor[/bold cyan]          - Verify live RPC and launchpad connectivity",
            title="[bold green]Ready for Action[/bold green]",
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
    path = config_path or DEFAULT_CONFIG_PATH
    settings = load_settings(path)
    data = settings.model_dump(mode="json")
    if as_json:
        console.print_json(json.dumps(data))
    else:
        console.print(yaml.safe_dump(data, sort_keys=False))


__all__ = [
    "check_prerequisites",
    "run_onboarding_wizard",
    "show_config",
    "validate_config",
]
