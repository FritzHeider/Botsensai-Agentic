"""Tests for unified supervisor lifecycle."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from botsensai.config import Settings
from botsensai.supervisor import Supervisor


def test_supervisor_initialization_and_timeout():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        settings = Settings(db_path=str(db_path))

        supervisor = Supervisor(
            settings=settings,
            enable_stream=False,
            enable_sweep=False,
            enable_labeller=False,
            integrity_interval=0.1,
        )

        async def run():
            return await supervisor.run(max_seconds=0.3)

        stats = asyncio.run(run())
        assert stats.stopped_because in ("duration_reached", "completed")
