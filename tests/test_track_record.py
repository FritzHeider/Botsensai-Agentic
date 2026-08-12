"""Tests for paper-trading track record generation and CLI command."""

import tempfile
from pathlib import Path

from typer.testing import CliRunner

from botsensai.backtest.engine import BacktestResult, TradeRecord
from botsensai.cli import app
from botsensai.execution.track_record import (
    build_track_record,
    generate_track_record_markdown,
)
from botsensai.models import utcnow

runner = CliRunner()


def test_generate_track_record_markdown() -> None:
    now = utcnow()
    trade = TradeRecord(
        token_key="test_token_key_123",
        symbol="TEST",
        entered_at=now,
        exited_at=now,
        score=0.75,
        coverage=0.9,
        regime="normal",
        size_native=0.25,
        pnl_native=0.05,
        multiple=1.20,
        exit_reason="take profit at 1.2x",
        entry_slippage_bps=15.0,
        hold_seconds=300.0,
    )
    result = BacktestResult(
        started_at=now,
        finished_at=now,
        window_start=now,
        window_end=now,
        universe_size=10,
        evaluated=10,
        entered=1,
        trades=[trade],
        account={
            "starting_native": 10.0,
            "equity_native": 10.05,
            "realized_pnl_native": 0.05,
            "rejected_orders": 0,
            "failed_fills": 0,
            "fills": 2,
        },
        synthetic=True,
    )

    md = generate_track_record_markdown(result, as_of=now)
    assert "# Botsensai Paper-Trading Track Record" in md
    assert "Bootstrap 95% Confidence Interval" in md
    assert "test_token_k" in md
    assert "take profit at 1.2x" in md


def test_build_track_record_synthetic() -> None:
    result, md = build_track_record(synthetic=True, universe=20, capital=10.0)
    assert result.synthetic is True
    assert "Executive Summary" in md
    assert "Bootstrap 95% Confidence Interval" in md or "bootstrap 95% confidence interval" in md.lower()


def test_cli_track_record_command() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_file = Path(tmp_dir) / "TRACK_RECORD.md"
        res = runner.invoke(
            app,
            ["track-record", "--synthetic", "--universe", "20", "--out", str(out_file)],
        )
        assert res.exit_code == 0, res.stdout
        assert out_file.exists()
        content = out_file.read_text(encoding="utf-8")
        assert "# Botsensai Paper-Trading Track Record" in content
        assert "confidence interval" in content.lower()
