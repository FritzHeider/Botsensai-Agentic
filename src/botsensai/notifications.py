"""Notification dispatchers for Discord, Telegram, and generic Webhooks."""

from __future__ import annotations

import asyncio
import contextlib
import platform
import subprocess
from dataclasses import dataclass
from typing import Any

import httpx

from botsensai.config import Settings
from botsensai.util.logging import get_logger

log = get_logger("notifications")


async def send_discord_webhook(
    webhook_url: str,
    title: str,
    description: str,
    fields: list[dict[str, Any]] | None = None,
    color: int = 0x00FF88,  # Green
) -> bool:
    """Send an embed notification to a Discord webhook."""
    embed: dict[str, Any] = {
        "title": title,
        "description": description,
        "color": color,
    }
    if fields:
        embed["fields"] = fields

    payload = {"embeds": [embed]}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(webhook_url, json=payload)
            return resp.is_success
    except Exception as exc:
        log.warning("discord_webhook_failed", error=str(exc))
        return False


async def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
) -> bool:
    """Send a markdown message to a Telegram chat/channel."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            return resp.is_success
    except Exception as exc:
        log.warning("telegram_message_failed", error=str(exc))
        return False


class NotificationDispatcher:
    """Unified dispatcher routing alerts to configured channels."""

    def __init__(self, settings: Settings) -> None:
        self.config = settings.notifications

    @property
    def is_configured(self) -> bool:
        return bool(
            self.config.discord_webhook_url
            or (self.config.telegram_bot_token and self.config.telegram_chat_id)
        )

    async def notify(
        self,
        event: str,
        title: str,
        body: str,
        fields: list[dict[str, Any]] | None = None,
    ) -> None:
        if not self.is_configured:
            return

        tasks = []
        if self.config.discord_webhook_url:
            tasks.append(
                send_discord_webhook(
                    self.config.discord_webhook_url,
                    title=f"[Botsensai] {title}",
                    description=body,
                    fields=fields,
                )
            )

        if self.config.telegram_bot_token and self.config.telegram_chat_id:
            tg_text = f"*Botsensai — {title}*\n\n{body}"
            tasks.append(
                send_telegram_message(
                    self.config.telegram_bot_token,
                    self.config.telegram_chat_id,
                    tg_text,
                )
            )

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def on_candidate_scored(
        self, symbol: str, mint: str, score: float, coverage: float, explanation: str | None
    ) -> None:
        if not self.config.on_high_score or score < self.config.min_score_alert:
            return
        fields = [
            {"name": "Symbol", "value": f"${symbol}", "inline": True},
            {"name": "Score", "value": f"{score:.3f}", "inline": True},
            {"name": "Coverage", "value": f"{coverage:.0%}", "inline": True},
            {"name": "Mint", "value": f"`{mint}`", "inline": False},
        ]
        await self.notify(
            event="high_score",
            title=f"High Conviction Candidate: ${symbol}",
            body=explanation or f"Scored {score:.3f} with {coverage:.0%} coverage.",
            fields=fields,
        )

    async def on_integrity_alarm(self, headline: str, detail: str) -> None:
        if not self.config.on_integrity_alarm:
            return
        fields = [{"name": "Detail", "value": detail, "inline": False}]
        await self.notify(
            event="alarm",
            title=f"⚠ Integrity Alarm: {headline}",
            body=detail,
            fields=fields,
        )


@dataclass
class DesktopAlert:
    title: str
    subtitle: str
    message: str
    sound: bool = True
    critical: bool = False


def send_desktop_notification(alert: DesktopAlert) -> bool:
    """Trigger OS-native toast notification and chime."""
    system = platform.system().lower()

    if system == "darwin":
        sound_name = "Sosumi" if alert.critical else "Glass"
        script = f'display notification "{alert.message}" with title "{alert.title}" subtitle "{alert.subtitle}" sound name "{sound_name}"'
        with contextlib.suppress(Exception):
            subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
            return True

    elif system == "linux":
        urgency = "critical" if alert.critical else "normal"
        with contextlib.suppress(Exception):
            subprocess.run(
                ["notify-send", "-u", urgency, f"{alert.title}: {alert.subtitle}", alert.message],
                check=False,
                capture_output=True,
            )
            return True

    return False


def notify_candidate_detected(symbol: str, score: float, mint: str) -> bool:
    """Dispatch high conviction discovery toast."""
    alert = DesktopAlert(
        title="Botsensai 2.0 Alpha Alert",
        subtitle=f"${symbol} — Conviction: {score:.3f}",
        message=f"Passed all safety vetoes. Mint: {mint[:12]}...",
        sound=True,
        critical=False,
    )
    return send_desktop_notification(alert)


def notify_veto_alarm(symbol: str, veto_reason: str) -> bool:
    """Dispatch critical safety veto warning toast."""
    alert = DesktopAlert(
        title="⚠️ Botsensai Safety Veto",
        subtitle=f"${symbol} — Refused",
        message=f"Hard anti-rug veto triggered: {veto_reason}",
        sound=True,
        critical=True,
    )
    return send_desktop_notification(alert)


__all__ = [
    "DesktopAlert",
    "NotificationDispatcher",
    "notify_candidate_detected",
    "notify_veto_alarm",
    "send_desktop_notification",
    "send_discord_webhook",
    "send_telegram_message",
]
