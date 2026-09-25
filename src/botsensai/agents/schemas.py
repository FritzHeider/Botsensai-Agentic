"""Pydantic schemas for Botsensai Autonomous Sentinel Agent."""

from typing import List, Literal, Optional
from pydantic import BaseModel, Field


class TokenAuthorities(BaseModel):
    """Token authority and extension breakdown."""
    mint_authority: Optional[str] = Field(None, description="Current mint authority public key, if live.")
    freeze_authority: Optional[str] = Field(None, description="Current freeze authority public key, if live.")
    permanent_delegate: Optional[str] = Field(None, description="Token-2022 permanent delegate public key, if configured.")
    is_token_2022: bool = Field(False, description="True if token uses TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb.")
    is_dangerous: bool = Field(False, description="True if freeze authority or permanent delegate is active.")
    has_weaponized_authority: bool = Field(False, description="True if freeze authority or permanent delegate is weaponized against wallet.")


class LiquidityMetrics(BaseModel):
    """On-chain and AMM liquidity profile."""
    dex_id: str = Field("unknown", description="DEX or pool protocol (e.g. pumpfun, raydium, meteora).")
    price_native_sol: float = Field(0.0, description="Token price expressed in native SOL.")
    liquidity_usd: float = Field(0.0, description="Total pool liquidity in USD.")
    volume_5m_usd: float = Field(0.0, description="5-minute trading volume in USD.")
    volume_1h_usd: float = Field(0.0, description="1-hour trading volume in USD.")


class TokenRiskReport(BaseModel):
    """Comprehensive structured risk and conviction verdict returned by the Sentinel Agent."""
    mint: str = Field(..., description="Solana token mint address.")
    symbol: str = Field(..., description="Token ticker symbol.")
    name: str = Field(..., description="Token full name.")
    verdict: Literal["APPROVE", "VETO", "QUARANTINE_DUST"] = Field(
        ...,
        description="Final action verdict: APPROVE for high conviction entries, VETO for high risk / scams, QUARANTINE_DUST for spam ads & airdrop dust."
    )
    risk_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Aggregate risk score from 0.0 (safe) to 1.0 (imminent rug/scam)."
    )
    conviction_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Meme breakout conviction score from 0.0 (no traction) to 1.0 (parabolic runner setup)."
    )
    veto_reasons: List[str] = Field(
        default_factory=list,
        description="List of specific veto triggers (e.g. 'permanent_delegate_live', 'freeze_authority_live', 'promotional_dust_spam')."
    )
    authorities: TokenAuthorities = Field(
        default_factory=TokenAuthorities,
        description="On-chain authority details inspected via Helius DAS."
    )
    liquidity: LiquidityMetrics = Field(
        default_factory=LiquidityMetrics,
        description="Current AMM and bonding curve liquidity metrics."
    )
    narrative_analysis: str = Field(
        ...,
        description="Concise qualitative assessment of narrative novelty, virality, organic vs wash volume, and security markers."
    )
    execution_recommendation: str = Field(
        ...,
        description="Exact execution guidance (e.g., 'Block buy: permanent delegate poses capital theft risk', or 'Proceed with dynamic 0.015 SOL allocation via Jito')."
    )
