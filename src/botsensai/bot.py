"""Interactive two-way Discord & Telegram Bot handler.

Enables chat commands (/score, /regime, /top, /autopsy) and interactive action
buttons for deep dossier inspection and paper trading signals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from botsensai.config import Settings
from botsensai.inspector import inspect_token
from botsensai.store.db import Database


@dataclass
class BotCommandResult:
    command: str
    target: str | None
    title: str
    body: str
    fields: list[dict[str, Any]]
    buttons: list[dict[str, str]]


async def handle_bot_command(
    command_text: str, settings: Settings, db: Database | None = None
) -> BotCommandResult:
    """Parse and execute a Telegram/Discord slash command."""
    parts = command_text.strip().split()
    cmd = parts[0].lower() if parts else "/help"
    arg = parts[1] if len(parts) > 1 else None

    active_db = db or Database(settings.path(settings.db_path))
    should_close = db is None

    try:
        if cmd in ("/score", "/inspect") and arg:
            launch, score = await inspect_token(arg, settings, deep_enrich=True)
            if not launch or not score:
                return BotCommandResult(
                    command=cmd,
                    target=arg,
                    title="Token Not Found",
                    body=f"Could not locate or score '{arg}'.",
                    fields=[],
                    buttons=[],
                )
            fields = [
                {"name": "Score", "value": f"{score.composite:.3f}", "inline": True},
                {"name": "Coverage", "value": f"{score.coverage:.0%}", "inline": True},
                {"name": "Vetoes", "value": ", ".join(score.vetoes) if score.vetoes else "None (Passed)", "inline": False},
            ]
            buttons = [
                {"label": "🔍 Deep Dossier", "url": f"https://dexscreener.com/solana/{launch.token.mint}"},
                {"label": "📊 View Radar", "callback_data": f"radar:{launch.token.mint}"},
            ]
            return BotCommandResult(
                command=cmd,
                target=arg,
                title=f"${launch.token.symbol or '?'} Score Analysis",
                body=score.explanation or f"Scored {score.composite:.3f} with {score.coverage:.0%} coverage.",
                fields=fields,
                buttons=buttons,
            )

        if cmd == "/regime":
            counts = active_db.counts()
            fields = [
                {"name": "Trading Mode", "value": settings.trading_mode.value.upper(), "inline": True},
                {"name": "Labelled Outcomes", "value": str(counts.get("outcomes", 0)), "inline": True},
                {"name": "Weights Version", "value": settings.scoring.weights_version, "inline": True},
            ]
            return BotCommandResult(
                command=cmd,
                target=None,
                title="Market Regime & Intelligence",
                body=f"System operating in {settings.trading_mode.value} mode.",
                fields=fields,
                buttons=[],
            )

        if cmd == "/top":
            scores = active_db.recent_scores(limit=5)
            fields = [
                {
                    "name": f"${r['token_key'].split(':')[-1][:8]}",
                    "value": f"Score: {r['composite']:.3f} │ Cov: {int(r['coverage'] * 100)}%",
                    "inline": False,
                }
                for r in scores
            ]
            return BotCommandResult(
                command=cmd,
                target=None,
                title="Top Scored Candidates",
                body="Recent top token conviction scores:",
                fields=fields,
                buttons=[],
            )

        # Default help
        return BotCommandResult(
            command="/help",
            target=None,
            title="Botsensai 2.0 Bot Commands",
            body=(
                "Available commands:\n"
                "• `/score <mint_or_ticker>` - Score and inspect token\n"
                "• `/regime` - Current market regime and weights\n"
                "• `/top` - Top candidate tokens\n"
                "• `/help` - Show command menu"
            ),
            fields=[],
            buttons=[],
        )
    finally:
        if should_close:
            active_db.close()


__all__ = ["BotCommandResult", "handle_bot_command"]
