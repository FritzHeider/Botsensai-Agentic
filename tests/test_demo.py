"""Unit tests for instant zero-config demo runner."""

import pytest

from botsensai.config import Settings
from botsensai.demo import run_demo_simulation, seed_demo_environment
from botsensai.store.db import Database


@pytest.mark.asyncio
async def test_demo_runner(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    tokens = seed_demo_environment(db)
    assert len(tokens) == 2
    assert tokens[0].symbol == "GIGAWHALE"
    assert tokens[1].symbol == "PEPERUG"

    scores = db.recent_scores(limit=10)
    assert len(scores) >= 2

    settings = Settings(db_path=str(tmp_path / "test.db"))
    # Run smoke simulation
    await run_demo_simulation(settings, non_interactive=True)
    db.close()
