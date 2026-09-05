"""Unit tests for the Rich Live split-pane terminal user interface (TUI)."""

import pytest
from rich.console import Console

from botsensai.config import Settings
from botsensai.models import Launch, Launchpad, TokenRef, utcnow
from botsensai.store.db import Database
from botsensai.tui import TerminalDashboard, run_tui_loop


def test_terminal_dashboard_panes_render(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    now = utcnow()
    token = TokenRef(mint="testmint123", name="Test", symbol="TEST")
    launch = Launch(token=token, launchpad=Launchpad.PUMPFUN, created_at=now)
    db.upsert_launch(launch)

    settings = Settings(db_path=str(tmp_path / "test.db"))
    console = Console(record=True, width=120, height=40)
    dashboard = TerminalDashboard(settings, console=console)

    dashboard.update_frame(db)

    # Verify all layout slots exist and rendered
    assert dashboard.layout["header"].renderable is not None
    assert dashboard.layout["stream"].renderable is not None
    assert dashboard.layout["candidates"].renderable is not None
    assert dashboard.layout["dossier"].renderable is not None
    assert dashboard.layout["footer"].renderable is not None

    db.close()


@pytest.mark.asyncio
async def test_tui_loop_bounded_run(tmp_path) -> None:
    settings = Settings(db_path=str(tmp_path / "test.db"))
    # Run loop bounded by 0.1s to verify execution without hanging
    await run_tui_loop(settings, max_seconds=0.1)
    assert True
