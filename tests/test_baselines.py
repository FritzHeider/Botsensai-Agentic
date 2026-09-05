"""Tests for null-hypothesis baseline strategies and evaluation."""

import json
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from botsensai.backtest.baselines import (
    BaselineScorer,
    evaluate_baselines,
    run_baselines,
)
from botsensai.backtest.engine import Backtester, BacktestResult, TradeRecord
from botsensai.cli import app
from botsensai.config import get_settings
from botsensai.util.synthetic import generate_cohort


def test_baseline_scorer_invalid_mode():
    """`BaselineScorer` raises ValueError for invalid modes."""
    with pytest.raises(ValueError, match="Unknown baseline mode: invalid_mode"):
        BaselineScorer(mode="invalid_mode")


def test_baseline_scorer_modes_and_one_entry_per_token():
    """`BaselineScorer` scores and ensures only one entry per token."""
    settings = get_settings()
    cohort = generate_cohort(n=5, seed=1337)
    tapes = Backtester.tapes_from_synthetic(cohort)

    # Test buy_everything mode
    scorer = BaselineScorer(mode="buy_everything", settings=settings)
    ctx1 = tapes[0].context_at(tapes[0].launch.created_at, peer_values={})
    score1 = scorer.score(ctx1)
    should_enter1, reason1 = scorer.should_enter(score1)
    assert should_enter1 is True
    assert "baseline buy_everything entry" in reason1

    # Second check for same token key should not enter again
    ctx1_again = tapes[0].context_at(tapes[0].snapshots[-1].as_of, peer_values={})
    score1_again = scorer.score(ctx1_again)
    should_enter1_again, _ = scorer.should_enter(score1_again)
    assert should_enter1_again is False


def test_evaluate_baselines_has_edge_and_no_edge():
    """`evaluate_baselines` correctly calculates `has_edge` and note formatting."""
    now = datetime.now(UTC)
    mock_trade_win = TradeRecord(
        token_key="solana:token1",
        symbol="T1",
        entered_at=now,
        exited_at=now,
        score=0.8,
        coverage=1.0,
        regime="normal",
        size_native=1.0,
        pnl_native=2.0,
        multiple=3.0,
        exit_reason="take_profit",
        entry_slippage_bps=5.0,
        hold_seconds=600.0,
        contributions={},
    )
    mock_trade_loss = TradeRecord(
        token_key="solana:token2",
        symbol="T2",
        entered_at=now,
        exited_at=now,
        score=0.8,
        coverage=1.0,
        regime="normal",
        size_native=1.0,
        pnl_native=-0.5,
        multiple=0.5,
        exit_reason="stop_loss",
        entry_slippage_bps=5.0,
        hold_seconds=300.0,
        contributions={},
    )

    comp_high = BacktestResult(
        started_at=now,
        finished_at=now,
        window_start=now,
        window_end=now,
        universe_size=10,
        evaluated=10,
        entered=1,
        trades=[mock_trade_win],
        synthetic=True,
    )
    comp_low = BacktestResult(
        started_at=now,
        finished_at=now,
        window_start=now,
        window_end=now,
        universe_size=10,
        evaluated=10,
        entered=1,
        trades=[mock_trade_loss],
        synthetic=True,
    )

    base_results = {
        "buy_everything": comp_low,
        "random_entry": comp_low,
        "buy_highest_volume": comp_low,
    }

    # Case 1: Composite beats all baselines
    comp1 = evaluate_baselines(comp_high, base_results)
    assert comp1.has_edge is True
    assert "beat all three null-hypothesis baselines" in comp1.notes[0]

    # Case 2: Composite fails against baselines
    comp2 = evaluate_baselines(comp_low, base_results)
    assert comp2.has_edge is False
    assert "does not beat all three null-hypothesis baselines" in comp2.notes[0]
    assert "it has no edge" in comp2.notes[0]


def test_run_baselines_synthetic_universe():
    """`run_baselines` executes all three baseline modes over synthetic tapes."""
    cohort = generate_cohort(n=10, seed=1337)
    tapes = Backtester.tapes_from_synthetic(cohort)
    results = run_baselines(tapes, synthetic=True, seed=1337)

    assert set(results.keys()) == {"buy_everything", "random_entry", "buy_highest_volume"}
    for _mode, res in results.items():
        assert isinstance(res, BacktestResult)
        assert res.synthetic is True


def test_cli_backtest_baselines_option(tmp_path):
    """`botsensai backtest --synthetic --baselines` exits 0 and prints comparison table."""
    out_file = tmp_path / "backtest_baselines.json"
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["backtest", "--synthetic", "--universe", "20", "--baselines", "--out", str(out_file)],
    )

    assert result.exit_code == 0, result.stdout
    assert "baseline comparison (null-hypothesis testing)" in result.stdout
    assert "buy_everything" in result.stdout
    assert "random_entry" in result.stdout
    assert "buy_highest_volume" in result.stdout
    assert out_file.exists()

    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert "baselines" in data
    assert "buy_everything" in data["baselines"]["baselines"]
    assert "random_entry" in data["baselines"]["baselines"]
    assert "buy_highest_volume" in data["baselines"]["baselines"]
    assert "has_edge" in data["baselines"]
