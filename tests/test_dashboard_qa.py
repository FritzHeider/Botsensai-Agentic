"""Integration tests for the Dashboard QA suite."""
from __future__ import annotations

from pathlib import Path

import pytest

# Import the QA runner directly
sys_path = Path(__file__).resolve().parents[1] / ".agents" / "skills" / "dashboard-qa" / "scripts"
import sys
sys.path.insert(0, str(sys_path))

pytest.importorskip("playwright")

from run_qa import run_qa
from test_api_stream import test_in_process_api


@pytest.mark.asyncio
async def test_dashboard_qa_against_dash_html(tmp_path):
    dash_path = Path(__file__).resolve().parents[1] / "dash.HTML"
    shot_path = tmp_path / "test_shot.png"

    report = await run_qa(
        target=str(dash_path),
        screenshot_path=str(shot_path),
        timeout_ms=10000,
    )

    assert report.passed, f"Dashboard QA failed: {[c for c in report.checks if not c.passed]}"
    assert len(report.page_errors) == 0
    assert len(report.network_leaks) == 0
    assert shot_path.exists()
    assert shot_path.stat().st_size > 1000


@pytest.mark.asyncio
async def test_dashboard_api_in_process():
    passed = await test_in_process_api()
    assert passed is True
