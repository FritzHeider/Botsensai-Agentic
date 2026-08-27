"""Tests for notification dispatcher."""

from __future__ import annotations

import asyncio

from botsensai.config import NotificationSettings, Settings
from botsensai.notifications import NotificationDispatcher


def test_notification_dispatcher_disabled_by_default():
    settings = Settings(notifications=NotificationSettings(enabled=False))
    dispatcher = NotificationDispatcher(settings)
    assert not dispatcher.is_configured

    # Calling notify when disabled should be a no-op and complete immediately
    async def run():
        await dispatcher.notify(event="test", title="Test Alert", body="Hello")

    asyncio.run(run())
