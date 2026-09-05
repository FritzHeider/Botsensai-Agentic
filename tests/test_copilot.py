"""Unit tests for AI Copilot and natural language token explainer."""

import pytest

from botsensai.config import Settings
from botsensai.copilot import ask_copilot


@pytest.mark.asyncio
async def test_ask_copilot_smoke(tmp_path) -> None:
    settings = Settings(db_path=str(tmp_path / "test.db"))

    # Test unknown token handling
    resp = await ask_copilot("Why was this vetoed?", "unknown_mint_address_123", settings)
    assert resp.query == "Why was this vetoed?"
    assert "Unable to locate" in resp.answer or resp.composite_score == 0.0
