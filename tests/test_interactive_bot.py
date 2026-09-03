"""Unit tests for interactive Telegram/Discord bot command handling."""

import pytest

from botsensai.bot import handle_bot_command
from botsensai.config import Settings


@pytest.mark.asyncio
async def test_bot_commands(tmp_path) -> None:
    settings = Settings(db_path=str(tmp_path / "test.db"))

    # Test /help
    res_help = await handle_bot_command("/help", settings)
    assert "Available commands" in res_help.body

    # Test /regime
    res_regime = await handle_bot_command("/regime", settings)
    assert "Market Regime" in res_regime.title

    # Test /top
    res_top = await handle_bot_command("/top", settings)
    assert "Top Scored" in res_top.title

    # Test /score unknown
    res_score = await handle_bot_command("/score unknown_token", settings)
    assert res_score.command == "/score"
