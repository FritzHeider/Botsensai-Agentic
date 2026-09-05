"""Unit tests for historical replay time-machine."""

from datetime import UTC, datetime

from botsensai.models import Launch, Launchpad, TokenRef
from botsensai.replay import render_replay_table, simulate_replay_ticks
from botsensai.store.db import Database


def test_simulate_replay_ticks(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    token_key = "solana:testmint123"
    token = TokenRef(mint="testmint123", name="Test", symbol="TEST")

    launch = Launch(token=token, launchpad=Launchpad.PUMPFUN, created_at=now)
    db.upsert_launch(launch)

    ticks = simulate_replay_ticks(token_key, db, step_seconds=10.0, max_steps=5)
    assert len(ticks) == 5
    assert ticks[0].offset_seconds == 0.0
    assert ticks[0].action_taken in ("WAIT", "ENTER", "VETO_REFUSE")

    # Render table smoke test
    render_replay_table(token_key, ticks)
    db.close()
