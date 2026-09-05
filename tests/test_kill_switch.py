"""Tests for the automatic risk kill switch and alerting."""

from __future__ import annotations

import pytest

from botsensai.config import Settings
from botsensai.models import Position, TokenRef, utcnow
from botsensai.pipeline import Pipeline
from botsensai.store.db import Database


def _pipeline(tmp_path) -> Pipeline:
    """A pipeline with a real store and no collectors, so nothing hits a network."""
    settings = Settings()
    db = Database(str(tmp_path / "collect.db"))
    return Pipeline(settings, db=db, collectors=[], broker=None)


@pytest.mark.asyncio
async def test_kill_switch_trips_on_daily_loss_breached(tmp_path):
    pipeline = _pipeline(tmp_path)
    try:
        assert pipeline.settings.risk.kill_switch is False
        pipeline.settings.risk.max_daily_loss_native = 1.0
        pipeline.broker.account.daily_loss_native = 1.5

        async def fake_sweep_discover(report, limit):
            return None
        pipeline._sweep_discover = fake_sweep_discover  # type: ignore[assignment]

        await pipeline.sweep()

        assert pipeline.settings.risk.kill_switch is True
    finally:
        pipeline.db.close()


@pytest.mark.asyncio
async def test_kill_switch_trips_on_three_consecutive_degraded_sweeps(tmp_path):
    pipeline = _pipeline(tmp_path)
    try:
        assert pipeline.settings.risk.kill_switch is False

        # Mock discover to raise an exception, marking it failed
        async def fake_sweep_discover_raise(report, limit):
            report.errors.append("discover failed: exception")
            return None
        pipeline._sweep_discover = fake_sweep_discover_raise  # type: ignore[assignment]

        # Sweep 1: degraded, increments to 1
        await pipeline.sweep()
        assert pipeline._consecutive_degraded_sweeps == 1
        assert pipeline.settings.risk.kill_switch is False

        # Sweep 2: degraded, increments to 2
        await pipeline.sweep()
        assert pipeline._consecutive_degraded_sweeps == 2
        assert pipeline.settings.risk.kill_switch is False

        # Mock a successful sweep in between to verify reset
        async def fake_sweep_discover_success(report, limit):
            from botsensai.collectors.base import CollectionResult
            return CollectionResult(surface="discover", started_at=utcnow())
        pipeline._sweep_discover = fake_sweep_discover_success  # type: ignore[assignment]

        await pipeline.sweep()
        assert pipeline._consecutive_degraded_sweeps == 0
        assert pipeline.settings.risk.kill_switch is False

        # Now do 3 degraded sweeps in a row
        pipeline._sweep_discover = fake_sweep_discover_raise  # type: ignore[assignment]

        await pipeline.sweep()
        assert pipeline._consecutive_degraded_sweeps == 1
        await pipeline.sweep()
        assert pipeline._consecutive_degraded_sweeps == 2
        await pipeline.sweep()
        # On the 3rd, it trips!
        assert pipeline.settings.risk.kill_switch is True
    finally:
        pipeline.db.close()


@pytest.mark.asyncio
async def test_kill_switch_trips_on_expectancy_below_floor(tmp_path):
    pipeline = _pipeline(tmp_path)
    try:
        assert pipeline.settings.risk.kill_switch is False
        pipeline.settings.risk.expectancy_floor = -0.05

        token = TokenRef(name="ABC", symbol="ABC", mint="mint_abc")

        # 1. Add 49 losing trades
        for _ in range(49):
            pos = Position(
                token=token,
                opened_at=utcnow(),
                closed_at=utcnow(),
                realized_pnl_native=-0.1,
                cost_basis_native=1.0,
            )
            pipeline.broker.account.closed.append(pos)

        async def fake_sweep_discover(report, limit):
            return None
        pipeline._sweep_discover = fake_sweep_discover  # type: ignore[assignment]

        # 49 trades should not trip expectancy check (requires >= 50)
        await pipeline.sweep()
        assert pipeline.settings.risk.kill_switch is False

        # 2. Add the 50th trade with a big profit (so mean expectancy is > -0.05)
        pos_50 = Position(
            token=token,
            opened_at=utcnow(),
            closed_at=utcnow(),
            realized_pnl_native=3.0,
            cost_basis_native=1.0,
        )
        pipeline.broker.account.closed.append(pos_50)

        await pipeline.sweep()
        assert pipeline.settings.risk.kill_switch is False

        # 3. Replace 50th trade with a loss to bring expectancy below -0.05
        pipeline.broker.account.closed[-1] = Position(
            token=token,
            opened_at=utcnow(),
            closed_at=utcnow(),
            realized_pnl_native=-2.0,
            cost_basis_native=1.0,
        )

        await pipeline.sweep()
        assert pipeline.settings.risk.kill_switch is True
    finally:
        pipeline.db.close()
