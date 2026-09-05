"""Interactive scenario training playground.

Presents traders with real-world curated scenarios (honeypots, slow rugs, sniper bundles,
viral cults) to train intuition and compare decisions against Botsensai's 34 adversarial signals.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

console = Console()


@dataclass
class Scenario:
    scenario_id: str
    title: str
    ticker: str
    narrative: str
    signals: dict[str, float]
    vetoes_triggered: list[str]
    actual_outcome: str  # "rugged" | "graduated_10x" | "slow_bleed"
    correct_decision: str  # "veto" | "enter" | "pass"
    explanation: str


CURATED_SCENARIOS = [
    Scenario(
        scenario_id="SCEN-01",
        title="Deployer Slow Rug via Funder Hub",
        ticker="PEPEWIF",
        narrative="Fast-rising meme with 1,200 holders, but top 8 holders share the same funding source wallet.",
        signals={"funder_dispersion": 0.12, "insider_overhang": 0.65, "social_authenticity": 0.40},
        vetoes_triggered=["insider_supply_overhang", "funder_graph_concentration"],
        actual_outcome="rugged",
        correct_decision="veto",
        explanation="The top 8 holders were secretly sybils funded by the deployer. Dev dumped 45 SOL at minute 12.",
    ),
    Scenario(
        scenario_id="SCEN-02",
        title="Genuine Viral Cult Meme",
        ticker="GIGAWHALE",
        narrative="Spontaneous organic artwork appearing across 50+ distinct accounts with unique perceptual hashes.",
        signals={"funder_dispersion": 0.94, "remix_originality": 0.88, "insider_overhang": 0.08},
        vetoes_triggered=[],
        actual_outcome="graduated_10x",
        correct_decision="enter",
        explanation="Decentralized community distribution, zero bundle concentration, and true viral derivative remixing.",
    ),
    Scenario(
        scenario_id="SCEN-03",
        title="Sniper Bundle Sandwich",
        ticker="SOLCAT",
        narrative="First block contained 14 buy transactions from fresh unfunded keypairs.",
        signals={"bundle_participation": 0.82, "dev_holding": 0.02, "funder_dispersion": 0.20},
        vetoes_triggered=["bundle_participation_ratio"],
        actual_outcome="rugged",
        correct_decision="veto",
        explanation="Jito bundle snipers owned 82% of supply on mint. Dumped instantly as retail bought.",
    ),
]


def evaluate_decision(scenario: Scenario, user_choice: str) -> tuple[bool, str]:
    """Grade trader decision against optimal adversarial action."""
    choice = user_choice.lower().strip()
    norm_choice = "enter" if choice in ("e", "enter", "buy") else "veto" if choice in ("v", "veto", "reject") else "pass"
    is_correct = norm_choice == scenario.correct_decision

    grade_msg = (
        f"[green bold]EXCELLENT DECISION![/green bold] You correctly chose to {norm_choice.upper()}."
        if is_correct
        else f"[red bold]SUB-OPTIMAL MOVE![/red bold] You chose {norm_choice.upper()}, but optimal was {scenario.correct_decision.upper()}."
    )
    return is_correct, f"{grade_msg}\n\n[bold]Forensic Reality:[/bold] {scenario.explanation}"


def play_scenario(scenario: Scenario, interactive: bool = True) -> tuple[bool, str]:
    """Run an individual scenario round."""
    console.print(Panel(
        f"[bold]{scenario.scenario_id}: {scenario.title}[/bold] (${scenario.ticker})\n\n"
        f"[dim]{scenario.narrative}[/dim]\n\n"
        f"[bold underline]Observed Signals:[/bold underline]\n" +
        "\n".join(f"  • {k}: [cyan]{v:.2f}[/cyan]" for k, v in scenario.signals.items()),
        title="[bold yellow]Scenario Challenge[/bold yellow]",
        expand=False,
    ))

    choice = Prompt.ask("Your Decision: [E]nter, [V]eto, or [P]ass?", choices=["e", "v", "p", "enter", "veto", "pass"], default="p") if interactive else "v"
    is_correct, feedback = evaluate_decision(scenario, choice)
    console.print(Panel(feedback, title="[bold]Outcome & Evaluation[/bold]", expand=False))
    return is_correct, feedback


async def run_playground(interactive: bool = True) -> int:
    """Run full interactive training scenario loop."""
    console.print("\n[bold cyan]🎮 Welcome to the Botsensai Adversarial Training Playground[/bold cyan]\n")
    correct_count = 0

    for sc in CURATED_SCENARIOS:
        is_corr, _ = play_scenario(sc, interactive=interactive)
        if is_corr:
            correct_count += 1

    console.print(f"\n[bold]Playground Completed![/bold] Score: {correct_count}/{len(CURATED_SCENARIOS)}\n")
    return correct_count


__all__ = ["CURATED_SCENARIOS", "Scenario", "evaluate_decision", "play_scenario", "run_playground"]
