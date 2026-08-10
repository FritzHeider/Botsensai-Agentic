"""Tests for fill model calibration against empirical trade data."""

from __future__ import annotations

from datetime import UTC, datetime

from botsensai.config import ExecutionSettings
from botsensai.execution.calibration import (
    CalibrationResult,
    calibrate_execution_settings,
    evaluate_fill_calibration,
)
from botsensai.execution.fills import CurveState, FillContext, FillSimulator
from botsensai.models import (
    Chain,
    CurveStage,
    Launch,
    Launchpad,
    MarketSnapshot,
    Order,
    Side,
    TokenRef,
    Trade,
)
from botsensai.store.db import Database


def test_evaluate_fill_calibration_empty_db(tmp_path):
    """An empty database returns a CalibrationResult with 0 matched trades."""
    db_file = tmp_path / "empty.db"
    db = Database(db_file)
    res = evaluate_fill_calibration(db)
    assert isinstance(res, CalibrationResult)
    assert res.matched_trades == 0
    assert res.observed_mean_slippage_bps == 0.0
    assert not res.is_optimistic


def test_evaluate_fill_calibration_with_data(tmp_path):
    """Test evaluation of fill calibration with synthetic trade and snapshot data."""
    db_file = tmp_path / "test.db"
    db = Database(db_file)

    now = datetime.now(UTC)
    t_ref = TokenRef(chain=Chain.SOLANA, mint="TestMint1111111111111111111111111111111111")

    # Insert a launch and snapshot
    launch = Launch(
        token=t_ref,
        launchpad=Launchpad.PUMPFUN,
        created_at=now,
    )
    db.upsert_launch(launch)
    snap = MarketSnapshot(
        token=t_ref,
        as_of=now,
        price_native=1e-6,
        liquidity_usd=1000.0,
        bonding_curve_progress=0.1,
        stage=CurveStage.BONDING,
    )
    db.insert_snapshots([snap])

    # Insert buy trade 5s later with higher effective price (slippage = 500 bps)
    t_time = datetime.fromtimestamp(now.timestamp() + 5.0, tz=UTC)
    tr = Trade(
        token=t_ref,
        signature="sig1",
        as_of=t_time,
        wallet="Wallet1",
        side=Side.BUY,
        amount_token=100_000.0,
        amount_native=0.105,  # price = 1.05e-6 vs snap_price 1.0e-6 -> +500 bps
        price_native=1.05e-6,
    )
    db.insert_trades([tr])

    # Evaluate calibration
    res = evaluate_fill_calibration(db, max_dt_seconds=30.0)
    assert res.matched_trades == 1
    assert res.observed_mean_slippage_bps == 500.0
    assert res.modelled_mean_slippage_bps > 0.0


def test_uncalibrated_vs_calibrated_slippage():
    """Verify that uncalibrated settings yield overly optimistic slippage compared to calibrated."""
    token = TokenRef(chain=Chain.SOLANA, mint="TestMint2222222222222222222222222222222222")
    now = datetime.now(UTC)

    snap = MarketSnapshot(
        token=token,
        as_of=now,
        price_native=1e-6,
        liquidity_usd=500.0,
        bonding_curve_progress=0.05,
        stage=CurveStage.BONDING,
    )
    curve = CurveState.from_snapshot(snap)
    ctx = FillContext(snapshot=snap, curve=curve, recent_volatility=0.5)

    order = Order(
        token=token,
        as_of=now,
        side=Side.BUY,
        size_native=0.5,  # Buy 0.5 SOL
        max_slippage_bps=50_000,
    )

    uncalibrated_cfg = ExecutionSettings(
        base_latency_ms=100.0,
        latency_jitter_ms=50.0,
        fail_probability=0.0,
        sandwich_probability=0.0,
        sandwich_extra_bps=0.0,
    )
    calibrated_cfg = calibrate_execution_settings(uncalibrated_cfg)

    sim_uncal = FillSimulator(uncalibrated_cfg, seed=42)
    sim_cal = FillSimulator(calibrated_cfg, seed=42)

    fill_uncal = sim_uncal.simulate(order, ctx)
    fill_cal = sim_cal.simulate(order, ctx)

    assert not fill_uncal.rejected
    assert not fill_cal.rejected

    # Calibrated settings must include latency, jitter, and sandwich impact
    assert fill_cal.slippage_bps > fill_uncal.slippage_bps
    assert fill_cal.latency_ms > fill_uncal.latency_ms


def test_calibrate_execution_settings_modifies_fields():
    """Verify that calibrate_execution_settings applies intended parameters."""
    cfg = calibrate_execution_settings()
    assert cfg.base_latency_ms == 650.0
    assert cfg.latency_jitter_ms == 350.0
    assert cfg.fail_probability == 0.08
    assert cfg.sandwich_probability == 0.25
    assert cfg.sandwich_extra_bps == 350.0
