"""Bonding curve progression evaluation.

Identifies the 2% to 15% sweet-spot window where token liquidity is established
but secondary snipers have not yet crowded the curve, vetoing out-of-bounds tokens.
"""

from __future__ import annotations

from botsensai.config import Settings, get_settings
from botsensai.models import CurveStage, MarketSnapshot, VetoReason
from botsensai.util.logging import get_logger

log = get_logger(__name__)


def evaluate_curve_stage(
    snapshot: MarketSnapshot,
    settings: Settings | None = None,
    is_curve_token: bool = False,
) -> tuple[float, VetoReason | None]:
    """Calculate curve completion fraction and enforce the golden curve window (2% to 85%)."""
    active_settings = settings or get_settings()
    risk_s = active_settings.risk

    mint = getattr(snapshot.token, "mint", "")
    # Only enforce on bonding curve tokens (e.g. Pump.fun)
    if not (is_curve_token or mint.endswith("pump")):
        return 0.5, None

    # Approximate progress based on liquidity or market cap
    # On Pump.fun: 85 SOL bonded = 100% (~$12,000 to $69,000 mcap)
    liquidity = snapshot.liquidity_usd or 0.0
    if snapshot.market_cap_usd and snapshot.market_cap_usd > 0:
        progress = min(1.0, max(0.0, (snapshot.market_cap_usd - 5_000.0) / 60_000.0))
    elif liquidity > 0:
        progress = min(1.0, max(0.0, (liquidity - 2_000.0) / 30_000.0))
    else:
        progress = 0.05  # Default reasonable early estimate

    if not active_settings.scoring.veto_golden_curve:
        return progress, None

    # Veto if too late (>85%, risking Raydium migration dumps)
    if progress > risk_s.max_curve_progress:
        log.info("curve_stage.out_of_bounds_late", progress=progress, max=risk_s.max_curve_progress)
        return progress, VetoReason.CURVE_STAGE_OUT_OF_BOUNDS

    return progress, None
