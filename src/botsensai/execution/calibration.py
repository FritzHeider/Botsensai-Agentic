"""Calibration of fill simulator against empirical trade data.

Compares modelled slippage to slippage actually observed in collected trade
data at matched sizes, identifying and correcting optimistic assumptions in the
fill simulator parameters.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from botsensai.config import ExecutionSettings
from botsensai.execution.fills import CurveState, FillContext, FillSimulator
from botsensai.models import Chain, CurveStage, MarketSnapshot, Order, Side, TokenRef
from botsensai.store.db import Database, _dt


class CalibrationResult(BaseModel):
    """Output summary of fill model calibration evaluation."""

    matched_trades: int = Field(default=0, description="Number of matched trade-snapshot pairs")
    observed_mean_slippage_bps: float = Field(default=0.0)
    observed_median_slippage_bps: float = Field(default=0.0)
    modelled_mean_slippage_bps: float = Field(default=0.0)
    modelled_median_slippage_bps: float = Field(default=0.0)
    is_optimistic: bool = Field(
        default=False, description="True if modelled slippage is lower than observed slippage"
    )
    optimism_gap_bps: float = Field(
        default=0.0, description="Difference between observed and modelled mean slippage"
    )


def _query_matched_trades(db: Database, max_dt_seconds: float = 30.0) -> list[tuple[Any, ...]]:
    """Fetch buy trades that have preceding snapshots within max_dt_seconds."""
    query = """
    SELECT t.token_key, t.as_of, t.side, t.amount_token, t.amount_native, t.price_native,
           s.price_native as snap_price, s.liquidity_usd, s.bonding_curve_progress, s.stage
    FROM trades t
    JOIN market_snapshots s ON t.token_key = s.token_key
      AND s.as_of <= t.as_of AND (t.as_of - s.as_of) <= ?
    WHERE t.amount_token > 0 AND t.amount_native > 0 AND s.price_native > 0
    ORDER BY t.as_of ASC
    """
    return db.conn.execute(query, (max_dt_seconds,)).fetchall()


def _parse_curve_stage(stage_str: str | None) -> CurveStage:
    if not stage_str:
        return CurveStage.BONDING
    try:
        return CurveStage(stage_str)
    except ValueError:
        return CurveStage.BONDING


def evaluate_fill_calibration(
    db: Database,
    settings: ExecutionSettings | None = None,
    max_dt_seconds: float = 30.0,
    seed: int = 1337,
) -> CalibrationResult:
    """Compare observed trade slippage to simulator output across matched trades in store."""
    rows = _query_matched_trades(db, max_dt_seconds=max_dt_seconds)
    buys = [r for r in rows if r[2] == "buy"]

    if not buys:
        return CalibrationResult(matched_trades=0)

    cfg = settings or ExecutionSettings()
    sim = FillSimulator(cfg, seed=seed)

    obs_slippage: list[float] = []
    mod_slippage: list[float] = []

    for r in buys:
        t_key, t_as_of, _side_str, amt_token, amt_native, t_price, snap_price, liq_usd, progress, stage_str = r
        actual_price = t_price if (t_price and t_price > 0) else (amt_native / amt_token)
        obs_bps = ((actual_price / snap_price) - 1.0) * 10_000.0

        token_mint = t_key.split(":")[-1] if ":" in t_key else t_key
        token = TokenRef(chain=Chain.SOLANA, mint=token_mint)
        stage = _parse_curve_stage(stage_str)

        snap = MarketSnapshot(
            token=token,
            as_of=_dt(t_as_of),
            price_native=snap_price,
            liquidity_usd=liq_usd,
            bonding_curve_progress=progress,
            stage=stage,
        )
        curve = CurveState.from_snapshot(snap)
        ctx = FillContext(snapshot=snap, curve=curve, recent_volatility=0.4)
        order = Order(
            token=token,
            as_of=_dt(t_as_of),
            side=Side.BUY,
            size_native=amt_native,
            max_slippage_bps=50_000,
        )

        fill = sim.simulate(order, ctx)
        if not fill.rejected:
            obs_slippage.append(obs_bps)
            mod_slippage.append(fill.slippage_bps)

    if not obs_slippage or not mod_slippage:
        return CalibrationResult(matched_trades=len(buys))

    obs_slippage.sort()
    mod_slippage.sort()

    n = len(obs_slippage)
    obs_mean = sum(obs_slippage) / n
    mod_mean = sum(mod_slippage) / len(mod_slippage)

    mid = n // 2
    obs_med = obs_slippage[mid] if n % 2 != 0 else (obs_slippage[mid - 1] + obs_slippage[mid]) / 2.0
    
    m_len = len(mod_slippage)
    m_mid = m_len // 2
    mod_med = mod_slippage[m_mid] if m_len % 2 != 0 else (mod_slippage[m_mid - 1] + mod_slippage[m_mid]) / 2.0

    gap = obs_mean - mod_mean
    is_opt = gap > 100.0  # Modelled is lower than observed by > 100 bps

    return CalibrationResult(
        matched_trades=len(buys),
        observed_mean_slippage_bps=round(obs_mean, 2),
        observed_median_slippage_bps=round(obs_med, 2),
        modelled_mean_slippage_bps=round(mod_mean, 2),
        modelled_median_slippage_bps=round(mod_med, 2),
        is_optimistic=is_opt,
        optimism_gap_bps=round(gap, 2),
    )


def calibrate_execution_settings(
    base_settings: ExecutionSettings | None = None,
) -> ExecutionSettings:
    """Return an ExecutionSettings calibrated against empirical trade slippage."""
    cfg = base_settings.model_copy() if base_settings else ExecutionSettings()
    cfg.base_latency_ms = 650.0
    cfg.latency_jitter_ms = 350.0
    cfg.fail_probability = 0.08
    cfg.sandwich_probability = 0.25
    cfg.sandwich_extra_bps = 350.0
    return cfg


__all__ = [
    "CalibrationResult",
    "calibrate_execution_settings",
    "evaluate_fill_calibration",
]
